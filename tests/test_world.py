"""Mundo y comandos: registro, cola de espera, eliminaciones y marcador."""

import ast
import pathlib

import pytest

from server.config import Config
from server.domain.arena import Arena
from server.domain.commands import ChargeCommand, JoinCommand, LeaveCommand, MoveCommand
from server.domain.vector import Vec2
from server.domain.world import World

CFG = Config()


@pytest.fixture
def world() -> World:
    return World(CFG, Arena(CFG.ARENA_RADIUS))


def join(world: World, n: int) -> None:
    for i in range(1, n + 1):
        JoinCommand(i, f"p{i}").execute(world)


def test_join_registra_con_color(world):
    JoinCommand(7, "Ana").execute(world)
    assert world.players[7].name == "Ana"
    assert world.players[7].color in CFG.PLAYER_COLORS
    assert not world.is_playing(7)  # nunca entra a mitad de ronda


def test_start_round_limita_a_max_activos_y_rota_la_cola(world):
    join(world, CFG.MAX_ACTIVE_PLAYERS + 2)
    world.start_round()
    assert len(world.discs) == CFG.MAX_ACTIVE_PLAYERS
    waiting = [CFG.MAX_ACTIVE_PLAYERS + 1, CFG.MAX_ACTIVE_PLAYERS + 2]
    assert not any(world.is_playing(i) for i in waiting)
    # Los que esperaban quedan primeros en la cola para la siguiente ronda.
    assert world.queue()[:2] == waiting


def test_discos_nacen_mirando_al_centro(world):
    join(world, 2)
    world.start_round()
    for d in world.discs.values():
        assert d.facing.dot(d.pos.normalized()) == pytest.approx(-1.0)


def test_leave_quita_disco_y_marcador(world):
    join(world, 2)
    world.start_round()
    world.award(1)
    LeaveCommand(1).execute(world)
    assert 1 not in world.players and 1 not in world.discs and 1 not in world.scores
    LeaveCommand(1).execute(world)  # repetido: no falla


def test_move_normaliza_y_actualiza_facing(world):
    join(world, 2)
    world.start_round()
    MoveCommand(1, 1, -1).execute(world)
    d = world.disc(1)
    assert d.move_dir.length() == pytest.approx(1.0)
    assert d.facing == d.move_dir


def test_move_con_valores_enormes_no_acelera_mas(world):
    # El servidor no confía en el cliente: la magnitud se descarta.
    join(world, 2)
    world.start_round()
    MoveCommand(1, 1000, 0).execute(world)
    assert world.disc(1).move_dir == Vec2(1, 0)


@pytest.mark.parametrize("cmd", [MoveCommand(99, 1, 0), ChargeCommand(99, True), ChargeCommand(99, False)])
def test_comandos_de_espectador_o_jugador_ido_se_ignoran(world, cmd):
    join(world, 2)
    world.start_round()
    cmd.execute(world)  # no lanza


def test_charge_on_off(world):
    join(world, 2)
    world.start_round()
    ChargeCommand(1, True).execute(world)
    assert world.disc(1).charging
    ChargeCommand(1, False).execute(world)
    assert world.disc(1).release_pending and not world.disc(1).charging


def test_eliminar_fuera_del_borde(world):
    join(world, 2)
    world.start_round()
    world.disc(1).pos = Vec2(CFG.ARENA_RADIUS + 1, 0)
    world.eliminate_outside()
    assert not world.disc(1).alive and world.disc(2).alive
    assert 1 in world.discs  # sigue visible, apagado


def test_award_none_no_suma(world):
    world.award(None)
    assert world.scores == {}


def test_dominio_no_importa_red():
    """Regla de la arquitectura hexagonal: domain/ es puro."""
    forbidden = {"asyncio", "socket", "selectors", "ssl", "threading"}
    domain = pathlib.Path(__file__).parent.parent / "server" / "domain"
    for path in domain.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            assert not names & forbidden, f"{path.name} importa {names & forbidden}"


def test_margen_de_caida_el_disco_cuelga_del_borde_sin_caer():
    cfg = CFG
    margin = cfg.DISC_RADIUS * cfg.ELIMINATION_MARGIN_DISCS
    world = World(cfg, Arena(cfg.ARENA_RADIUS, margin))
    join(world, 2)
    world.start_round()
    world.disc(1).pos = Vec2(cfg.ARENA_RADIUS + margin - 0.1, 0)   # pasó el borde, pero no el margen
    world.disc(2).pos = Vec2(0, cfg.ARENA_RADIUS + margin + 0.1)   # pasó el margen
    world.eliminate_outside()
    assert world.disc(1).alive and not world.disc(2).alive


def test_main_arma_la_arena_con_el_margen_de_la_config():
    import dataclasses

    from server.main import build
    app = build(dataclasses.replace(CFG, PORT=0), {})   # puerto 0: nunca choca con un servidor en marcha
    arena = app.game.world.arena
    assert arena.elimination_radius == CFG.ARENA_RADIUS + CFG.DISC_RADIUS * CFG.ELIMINATION_MARGIN_DISCS
    app.listener._sock.close()


def test_los_que_llegan_despues_de_empezar_la_ronda_juegan_antes_que_los_que_ya_jugaron(world):
    """El fallo visto con 50 bots: quien jugó la ronda 1 volvía a jugar la 2
    porque los que llegaron después quedaban detrás de él en la cola."""
    join(world, CFG.MAX_ACTIVE_PLAYERS)
    world.start_round()
    first = set(world.discs)
    for pid in range(100, 100 + CFG.MAX_ACTIVE_PLAYERS):       # llegan con la ronda ya en marcha
        world.add_player(pid, f"tarde{pid}")
    world.end_round()
    world.start_round()
    assert set(world.discs).isdisjoint(first)                  # ninguno repite


def test_rotacion_justa_todos_juegan_antes_de_que_alguien_repita(world):
    total = CFG.MAX_ACTIVE_PLAYERS * 3 + 5
    join(world, total)
    played: list[int] = []
    for _ in range(4):                                         # 4 rondas: 3 grupos completos + los 5 restantes
        world.start_round()
        played += list(world.discs)
        world.end_round()
    assert set(played[: total]) == set(range(1, total + 1))    # las primeras `total` plazas son todos distintos
