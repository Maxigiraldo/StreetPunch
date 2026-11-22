"""El writer de un cliente que no lee no puede quedarse esperando para siempre."""

import asyncio

from server.adapters.client_session import ClientSession
from server.config import Config
from server.metrics import Metrics


class StuckTransport:
    aborted = False

    def abort(self) -> None:
        self.aborted = True


class StuckWriter:
    """Un cliente zombi: el búfer de envío está lleno y drain() nunca termina."""
    def __init__(self) -> None:
        self.transport = StuckTransport()
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        await asyncio.Event().wait()


def make_session(cfg: Config, writer: StuckWriter) -> tuple[ClientSession, Metrics]:
    metrics = Metrics(cfg.METRICS_TICK_WINDOW)
    session = ClientSession(1, None, writer, None, None, None, None, cfg, metrics)  # type: ignore[arg-type]
    return session, metrics


def test_drain_sin_terminar_aborta_el_socket_y_cuenta_el_timeout():
    async def go():
        cfg = Config(WRITE_TIMEOUT_S=0.1)
        writer = StuckWriter()
        session, metrics = make_session(cfg, writer)
        session.on_snapshot(b"estado")
        await asyncio.wait_for(session._write_loop(), 2)   # termina sola: no se cuelga
        assert writer.written == [b"estado"]
        assert writer.transport.aborted
        assert metrics.write_timeouts == 1
    asyncio.run(go())


def test_close_pendiente_tambien_respeta_el_plazo_de_escritura():
    async def go():
        cfg = Config(WRITE_TIMEOUT_S=0.1)
        writer = StuckWriter()
        session, metrics = make_session(cfg, writer)
        session.request_shutdown()
        await asyncio.wait_for(session._write_loop(), 2)
        assert writer.transport.aborted and metrics.write_timeouts == 1
    asyncio.run(go())
