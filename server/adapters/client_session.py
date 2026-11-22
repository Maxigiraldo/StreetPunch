"""Una conexión WebSocket ya abierta: dos tareas y un buzón.

    reader ──comandos──> GameSession (cola, put_nowait)
       │
       └─pong/close──> casillas de control ─┐
                                            ├─> writer ──> socket
    tick ──on_welcome / on_snapshot──> buzón┘

REGLA: solo la tarea writer escribe en el socket y hace drain. El reader y el
tick solo dejan bytes en casillas y hacen event.set(), sin ningún await.

Las casillas (pong, close, welcome, buzón) guardan UN valor cada una. Si llega
uno nuevo antes de que el writer envíe el anterior, lo reemplaza:
- buzón: solo importa el último estado (backpressure; se cuenta en métricas);
- pong: el RFC 6455 (§5.5.3) permite contestar solo el ping más reciente. Así,
  un cliente que manda pings sin leer no puede hacer crecer una cola sin límite.
Esta es la "control_queue" del diseño, acotada a una casilla por tipo.
"""

import asyncio
import logging

from server.adapters.codec import JsonFrameEncoder, MessageCodec, MessageError
from server.adapters.ws_frames import CloseCode, Frame, FrameReader, FrameWriter, Opcode, ProtocolError
from server.application.game_session import GameSession
from server.application.ports import SnapshotObserver
from server.application.session_commands import Connect, Disconnect
from server.config import Config
from server.domain.commands import Command, JoinCommand
from server.metrics import Metrics

log = logging.getLogger(__name__)


class ClientSession(SnapshotObserver):
    def __init__(self, player_id: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 game: GameSession, hello_codec: MessageCodec, game_codec: MessageCodec,
                 encoder: JsonFrameEncoder, config: Config, metrics: Metrics) -> None:
        self.player_id = player_id
        self._reader = reader
        self._writer = writer
        self._game = game
        self._game_codec = game_codec
        self._encoder = encoder
        self._cfg = config
        self._metrics = metrics
        self._frames = FrameReader(config.MAX_FRAME_BYTES)

        # Etapa actual: antes de hello solo se acepta hello. Al entrar se
        # cambian codec y destino (un cambio de estado sin if/elif).
        self._codec = hello_codec
        self._deliver = self._deliver_hello

        # Casillas que lee el writer. Las escriben el reader o el tick.
        self._event = asyncio.Event()
        self._mailbox: bytes | None = None
        self._welcome: bytes | None = None
        self._pong: bytes | None = None
        self._close_frames: bytes | None = None

        # Qué hacer con cada tipo de trama: registro, no if/elif.
        self._handlers = {
            Opcode.TEXT: self._on_text,
            Opcode.PING: self._on_ping,
            Opcode.PONG: self._on_pong,
            Opcode.CLOSE: self._on_close,
        }

    # ---------------- SnapshotObserver: los llama el tick ----------------
    # Sin await y sin E/S: solo dejar los bytes y avisar al writer.

    def on_snapshot(self, data: bytes) -> None:
        if self._mailbox is not None:
            self._metrics.mailbox_overwrites += 1   # el writer no alcanzó a enviar el anterior
        self._mailbox = data
        self._event.set()

    def on_welcome(self, data: bytes) -> None:
        self._welcome = data
        self._event.set()

    # ---------------- ciclo de vida ----------------

    async def run(self, leftover: bytes) -> None:
        """Corre reader y writer hasta que uno termine. Siempre avisa la
        salida al tick; el socket lo cierra quien creó la sesión (Listener)."""
        reader_task = asyncio.create_task(self._read_loop(leftover), name=f"reader-{self.player_id}")
        writer_task = asyncio.create_task(self._write_loop(), name=f"writer-{self.player_id}")
        try:
            done, _ = await asyncio.wait({reader_task, writer_task},
                                         return_when=asyncio.FIRST_COMPLETED)
            if reader_task in done and self._close_frames is not None:
                # El reader pidió cerrar (error o close del cliente): se le da
                # al writer un plazo para enviar la trama close.
                await asyncio.wait({writer_task}, timeout=self._cfg.WRITE_TIMEOUT_S)
        finally:
            for task in (reader_task, writer_task):
                task.cancel()
            for result in await asyncio.gather(reader_task, writer_task, return_exceptions=True):
                # Una excepción de este cliente se registra y no pasa de aquí
                # (AGENTS.md §4.9). ConnectionError es la desconexión normal.
                if isinstance(result, Exception) and not isinstance(result, ConnectionError):
                    log.error("cliente %d: %r", self.player_id, result, exc_info=result)
            # Pase lo que pase, el tick se entera. Si nunca mandó hello, es
            # un Disconnect de un id desconocido y no hace nada.
            self._game.submit_membership(Disconnect(self.player_id))

    # ---------------- reader ----------------

    async def _read_loop(self, leftover: bytes) -> None:
        try:
            # El sobrante del saludo HTTP puede traer el comienzo de la primera trama.
            self._frames.feed(leftover)
            while True:
                # Puede haber 0, 1 o varias tramas completas en el búfer.
                while (frame := self._frames.next_frame()) is not None:
                    if not self._handlers[frame.opcode](frame):
                        return
                data = await self._reader.read(self._cfg.READ_CHUNK_BYTES)
                if not data:        # EOF: el cliente cerró el TCP sin close
                    return
                self._frames.feed(data)
        except ProtocolError as err:
            log.info("cliente %d: framing inválido (%s)", self.player_id, err.reason)
            self._request_close(err.close_code)
        except MessageError as err:
            log.info("cliente %d: mensaje inválido (%s)", self.player_id, err.code)
            self._request_close(err.close_code, self._encoder.encode_error(err.code))

    def _on_text(self, frame: Frame) -> bool:
        command = self._codec.decode(frame.payload, self.player_id)
        self._deliver(command)
        return True

    def _deliver_hello(self, command: JoinCommand) -> None:
        # El codec de la etapa hello solo tiene la fábrica de JoinCommand.
        self._game.submit_membership(Connect(command, self))
        self._codec = self._game_codec
        self._deliver = self._deliver_input

    def _deliver_input(self, command: Command) -> None:
        self._game.submit_input(self.player_id, command)   # put_nowait; si está llena, se descarta

    def _on_ping(self, frame: Frame) -> bool:
        self._pong = FrameWriter.pong(frame.payload)
        self._event.set()
        return True

    def _on_pong(self, frame: Frame) -> bool:
        return True   # P6: medirá el RTT y renovará el plazo de inactividad

    def _on_close(self, frame: Frame) -> bool:
        # §5.5.1: al recibir close se responde close y se deja de leer.
        self._request_close(CloseCode.NORMAL)
        return False

    def _request_close(self, code: CloseCode, error_frame: bytes = b"") -> None:
        # El error y el close van juntos en una sola write(): ningún estado
        # puede colarse entre ellos.
        self._close_frames = error_frame + FrameWriter.close(code)
        self._event.set()

    # ---------------- writer ----------------

    async def _write_loop(self) -> None:
        while True:
            # ORDEN (AGENTS.md §4.5): wait, luego clear, y DESPUÉS leer las
            # casillas. Si se leyera antes del clear, un set() del tick que
            # llegue entre la lectura y el clear se borraría: ese estado
            # quedaría en el buzón sin que nadie despertara al writer.
            await self._event.wait()
            self._event.clear()

            # Región crítica SIN await: tomar y vaciar las casillas. Nadie
            # puede intercalarse aquí, porque las otras tareas solo corren en
            # un await.
            close, pong, welcome, state = self._close_frames, self._pong, self._welcome, self._mailbox
            self._pong = self._welcome = self._mailbox = None

            if close is not None:
                self._writer.write(close)
                await self._writer.drain()
                return
            # Prioridad: control, luego welcome (siempre antes del primer
            # estado), luego estado. Cada trama ocupa UNA write().
            for data in (pong, welcome, state):
                if data is not None:
                    self._writer.write(data)
            # drain espera solo si el búfer de envío pasó su marca alta: un
            # cliente lento frena a ESTE writer, nunca al tick ni a los demás.
            await self._writer.drain()
