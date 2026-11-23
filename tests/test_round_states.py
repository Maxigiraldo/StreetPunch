"""Fases de la ronda: transiciones, plazos exactos y robustez (patrón State)."""

import random

import pytest

from server.config import Config
from server.domain.arena import Arena
from server.domain.commands import ChargeCommand, JoinCommand, LeaveCommand, MoveCommand, StartCommand
from server.domain.physics import Physics
from server.domain.round_states import (
    Countdown, Playing, RoundContext, RoundOver, RoundState, Waiting,
)
from server.domain.vector import Vec2
from server.domain.world import World

CFG = Config()


@pytest.fixture
def ctx() -> RoundContext:
    return RoundContext(CFG, Physics(CFG))


@pytest.fixture
def world() -> World:
    return World(CFG, Arena(CFG.ARENA_RADIUS))


def join(world: World, n: int) -> None:
    for i in range(1, n + 1):
        JoinCommand(i, f"p{i}").execute(world)


def tick(state: RoundState, world: World, n: int = 1) -> RoundState:
    for _ in range(n):
        state = state.on_tick(world)
    return state


def begin(ctx: RoundContext, world: World) -> RoundState:
    """El anfitrión (el primero en llegar, id 1) inicia desde la sala."""
    StartCommand(1).execute(world)
    return tick(Waiting(ctx), world)


def to_playing(ctx: RoundContext, world: World) -> RoundState:
    state = begin(ctx, world)
    return tick(state, world, ctx.ticks_for(CFG.COUNTDOWN_S))


def test_waiting_espera_al_minimo(ctx, world):
    join(world, CFG.MIN_PLAYERS_TO_START - 1)
    assert isinstance(tick(Waiting(ctx), world, 100), Waiting)


def test_waiting_inicia_cuenta_regresiva(ctx, world):
    join(world, CFG.MIN_PLAYERS_TO_START)
    state = begin(ctx, world)
    assert isinstance(state, Countdown)
    assert state.remaining_s == pytest.approx(CFG.COUNTDOWN_S)
    assert len(world.discs) == CFG.MIN_PLAYERS_TO_START


def test_cuenta_regresiva_dura_exactamente_su_plazo(ctx, world):
    join(world, 2)
    state = begin(ctx, world)
    n = ctx.ticks_for(CFG.COUNTDOWN_S)
    assert isinstance(tick(state, world, n - 1), Countdown)
    assert isinstance(state.on_tick(world), Playing)


def test_cuenta_regresiva_se_cancela_si_alguien_se_va(ctx, world):
    join(world, 2)
    state = begin(ctx, world)
    LeaveCommand(1).execute(world)
    state = tick(state, world)
    assert isinstance(state, Waiting)
    assert world.discs == {}


def test_discos_quietos_fuera_de_playing(ctx, world):
    join(world, 2)
    state = begin(ctx, world)
    MoveCommand(1, 1, 0).execute(world)
    before = world.disc(1).pos
    tick(state, world, 10)
    assert world.disc(1).pos == before


def test_ultimo_en_pie_gana_y_suma(ctx, world):
    join(world, 3)
    state = to_playing(ctx, world)
    assert isinstance(state, Playing)
    world.disc(1).pos = Vec2(CFG.ARENA_RADIUS * 2, 0)
    state = tick(state, world)
    assert isinstance(state, Playing)  # quedan 2
    world.disc(2).pos = Vec2(CFG.ARENA_RADIUS * 2, 0)
    state = tick(state, world)
    assert isinstance(state, RoundOver)
    assert state.winner == 3
    assert world.scores == {3: 1}


def test_si_se_van_todos_menos_uno_termina_la_ronda(ctx, world):
    join(world, 2)
    state = to_playing(ctx, world)
    LeaveCommand(1).execute(world)
    state = tick(state, world)
    assert isinstance(state, RoundOver) and state.winner == 2


def test_empate_si_caen_los_dos_ultimos_juntos(ctx, world):
    join(world, 2)
    state = to_playing(ctx, world)
    for d in world.discs.values():
        d.pos = Vec2(0, CFG.ARENA_RADIUS * 2)
    state = tick(state, world)
    assert isinstance(state, RoundOver) and state.winner is None
    assert world.scores == {}


def test_resultados_vuelven_a_waiting_y_limpian(ctx, world):
    join(world, 2)
    state = RoundOver(ctx, winner=1)
    world.start_round()
    state = tick(state, world, ctx.ticks_for(CFG.RESULTS_S))
    assert isinstance(state, Waiting)
    assert world.discs == {}


def test_nombres_de_fase_del_protocolo(ctx):
    phases = [Waiting(ctx), Countdown(ctx), Playing(ctx), RoundOver(ctx, None)]
    assert [s.PHASE for s in phases] == ["waiting", "countdown", "playing", "round_over"]
    assert Waiting(ctx).remaining_s is None and Playing(ctx).remaining_s is None


def test_ningun_estado_lanza_excepciones_con_entradas_al_azar(ctx, world):
    """Fuzz: miles de ticks con entradas, salidas y comandos al azar,
    incluidos ids que no existen. Contrato LSP: nunca se lanza nada y
    siempre se devuelve un RoundState."""
    rng = random.Random(1234)  # semilla fija: si falla, se reproduce igual
    state: RoundState = Waiting(ctx)
    next_id = 1
    for _ in range(5000):
        roll = rng.random()
        pid = rng.randint(1, next_id + 2)
        if roll < 0.03:
            JoinCommand(next_id, "x").execute(world)
            next_id += 1
        elif roll < 0.05:
            LeaveCommand(pid).execute(world)
        elif roll < 0.5:
            MoveCommand(pid, rng.choice((-1, 0, 1)), rng.choice((-1, 0, 1))).execute(world)
        elif roll < 0.6:
            ChargeCommand(pid, rng.random() < 0.5).execute(world)
        elif roll < 0.7:
            StartCommand(pid).execute(world)
        state = state.on_tick(world)
        assert isinstance(state, RoundState)
    assert sum(world.scores.values()) > 0  # se jugaron rondas de verdad


# ---------------- sala de espera con anfitrión ----------------

def test_la_sala_no_arranca_sola(ctx, world):
    join(world, CFG.MAX_ROOM_PLAYERS)          # ni siquiera llena
    assert isinstance(tick(Waiting(ctx), world, 500), Waiting) and world.discs == {}


def test_solo_el_anfitrion_inicia(ctx, world):
    join(world, 3)
    StartCommand(2).execute(world)               # no es el anfitrión
    assert isinstance(tick(Waiting(ctx), world), Waiting)
    StartCommand(1).execute(world)
    assert isinstance(tick(Waiting(ctx), world), Countdown)


def test_el_anfitrion_es_el_mas_antiguo_y_pasa_al_siguiente(ctx, world):
    join(world, 3)
    assert world.host() == 1
    LeaveCommand(1).execute(world)
    assert world.host() == 2
    StartCommand(1).execute(world)               # el que se fue ya no manda
    assert isinstance(tick(Waiting(ctx), world), Waiting)


def test_iniciar_sin_minimo_se_descarta_y_no_queda_guardado(ctx, world):
    """Si la orden quedara pendiente, el siguiente en llegar dispararía la ronda
    sin que el anfitrión lo pidiera (el fallo de 'tú + bot0 juegan solos')."""
    join(world, 1)
    waiting = Waiting(ctx)
    StartCommand(1).execute(world)
    assert isinstance(tick(waiting, world), Waiting)
    world.add_player(2, "otro")
    assert isinstance(tick(waiting, world, 5), Waiting)


def test_orden_dada_durante_la_ronda_no_cuenta_para_la_siguiente(ctx, world):
    join(world, 3)
    state = to_playing(ctx, world)
    StartCommand(1).execute(world)               # inicia "de más" a mitad de ronda
    world.disc(2).pos = Vec2(CFG.ARENA_RADIUS * 3, 0)
    world.disc(3).pos = Vec2(CFG.ARENA_RADIUS * 3, 0)
    state = tick(state, world, 1 + ctx.ticks_for(CFG.RESULTS_S))   # RoundOver y fin
    assert isinstance(state, Waiting)
    assert isinstance(tick(state, world, 30), Waiting)             # espera al anfitrión


def test_tras_un_fin_de_ronda_vuelve_a_la_sala(ctx, world):
    join(world, 2)
    state = to_playing(ctx, world)
    world.disc(2).pos = Vec2(CFG.ARENA_RADIUS * 3, 0)
    state = tick(state, world, 1 + ctx.ticks_for(CFG.RESULTS_S))
    assert isinstance(state, Waiting)
    assert isinstance(tick(state, world, 30), Waiting)             # no hay arranque automático
    StartCommand(1).execute(world)
    assert isinstance(tick(state, world), Countdown)
