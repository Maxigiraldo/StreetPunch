"""El vestíbulo: salas, tope de jugadores, entrar solo en espera, salas vacías."""

import pytest

from server.config import Config
from server.domain.arena import Arena
from server.domain.commands import (
    ChargeCommand, CreateRoomCommand, JoinRoomCommand, LeaveRoomCommand, MoveCommand, StartCommand,
)
from server.domain.hall import (
    ALREADY_IN_ROOM, NO_SUCH_ROOM, NOT_IN_ROOM, ROOM_BUSY, ROOM_FULL, TOO_MANY_ROOMS, Hall,
)
from server.domain.physics import Physics
from server.domain.round_states import RoundContext

CFG = Config()


def make_hall(config: Config = CFG) -> Hall:
    return Hall(config, Arena(config.ARENA_RADIUS), RoundContext(config, Physics(config)))


@pytest.fixture
def hall() -> Hall:
    h = make_hall()
    for pid in range(1, 21):
        h.connect(pid, f"p{pid}")
    return h


def codes(hall: Hall) -> list[tuple[int, str]]:
    return hall.drain_rejections()


def test_al_conectar_se_esta_en_el_vestibulo(hall):
    assert hall.room_of(1) is None and hall.lobby_members() == list(range(1, 21))


def test_crear_sala_te_hace_anfitrion(hall):
    hall.create_room(1)
    room = hall.room_of(1)
    assert room is not None and room.world.host() == 1
    assert 1 not in hall.lobby_members()


def test_unirse_a_una_sala_abierta(hall):
    hall.create_room(1)
    hall.join_room(2, hall.room_of(1).id)
    assert hall.room_of(2) is hall.room_of(1)
    assert list(hall.room_of(1).world.players) == [1, 2]      # orden de llegada: el anfitrión primero
    assert codes(hall) == []


def test_sala_llena_no_deja_entrar(hall):
    hall.create_room(1)
    room_id = hall.room_of(1).id
    for pid in range(2, CFG.MAX_ROOM_PLAYERS + 1):
        hall.join_room(pid, room_id)
    assert hall.room_of(1).world.player_count() == CFG.MAX_ROOM_PLAYERS
    hall.join_room(CFG.MAX_ROOM_PLAYERS + 1, room_id)
    assert codes(hall) == [(CFG.MAX_ROOM_PLAYERS + 1, ROOM_FULL)]
    assert hall.room_of(CFG.MAX_ROOM_PLAYERS + 1) is None        # sigue en el vestíbulo


def test_sala_jugando_no_deja_entrar(hall):
    hall.create_room(1)
    hall.join_room(2, hall.room_of(1).id)
    StartCommand(1).run(hall)
    hall.step()                                                  # arranca la cuenta atrás
    assert hall.room_of(1).phase == "countdown"
    hall.join_room(3, hall.room_of(1).id)
    assert codes(hall) == [(3, ROOM_BUSY)]


def test_sala_inexistente(hall):
    hall.join_room(1, 99)
    assert codes(hall) == [(1, NO_SUCH_ROOM)]


def test_no_se_puede_estar_en_dos_salas(hall):
    hall.create_room(1)
    hall.create_room(1)
    hall.join_room(1, hall.room_of(1).id)
    assert codes(hall) == [(1, ALREADY_IN_ROOM), (1, ALREADY_IN_ROOM)]
    assert len(hall.rooms) == 1


def test_tope_de_salas():
    cfg = Config(MAX_ROOMS=2)
    hall = make_hall(cfg)
    for pid in (1, 2, 3):
        hall.connect(pid, f"p{pid}")
        hall.create_room(pid)
    assert len(hall.rooms) == 2
    assert codes(hall) == [(3, TOO_MANY_ROOMS)]


def test_salir_devuelve_al_vestibulo_y_la_sala_vacia_desaparece(hall):
    hall.create_room(1)
    hall.join_room(2, hall.room_of(1).id)
    hall.leave_room(1)
    assert hall.room_of(1) is None and len(hall.rooms) == 1
    assert hall.room_of(2).world.host() == 2          # el anfitrión pasó al siguiente
    hall.leave_room(2)
    assert hall.rooms == {}                           # vacía: desaparece


def test_salir_sin_estar_en_sala_se_rechaza(hall):
    hall.leave_room(1)
    assert codes(hall) == [(1, NOT_IN_ROOM)]


def test_desconectar_saca_de_la_sala_sin_ruido(hall):
    hall.create_room(1)
    hall.disconnect(1)
    assert hall.rooms == {} and not hall.is_connected(1) and codes(hall) == []
    hall.disconnect(1)                                # repetido: no falla


def test_ordenes_de_quien_se_fue_se_ignoran(hall):
    hall.disconnect(5)
    CreateRoomCommand(5).run(hall)
    JoinRoomCommand(5, 1).run(hall)
    assert hall.rooms == {} and codes(hall) == []


def test_orden_de_partida_fuera_de_sala_se_ignora(hall):
    """En el vestíbulo no hay mundo: moverse o cargar no hace nada ni falla."""
    for cmd in (MoveCommand(1, 1, 0), ChargeCommand(1, True), StartCommand(1)):
        cmd.run(hall)
    assert hall.rooms == {}


def test_las_salas_son_independientes(hall):
    hall.create_room(1)
    hall.join_room(2, hall.room_of(1).id)
    hall.create_room(3)
    hall.join_room(4, hall.room_of(3).id)
    StartCommand(1).run(hall)
    hall.step()
    assert hall.room_of(1).phase == "countdown" and hall.room_of(3).phase == "waiting"
    assert hall.room_of(3).world.discs == {}


def test_comandos_de_sala_pasan_por_run_sin_if_de_tipos(hall):
    CreateRoomCommand(1).run(hall)
    JoinRoomCommand(2, hall.room_of(1).id).run(hall)
    LeaveRoomCommand(2).run(hall)
    assert hall.room_of(2) is None and hall.room_of(1).world.player_count() == 1


def test_resumen_de_salas(hall):
    hall.create_room(1)
    hall.join_room(2, hall.room_of(1).id)
    (info,) = hall.summary()
    assert (info.players, info.max_players, info.phase, info.host, info.open) == (
        2, CFG.MAX_ROOM_PLAYERS, "waiting", "p1", True)
    StartCommand(1).run(hall)
    hall.step()
    assert hall.summary()[0].open is False            # jugando: ya no se entra


def test_color_es_el_mismo_en_vestibulo_y_en_sala(hall):
    before = hall.color_of(7)
    hall.create_room(7)
    assert hall.room_of(7).world.players[7].color == before
