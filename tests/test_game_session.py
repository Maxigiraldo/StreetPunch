"""GameSession sin sockets: se llama tick() a mano con observadores falsos.

Demuestra las reglas de concurrencia que no dependen de la red: estado
codificado una vez por sala, welcome antes de todo, frecuencia por fase,
límite de entradas, cola llena, membresía que nunca se pierde, y el aislamiento
entre salas (lo de una sala no llega a otra).
"""

import pytest

from server.application.game_session import GameSession
from server.application.ports import Snapshot, SnapshotEncoder, SnapshotObserver, WelcomeInfo
from server.application.send_policies import EveryNthTick, EveryTick
from server.application.session_commands import Connect, Disconnect
from server.config import Config
from server.domain.arena import Arena
from server.domain.commands import (
    CreateRoomCommand, JoinCommand, JoinRoomCommand, LeaveRoomCommand, MoveCommand, StartCommand,
)
from server.domain.hall import Hall
from server.domain.physics import Physics
from server.domain.round_states import RoundContext
from server.metrics import Metrics


class FakeEncoder(SnapshotEncoder):
    def __init__(self) -> None:
        self.state_calls = 0

    def encode_state(self, snapshot: Snapshot) -> bytes:
        self.state_calls += 1
        return f"state:{snapshot.tick}:{snapshot.phase}".encode()

    def encode_welcome(self, welcome: WelcomeInfo) -> bytes:
        return f"welcome:{welcome.id}".encode()

    def encode_roster(self, entries, host) -> bytes:
        return (f"roster:host={host}:" + ",".join(f"{e.id}={e.name}" for e in entries)).encode()

    def encode_rooms(self, rooms) -> bytes:
        return ("rooms:" + ";".join(f"{r.id}={r.players}/{r.max_players}:{r.phase}:{'open' if r.open else 'closed'}"
                                    for r in rooms)).encode()

    def encode_room(self, room_id) -> bytes:
        return f"room:{room_id}".encode()

    def encode_error(self, code: str) -> bytes:
        return f"error:{code}".encode()


class FakeObserver(SnapshotObserver):
    def __init__(self) -> None:
        self.received: list[bytes] = []     # welcome y estados, en orden de llegada
        self.rosters: list[bytes] = []
        self.rooms: list[bytes] = []
        self.notices: list[bytes] = []

    def on_roster(self, data: bytes) -> None:
        self.rosters.append(data)

    def on_rooms(self, data: bytes) -> None:
        self.rooms.append(data)

    def on_notice(self, data: bytes) -> None:
        self.notices.append(data)

    def on_snapshot(self, data: bytes) -> None:
        self.received.append(data)

    def on_welcome(self, data: bytes) -> None:
        self.received.append(data)

    def states(self) -> list[bytes]:
        return [d for d in self.received if d.startswith(b"state:")]


class BrokenObserver(FakeObserver):
    def on_roster(self, data: bytes) -> None:
        raise RuntimeError("socket roto")

    def on_rooms(self, data: bytes) -> None:
        raise RuntimeError("socket roto")

    def on_notice(self, data: bytes) -> None:
        raise RuntimeError("socket roto")

    def on_snapshot(self, data: bytes) -> None:
        raise RuntimeError("socket roto")

    def on_welcome(self, data: bytes) -> None:
        raise RuntimeError("socket roto")


def make(config: Config = Config()) -> tuple[GameSession, FakeEncoder, Metrics]:
    metrics = Metrics(config.METRICS_TICK_WINDOW)
    encoder = FakeEncoder()
    quiet = EveryNthTick(config.QUIET_EVERY_N_TICKS)
    policies = {"waiting": quiet, "countdown": quiet, "playing": EveryTick(), "round_over": quiet}
    hall = Hall(config, Arena(config.ARENA_RADIUS), RoundContext(config, Physics(config)))
    return GameSession(config, hall, encoder, policies, metrics), encoder, metrics


def connect(game: GameSession, player_id: int, observer: SnapshotObserver | None = None) -> FakeObserver:
    observer = observer or FakeObserver()
    game.submit_membership(Connect(JoinCommand(player_id, f"p{player_id}"), observer))
    return observer


def do(game: GameSession, player_id: int, command) -> None:
    """Una orden de un cliente: llega como entrada y el tick la aplica."""
    game.submit_input(player_id, command)


def room_with(game: GameSession, host: int, *guests: int) -> int:
    """Crea una sala con `host` y mete a `guests` (ya conectados). Devuelve su id."""
    do(game, host, CreateRoomCommand(host))
    game.tick()
    room_id = game.hall.room_of(host).id
    for guest in guests:
        do(game, guest, JoinRoomCommand(guest, room_id))
    game.tick()
    return room_id


def start(game: GameSession, host: int) -> None:
    do(game, host, StartCommand(host))


# ---------------- membresía y welcome ----------------

def test_welcome_llega_antes_que_cualquier_otro_mensaje():
    game, _, _ = make()
    obs = connect(game, 1)
    for _ in range(3):
        game.tick()
    assert obs.received[0] == b"welcome:1"
    assert obs.rooms and obs.rooms[0] == b"rooms:"     # en el vestíbulo, y aún no hay salas


def test_desconectado_deja_de_recibir():
    game, _, _ = make()
    obs = connect(game, 1)
    game.tick()
    game.submit_membership(Disconnect(1))
    game.tick()
    n = (len(obs.received), len(obs.rooms), len(obs.notices))
    game.tick()
    assert (len(obs.received), len(obs.rooms), len(obs.notices)) == n


def test_disconnect_no_se_pierde_aunque_la_cola_de_entradas_este_llena():
    cfg = Config(INPUT_QUEUE_SIZE=1)
    game, _, _ = make(cfg)
    connect(game, 1)
    game.tick()
    for _ in range(10):
        game.submit_input(1, MoveCommand(1, 1, 0))
    game.submit_membership(Disconnect(1))
    game.tick()
    assert not game.hall.is_connected(1)


def test_observador_roto_no_detiene_a_los_demas():
    game, _, _ = make()
    connect(game, 1, BrokenObserver())
    good = connect(game, 2)
    room_with(game, 1, 2)
    start(game, 1)
    for _ in range(30):                           # 1 s: en cuenta atrás llegan ~10 estados
        game.tick()
    assert len(good.states()) >= 5


# ---------------- entradas ----------------

def test_maximo_de_entradas_por_cliente_por_tick():
    cfg = Config()
    game, _, metrics = make(cfg)
    connect(game, 1), connect(game, 2)
    room_with(game, 1, 2)
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


def test_las_ordenes_de_sala_tambien_cuentan_para_el_limite():
    """Un cliente que inunde con crear/salir no puede saltarse el tope por tick."""
    cfg = Config()
    game, _, metrics = make(cfg)
    connect(game, 1)
    game.tick()
    for _ in range(cfg.MAX_INPUTS_PER_CLIENT_PER_TICK + 10):
        do(game, 1, LeaveRoomCommand(1))
    game.tick()
    assert metrics.inputs_dropped_rate == 10


# ---------------- vestíbulo: lista de salas ----------------

def test_el_vestibulo_recibe_la_lista_de_salas_solo_cuando_cambia():
    game, _, _ = make()
    a, b = connect(game, 1), connect(game, 2)
    game.tick()
    for _ in range(10):
        game.tick()
    assert a.rooms == [b"rooms:"] and b.rooms == [b"rooms:"]      # una vez, sin repetir
    do(game, 1, CreateRoomCommand(1))
    game.tick()
    assert b.rooms[-1] == b"rooms:1=1/8:waiting:open"            # otra sala nueva: b la ve
    assert len(b.rooms) == 2


def test_quien_llega_al_vestibulo_recibe_la_lista_actual():
    game, _, _ = make()
    connect(game, 1)
    game.tick()
    do(game, 1, CreateRoomCommand(1))
    game.tick()
    late = connect(game, 2)
    game.tick()
    assert late.rooms == [b"rooms:1=1/8:waiting:open"]


def test_quien_esta_en_una_sala_no_recibe_la_lista_y_al_salir_si():
    game, _, _ = make()
    a, b = connect(game, 1), connect(game, 2)
    room_with(game, 1, 2)
    n = len(b.rooms)
    do(game, 1, CreateRoomCommand(1))                              # ya está en sala: se rechaza, no cambia nada
    game.tick()
    assert len(b.rooms) == n                                       # b está en la sala: nada de lista
    do(game, 2, LeaveRoomCommand(2))
    game.tick()
    assert b.rooms[-1] == b"rooms:1=1/8:waiting:open"             # volvió al vestíbulo: la recibe


# ---------------- avisos: cambio de sala y rechazos ----------------

def test_aviso_de_cambio_de_sala_y_de_vuelta_al_vestibulo():
    game, _, _ = make()
    a = connect(game, 1)
    game.tick()
    assert a.notices == []                                         # empieza en el vestíbulo sin avisar
    do(game, 1, CreateRoomCommand(1))
    game.tick()
    assert a.notices == [b"room:1"]
    do(game, 1, LeaveRoomCommand(1))
    game.tick()
    assert a.notices[-1] == b"room:None"


def test_rechazo_sala_llena_llega_solo_al_que_lo_provoco():
    cfg = Config(MAX_ROOM_PLAYERS=2)
    game, _, _ = make(cfg)
    a, b, c = connect(game, 1), connect(game, 2), connect(game, 3)
    room_id = room_with(game, 1, 2)
    do(game, 3, JoinRoomCommand(3, room_id))
    game.tick()
    assert c.notices == [b"error:room_full"] and a.notices == [b"room:1"] and b.notices == [b"room:1"]


def test_unirse_a_sala_inexistente_avisa():
    game, _, _ = make()
    a = connect(game, 1)
    do(game, 1, JoinRoomCommand(1, 99))
    game.tick()
    assert a.notices == [b"error:no_room"]


# ---------------- estados por sala ----------------

def test_estado_se_codifica_una_vez_por_sala_y_es_el_mismo_objeto_para_todos():
    game, encoder, _ = make()
    observers = [connect(game, i) for i in (1, 2, 3)]
    room_with(game, 1, 2, 3)
    start(game, 1)
    game.tick()                                   # arranca la cuenta atrás
    before = encoder.state_calls
    for _ in range(Config().TICK_HZ):             # lo que dura, p. ej., 1 s
        game.tick()
    assert encoder.state_calls - before <= Config().TICK_HZ        # una vez por tick, no una por cliente
    last = [o.received[-1] for o in observers]
    assert all(x is last[0] for x in last)        # identidad, no solo igualdad


def test_el_estado_de_una_sala_no_llega_a_otra():
    game, _, _ = make()
    a, b, c, d = (connect(game, i) for i in (1, 2, 3, 4))
    room_with(game, 1, 2)
    room_with(game, 3, 4)
    start(game, 1)
    for _ in range(10):
        game.tick()
    assert any(b"countdown" in s for s in a.states())
    assert all(b"waiting" in s for s in c.states())                # la otra sala sigue en espera
    assert not any(b"countdown" in s or b"playing" in s for s in d.states())


def test_el_vestibulo_no_recibe_estados():
    game, _, _ = make()
    a, lobby = connect(game, 1), connect(game, 2)
    room_with(game, 1)
    for _ in range(10):
        game.tick()
    assert lobby.states() == []


def test_frecuencia_por_fase_30hz_jugando_y_10hz_en_espera():
    cfg = Config()
    game, _, _ = make(cfg)
    a, b = connect(game, 1), connect(game, 2)
    room_with(game, 1, 2)
    waiting_before = len(a.states())
    for _ in range(cfg.TICK_HZ):                                    # 1 s en espera
        game.tick()
    assert len(a.states()) - waiting_before == pytest.approx(cfg.QUIET_PHASE_HZ, abs=1)
    start(game, 1)
    for _ in range(int(cfg.COUNTDOWN_S * cfg.TICK_HZ) + 2):         # hasta que juega
        game.tick()
    assert game.hall.room_of(1).phase == "playing"
    playing_before = len(a.states())
    for _ in range(cfg.TICK_HZ):
        game.tick()
    assert len(a.states()) - playing_before == cfg.TICK_HZ


def test_un_no_anfitrion_no_puede_iniciar():
    game, _, _ = make()
    connect(game, 1), connect(game, 2)
    room_with(game, 1, 2)
    start(game, 2)
    for _ in range(3):
        game.tick()
    assert game.hall.room_of(1).phase == "waiting"


# ---------------- roster por sala ----------------

def test_roster_llega_a_los_de_la_sala_incluido_el_nuevo():
    game, _, _ = make()
    a, b = connect(game, 1), connect(game, 2)
    room_with(game, 1)
    assert a.rosters == [b"roster:host=1:1=p1"]
    do(game, 2, JoinRoomCommand(2, game.hall.room_of(1).id))
    game.tick()
    assert a.rosters == [b"roster:host=1:1=p1", b"roster:host=1:1=p1,2=p2"]
    assert b.rosters == [b"roster:host=1:1=p1,2=p2"]               # el nuevo recibe la lista completa


def test_roster_no_se_repite_si_nada_cambia():
    game, _, _ = make()
    obs = connect(game, 1)
    room_with(game, 1)
    for _ in range(10):
        game.tick()
    assert len(obs.rosters) == 1


def test_roster_de_una_sala_no_llega_a_otra_ni_al_vestibulo():
    game, _, _ = make()
    a, b, lobby = connect(game, 1), connect(game, 2), connect(game, 3)
    room_with(game, 1)
    room_with(game, 2)
    assert a.rosters == [b"roster:host=1:1=p1"] and b.rosters == [b"roster:host=2:2=p2"]
    assert lobby.rosters == []


def test_roster_al_salir_alguien_ya_no_lo_incluye_y_cambia_el_anfitrion():
    game, _, _ = make()
    a, b = connect(game, 1), connect(game, 2)
    room_with(game, 1, 2)
    game.submit_membership(Disconnect(1))
    game.tick()
    assert b.rosters[-1] == b"roster:host=2:2=p2"


def test_sala_vacia_desaparece_y_su_roster_se_olvida():
    game, _, _ = make()
    connect(game, 1)
    room_with(game, 1)
    do(game, 1, LeaveRoomCommand(1))
    game.tick()
    assert game.hall.rooms == {} and game._last_roster == {}


def test_stats_para_metrics():
    game, _, _ = make()
    connect(game, 1), connect(game, 2), connect(game, 3)
    room_with(game, 1, 2)
    stats = game.stats()
    assert (stats["rooms"], stats["in_rooms"], stats["lobby"], stats["subscribed"]) == (1, 2, 1, 3)
