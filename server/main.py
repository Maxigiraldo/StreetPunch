"""Punto de entrada. Es el ÚNICO lugar que construye y conecta los objetos.

Ejecutar:  .venv/bin/python -m server.main
"""

import asyncio
import json
import logging
import pathlib
import signal
from dataclasses import dataclass

from server.adapters.client_session import ClientSession
from server.adapters.codec import JsonFrameEncoder, game_codec, hello_codec
from server.adapters.http_handshake import HttpHandshake, StaticFile
from server.adapters.ws_frames import CloseCode, FrameWriter
from server.adapters.listener import Listener, create_listen_socket
from server.application.game_session import GameSession
from server.application.send_policies import EveryNthTick, EveryTick
from server.config import Config
from server.domain.arena import Arena
from server.domain.hall import Hall
from server.domain.physics import Physics
from server.domain.round_states import RoundContext
from server.log_filters import RateLimitFilter
from server.metrics import Metrics

WS_PATH = "/ws"
METRICS_PATH = "/metrics"
CLIENT_DIR = pathlib.Path(__file__).resolve().parent.parent / "client"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def load_static(directory: pathlib.Path) -> dict[str, StaticFile]:
    """Lee el cliente UNA vez al arrancar (aquí la E/S síncrona no estorba:
    el event loop todavía no atiende a nadie)."""
    files: dict[str, StaticFile] = {}
    for path in sorted(directory.glob("*")):
        content_type = CONTENT_TYPES.get(path.suffix)
        if content_type is None or not path.is_file():
            continue
        files["/" + path.name] = StaticFile(path.read_bytes(), content_type)
    if "/index.html" in files:
        files["/"] = files["/index.html"]
    return files


@dataclass
class App:
    listener: Listener
    game: GameSession
    metrics: Metrics

    async def run(self) -> None:
        tick = asyncio.create_task(self.game.run_tick_loop(), name="tick")
        try:
            await self.listener.serve()
        finally:
            tick.cancel()

    async def shutdown(self) -> None:
        """Apagado ordenado. El tick sigue corriendo mientras los clientes se
        van (él ejecuta sus Disconnect); run() lo cancela al terminar serve()."""
        await self.listener.shutdown()


def build(config: Config, static: dict[str, StaticFile]) -> App:
    """Arma el grafo de objetos (inversión de dependencias): cada clase recibe
    lo que necesita por constructor y no sabe quién lo creó."""
    metrics = Metrics(config.METRICS_TICK_WINDOW)
    arena = Arena(config.ARENA_RADIUS, config.DISC_RADIUS * config.ELIMINATION_MARGIN_DISCS)
    hall = Hall(config, arena, RoundContext(config, Physics(config)))
    encoder = JsonFrameEncoder(config)
    # Frecuencia de envío por fase de la sala (Strategy): 30 Hz solo mientras se
    # juega; en espera, cuenta atrás y resultados nada se mueve.
    quiet = EveryNthTick(config.QUIET_EVERY_N_TICKS)
    policies = {"waiting": quiet, "countdown": quiet, "playing": EveryTick(), "round_over": quiet}
    game = GameSession(config, hall, encoder, policies, metrics)
    hello, play = hello_codec(config), game_codec()

    def new_session(player_id: int, reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter) -> ClientSession:
        return ClientSession(player_id, reader, writer, game, hello, play, encoder, config, metrics)

    def metrics_page() -> StaticFile:
        # Solo lee contadores y estado (nada se modifica), así que puede
        # llamarse desde el handshake sin romper la regla de único escritor.
        body = {**metrics.snapshot(), "game": game.stats(), "config": {"tick_hz": config.TICK_HZ}}
        return StaticFile(json.dumps(body, separators=(",", ":")).encode(), "application/json")

    handshake = HttpHandshake(static, WS_PATH, config.MAX_HTTP_HEADER_BYTES, config.READ_CHUNK_BYTES,
                              dynamic={METRICS_PATH: metrics_page})
    sock = create_listen_socket(config.HOST, config.PORT, config.LISTEN_BACKLOG)
    full_reply = encoder.encode_error("full") + FrameWriter.close(CloseCode.TRY_AGAIN_LATER)
    listener = Listener(sock, config, handshake, new_session, metrics, full_reply)
    return App(listener, game, metrics)


async def serve_until_signal(app: App) -> None:
    """Corre el servidor hasta SIGINT (Ctrl+C) o SIGTERM (kill, systemd,
    Railway) y entonces lo apaga en orden: close 1001 a cada cliente, plazo
    de gracia y cierre. Un handler de señal no puede hacer await: solo marca
    un Event y la corrutina principal hace el trabajo."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    running = asyncio.create_task(app.run(), name="server")
    stopper = asyncio.create_task(stop.wait(), name="stop-signal")
    try:
        await asyncio.wait({running, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            logging.getLogger(__name__).info("señal recibida: apagando")
            await app.shutdown()
        await running          # propaga un fallo real del servidor, si lo hubo
    finally:
        stopper.cancel()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config()
    # Sin descriptores libres asyncio repite el mismo error miles de veces por
    # segundo; con logging síncrono eso frenaría el tick (ver log_filters.py).
    logging.getLogger("asyncio").addFilter(RateLimitFilter("out of system resource", config.LOG_REPEAT_INTERVAL_S))
    asyncio.run(serve_until_signal(build(config, load_static(CLIENT_DIR))))


if __name__ == "__main__":
    main()
