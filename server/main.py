"""Punto de entrada. Es el ÚNICO lugar que construye y conecta los objetos.

Ejecutar:  .venv/bin/python -m server.main
"""

import asyncio
import json
import logging
import pathlib
from dataclasses import dataclass

from server.adapters.client_session import ClientSession
from server.adapters.codec import JsonFrameEncoder, game_codec, hello_codec
from server.adapters.http_handshake import HttpHandshake, StaticFile
from server.adapters.listener import Listener, create_listen_socket
from server.application.game_session import GameSession
from server.application.send_policies import EveryNthTick, EveryTick
from server.config import Config
from server.domain.arena import Arena
from server.domain.physics import Physics
from server.domain.round_states import RoundContext, Waiting
from server.domain.world import ROLE_PLAYER, ROLE_SPECTATOR, World
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


def build(config: Config, static: dict[str, StaticFile]) -> App:
    """Arma el grafo de objetos (inversión de dependencias): cada clase recibe
    lo que necesita por constructor y no sabe quién lo creó."""
    metrics = Metrics(config.METRICS_TICK_WINDOW)
    world = World(config, Arena(config.ARENA_RADIUS))
    initial = Waiting(RoundContext(config, Physics(config)))
    encoder = JsonFrameEncoder(config)
    policies = {
        ROLE_PLAYER: EveryTick(),
        ROLE_SPECTATOR: EveryNthTick(config.SPECTATOR_EVERY_N_TICKS),
    }
    game = GameSession(config, world, initial, encoder, policies, metrics)
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
    listener = Listener(sock, config, handshake, new_session, metrics)
    return App(listener, game, metrics)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config()
    try:
        # P6 reemplaza esto por SIGTERM/SIGINT con cierre ordenado (close 1001).
        asyncio.run(build(config, load_static(CLIENT_DIR)).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
