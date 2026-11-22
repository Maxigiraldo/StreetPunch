"""Socket de escucha creado a mano y aceptación de conexiones.

Prohibido asyncio.start_server(host, port): ahí asyncio crearía y configuraría
el socket por nosotros. Aquí cada paso del ciclo de vida queda explícito y
luego el socket ya listo se entrega con start_server(sock=...).
"""

import asyncio
import itertools
import logging
import socket
from collections.abc import Callable

from server.adapters.client_session import ClientSession
from server.adapters.http_handshake import HandshakeResult, HttpHandshake
from server.config import Config
from server.metrics import Metrics

log = logging.getLogger(__name__)

SessionFactory = Callable[[int, asyncio.StreamReader, asyncio.StreamWriter], ClientSession]


def create_listen_socket(host: str, port: int, backlog: int) -> socket.socket:
    # AF_INET = IPv4; SOCK_STREAM = TCP (flujo de bytes confiable y ordenado).
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # SO_REUSEADDR: al cerrar el servidor, las conexiones que cerramos
        # nosotros quedan en TIME_WAIT (~60 s en Linux) para absorber paquetes
        # retrasados. Sin esta opción, bind() fallaría con "Address already in
        # use" hasta que expiren, y no podríamos reiniciar de inmediato.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        # listen(backlog): el kernel completa por su cuenta el saludo TCP
        # (SYN, SYN-ACK, ACK) y encola la conexión lista en la cola de
        # aceptación, de hasta `backlog` entradas (recortado a somaxconn),
        # hasta que la aceptemos.
        sock.listen(backlog)
        # No bloqueante: accept() nunca detiene el hilo. El event loop
        # registra el socket en epoll y solo lo acepta cuando hay algo listo.
        sock.setblocking(False)
    except BaseException:
        sock.close()   # sin fuga de descriptor si bind/listen fallan
        raise
    return sock


class Listener:
    def __init__(self, sock: socket.socket, config: Config, handshake: HttpHandshake,
                 session_factory: SessionFactory, metrics: Metrics, full_reply: bytes) -> None:
        self._sock = sock
        self._cfg = config
        self._handshake = handshake
        self._session_factory = session_factory
        self._metrics = metrics
        # Trama `error full` + close 1013, ya codificada (se envía tal cual).
        self._full_reply = full_reply
        # Cupos de conexión. Se toman al aceptar y se devuelven en finally.
        self._slots = asyncio.Semaphore(config.MAX_CONNECTIONS)
        # next() sobre un contador no tiene await: dos conexiones nunca
        # reciben el mismo id.
        self._ids = itertools.count(1)
        self._rejecting = 0
        # Para el apagado ordenado: todo socket abierto y toda sesión viva.
        # Solo el event loop los toca, y add/discard no tienen await.
        self._writers: set[asyncio.StreamWriter] = set()
        self._sessions: set[ClientSession] = set()
        self._no_connections = asyncio.Event()
        self._no_connections.set()
        self._server: asyncio.Server | None = None
        self._closing = False

    @property
    def port(self) -> int:
        return self._sock.getsockname()[1]

    async def serve(self) -> None:
        # backlog explícito: start_server vuelve a llamar a sock.listen() con
        # su valor por defecto (100) y pisaría el listen(LISTEN_BACKLOG) que
        # hicimos a mano. Se comprueba con `ss -tlnp` (columna Send-Q).
        self._server = await asyncio.start_server(self._on_connect, sock=self._sock,
                                                  backlog=self._cfg.LISTEN_BACKLOG)
        log.info("escuchando en %s:%d", *self._sock.getsockname())
        # Si accept() falla por EMFILE/ENFILE (sin descriptores), asyncio NO
        # tumba el servidor: registra el error, deja de aceptar 1 s y reintenta
        # (selector_events._accept_connection). Se comprueba con `ulimit -n`.
        try:
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            # server.close() (apagado) también cancela serve_forever: eso es
            # una parada ordenada, no una cancelación de quien nos llamó.
            if not self._closing:
                raise

    async def shutdown(self) -> None:
        """Apagado ordenado: deja de aceptar, avisa a cada cliente con close
        1001 y espera SHUTDOWN_GRACE_S a que se vayan; los que queden se cortan."""
        self._closing = True
        if self._server is not None:
            self._server.close()          # cierra el socket de escucha: no se acepta a nadie más
        for session in list(self._sessions):
            session.request_shutdown()    # el writer de cada cliente envía el close
        try:
            await asyncio.wait_for(self._no_connections.wait(), self._cfg.SHUTDOWN_GRACE_S)
        except TimeoutError:
            log.warning("%d conexión(es) no cerraron a tiempo; se cortan", len(self._writers))
            for writer in list(self._writers):
                writer.transport.abort()
            await self._no_connections.wait()   # abort() dispara los finally de cada una

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        self._no_connections.clear()
        try:
            # Región crítica: entre locked() y acquire() no hay suspensión. Con
            # cupo libre y sin nadie esperando, Semaphore.acquire() descuenta y
            # retorna sin ceder el loop, así que nadie puede robar el cupo en
            # medio. Si no hay cupo, se rechaza en vez de esperar: una conexión
            # en espera también ocupa un descriptor.
            if self._slots.locked():
                await self._reject(reader, writer)
            else:
                await self._slots.acquire()
                try:
                    await self._serve_client(reader, writer)
                finally:
                    self._slots.release()
        finally:
            await self._close(writer)     # SIEMPRE: cierra el socket y libera el descriptor
            self._writers.discard(writer)
            if not self._writers:
                self._no_connections.set()

    async def _serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._metrics.connections_active += 1
        self._metrics.connections_total += 1
        session: ClientSession | None = None
        try:
            result = await self._handshake_with_timeout(reader, writer)
            if result is not None and result.upgraded:
                session = self._session_factory(next(self._ids), reader, writer)
                self._sessions.add(session)
                await session.run(result.leftover)
        except ConnectionError:
            pass   # el cliente se fue: es normal, no es un error del servidor
        except Exception:
            # Ninguna excepción de un cliente puede tumbar el servidor.
            log.exception("error atendiendo una conexión")
        finally:
            if session is not None:
                self._sessions.discard(session)
            self._metrics.connections_active -= 1

    async def _handshake_with_timeout(self, reader: asyncio.StreamReader,
                                      writer: asyncio.StreamWriter) -> HandshakeResult | None:
        """None si el saludo HTTP no terminó a tiempo (slowloris o cliente roto)."""
        try:
            return await asyncio.wait_for(self._handshake.handle(reader, writer),
                                          self._cfg.HANDSHAKE_TIMEOUT_S)
        except TimeoutError:
            self._metrics.handshake_timeouts += 1
            log.info("saludo HTTP sin terminar en %.0f s; se cierra", self._cfg.HANDSHAKE_TIMEOUT_S)
            return None

    async def _reject(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Servidor lleno: se contesta el saludo y, si era un WebSocket, se le
        dice `full` y se cierra con 1013 ("vuelve a intentarlo luego"). Las
        páginas HTTP normales se siguen sirviendo. Acotado por
        MAX_REJECTING_HANDSHAKES para que rechazar no agote los descriptores."""
        self._metrics.connections_rejected += 1
        if self._rejecting >= self._cfg.MAX_REJECTING_HANDSHAKES:
            writer.transport.abort()
            return
        self._rejecting += 1
        try:
            result = await self._handshake_with_timeout(reader, writer)
            if result is not None and result.upgraded:
                writer.write(self._full_reply)   # error + close en UNA write()
                await asyncio.wait_for(writer.drain(), self._cfg.WRITE_TIMEOUT_S)
        except (ConnectionError, TimeoutError):
            pass
        except Exception:
            log.exception("error rechazando una conexión")
        finally:
            self._rejecting -= 1

    async def _close(self, writer: asyncio.StreamWriter) -> None:
        """Cierra el socket y libera el descriptor en TODOS los casos.

        close() intenta enviar primero lo que quede en el búfer. Con un
        cliente que no lee, eso podría no terminar nunca, así que se espera un
        plazo y luego abort(), que descarta el búfer y cierra ya (envía RST).
        """
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), self._cfg.WRITE_TIMEOUT_S)
        except (TimeoutError, ConnectionError):
            writer.transport.abort()
