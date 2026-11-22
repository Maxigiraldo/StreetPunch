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
from server.adapters.http_handshake import HttpHandshake
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
                 session_factory: SessionFactory, metrics: Metrics) -> None:
        self._sock = sock
        self._cfg = config
        self._handshake = handshake
        self._session_factory = session_factory
        self._metrics = metrics
        # Cupos de conexión. Se toman al aceptar y se devuelven en finally.
        self._slots = asyncio.Semaphore(config.MAX_CONNECTIONS)
        # next() sobre un contador no tiene await: dos conexiones nunca
        # reciben el mismo id.
        self._ids = itertools.count(1)

    @property
    def port(self) -> int:
        return self._sock.getsockname()[1]

    async def serve(self) -> None:
        # backlog explícito: start_server vuelve a llamar a sock.listen() con
        # su valor por defecto (100) y pisaría el listen(LISTEN_BACKLOG) que
        # hicimos a mano. Se comprueba con `ss -tlnp` (columna Send-Q).
        server = await asyncio.start_server(self._on_connect, sock=self._sock,
                                            backlog=self._cfg.LISTEN_BACKLOG)
        log.info("escuchando en %s:%d", *self._sock.getsockname())
        async with server:
            await server.serve_forever()

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Región crítica: entre locked() y acquire() no hay suspensión. Con
        # cupo libre y sin nadie esperando, Semaphore.acquire() descuenta y
        # retorna sin ceder el loop, así que nadie puede robar el cupo en
        # medio. Si no hay cupo, se rechaza en vez de esperar: una conexión
        # en espera también ocupa un descriptor.
        if self._slots.locked():
            self._metrics.connections_rejected += 1
            writer.close()   # P6: responder 1013 tras el saludo
            return
        await self._slots.acquire()
        self._metrics.connections_active += 1
        self._metrics.connections_total += 1
        try:
            result = await self._handshake.handle(reader, writer)
            if result.upgraded:
                session = self._session_factory(next(self._ids), reader, writer)
                await session.run(result.leftover)
        except ConnectionError:
            pass   # el cliente se fue: es normal, no es un error del servidor
        except Exception:
            # Ninguna excepción de un cliente puede tumbar el servidor.
            log.exception("error atendiendo una conexión")
        finally:
            await self._close(writer)
            self._slots.release()
            self._metrics.connections_active -= 1

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
