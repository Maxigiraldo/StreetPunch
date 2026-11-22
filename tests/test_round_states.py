"""Fases de la ronda: transiciones, plazos exactos y robustez (patrón State)."""

import random

import pytest

from server.config import Config
from server.domain.arena import Arena
from server.domain.commands import ChargeCommand, JoinCommand, LeaveCommand, MoveCommand
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


def to_playing(ctx: RoundContext, world: World) -> RoundState:
    state = tick(Waiting(ctx), world)
    return tick(state, world, ctx.ticks_for(CFG.COUNTDOWN_S))


def test_waiting_espera_al_minimo(ctx, world):
    join(world, CFG.MIN_PLAYERS_TO_START - 1)
    assert isinstance(tick(Waiting(ctx), world, 100), Waiting)


def test_waiting_inicia_cuenta_regresiva(ctx, world):
    join(world, CFG.MIN_PLAYERS_TO_START)
    state = tick(Waiting(ctx), world)
    assert isinstance(state, Countdown)
    assert state.remaining_s == pytest.approx(CFG.COUNTDOWN_S)
    assert len(world.discs) == CFG.MIN_PLAYERS_TO_START


def test_cuenta_regresiva_dura_exactamente_su_plazo(ctx, world):
    join(world, 2)
    state = tick(Waiting(ctx), world)
    n = ctx.ticks_for(CFG.COUNTDOWN_S)
    assert isinstance(tick(state, world, n - 1), Countdown)
    assert isinstance(state.on_tick(world), Playing)


def test_cuenta_regresiva_se_cancela_si_alguien_se_va(ctx, world):
    join(world, 2)
    state = tick(Waiting(ctx), world)
    LeaveCommand(1).execute(world)
    state = tick(state, world)
    assert isinstance(state, Waiting)
    assert world.discs == {}


def test_discos_quietos_fuera_de_playing(ctx, world):
    join(world, 2)
    state = tick(Waiting(ctx), world)
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
        state = state.on_tick(world)
        assert isinstance(state, RoundState)
    assert sum(world.scores.values()) > 0  # se jugaron rondas de verdad
