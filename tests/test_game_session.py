"""GameSession sin sockets: se llama tick() a mano con observadores falsos.

Demuestra las reglas de concurrencia que no dependen de la red:
estado codificado una vez, welcome antes del primer estado, frecuencias por
rol, límite de entradas, cola llena y membresía que nunca se pierde.
"""

import pytest

from server.application.game_session import GameSession
from server.application.ports import Snapshot, SnapshotEncoder, SnapshotObserver, WelcomeInfo
from server.application.send_policies import EveryNthTick, EveryTick
from server.application.session_commands import Connect, Disconnect
from server.config import Config
from server.domain.arena import Arena
from server.domain.commands import JoinCommand, MoveCommand
from server.domain.physics import Physics
from server.domain.round_states import RoundContext, Waiting
from server.domain.world import ROLE_PLAYER, ROLE_SPECTATOR, World
from server.metrics import Metrics


class FakeEncoder(SnapshotEncoder):
    def __init__(self) -> None:
        self.state_calls = 0

    def encode_state(self, snapshot: Snapshot) -> bytes:
        self.state_calls += 1
        return f"state:{snapshot.tick}:{snapshot.phase}".encode()

    def encode_welcome(self, welcome: WelcomeInfo) -> bytes:
        return f"welcome:{welcome.id}".encode()


class FakeObserver(SnapshotObserver):
    def __init__(self) -> None:
        self.received: list[bytes] = []

    def on_snapshot(self, data: bytes) -> None:
        self.received.append(data)

    def on_welcome(self, data: bytes) -> None:
        self.received.append(data)


class BrokenObserver(SnapshotObserver):
    def on_snapshot(self, data: bytes) -> None:
        raise RuntimeError("socket roto")

    def on_welcome(self, data: bytes) -> None:
        raise RuntimeError("socket roto")


def make(config: Config = Config()) -> tuple[GameSession, FakeEncoder, Metrics]:
    metrics = Metrics(config.METRICS_TICK_WINDOW)
    encoder = FakeEncoder()
    policies = {ROLE_PLAYER: EveryTick(), ROLE_SPECTATOR: EveryNthTick(config.SPECTATOR_EVERY_N_TICKS)}
    game = GameSession(config, World(config, Arena(config.ARENA_RADIUS)),
                       Waiting(RoundContext(config, Physics(config))), encoder, policies, metrics)
    return game, encoder, metrics


def connect(game: GameSession, player_id: int, observer: SnapshotObserver | None = None) -> SnapshotObserver:
    observer = observer or FakeObserver()
    game.submit_membership(Connect(JoinCommand(player_id, f"p{player_id}"), observer))
    return observer


def test_welcome_llega_antes_que_el_primer_estado():
    game, _, _ = make()
    obs = connect(game, 1)
    for _ in range(3):
        game.tick()
    assert obs.received[0] == b"welcome:1"
    assert obs.received[1].startswith(b"state:")


def test_estado_se_codifica_una_vez_y_es_el_mismo_objeto_para_todos():
    game, encoder, _ = make()
    observers = [connect(game, i) for i in (1, 2, 3)]
    game.tick()   # entran y empieza la cuenta: los tres son jugadores
    game.tick()
    assert encoder.state_calls == 2
    last = [o.received[-1] for o in observers]
    assert all(x is last[0] for x in last)   # identidad, no solo igualdad


def test_espectador_a_10hz_y_jugador_a_30hz():
    cfg = Config()
    game, _, _ = make(cfg)
    a, b = connect(game, 1), connect(game, 2)
    game.tick()                                  # la ronda empieza: 1 y 2 juegan
    c = connect(game, 3)                         # llega tarde: espectador
    before = {id(o): len(o.received) for o in (a, b, c)}
    for _ in range(cfg.TICK_HZ):                 # 1 s
        game.tick()
    got = {id(o): len(o.received) - before[id(o)] for o in (a, b, c)}
    assert got[id(a)] == cfg.TICK_HZ
    # c recibió su welcome más ~10 estados
    assert got[id(c)] - 1 == pytest.approx(cfg.SPECTATOR_HZ, abs=1)


def test_maximo_de_entradas_por_cliente_por_tick():
    cfg = Config()
    game, _, metrics = make(cfg)
    connect(game, 1)
    connect(game, 2)
    game.tick()
    for _ in range(cfg.MAX_INPUTS_PER_CLIENT_PER_TICK + 3):
        game.submit_input(1, MoveCommand(1, 1, 0))
    game.submit_input(2, MoveCommand(2, 1, 0))   # el límite es POR cliente
    game.tick()
    assert metrics.inputs_dropped_rate == 3


def test_cola_llena_descarta_y_cuenta_sin_bloquear():
    cfg = Config(INPUT_QUEUE_SIZE=2)
    game, _, metrics = make(cfg)
    results = [game.submit_input(1, MoveCommand(1, 1, 0)) for _ in range(5)]
    assert results == [True, True, False, False, False]
    assert metrics.inputs_dropped_queue == 3


def test_disconnect_no_se_pierde_aunque_la_cola_de_entradas_este_llena():
    cfg = Config(INPUT_QUEUE_SIZE=1)
    game, _, _ = make(cfg)
    connect(game, 1)
    game.tick()
    for _ in range(10):
        game.submit_input(1, MoveCommand(1, 1, 0))
    game.submit_membership(Disconnect(1))
    game.tick()
    assert 1 not in game.world.players


def test_desconectado_deja_de_recibir():
    game, _, _ = make()
    obs = connect(game, 1)
    game.tick()
    game.submit_membership(Disconnect(1))
    game.tick()
    n = len(obs.received)
    game.tick()
    assert len(obs.received) == n


def test_observador_roto_no_detiene_a_los_demas():
    game, _, _ = make()
    connect(game, 1, BrokenObserver())
    good = connect(game, 2)
    for _ in range(5):
        game.tick()
    assert len(good.received) >= 5
