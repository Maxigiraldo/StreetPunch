"""Fases de la ronda (patrón State).

El tick no pregunta "¿en qué fase estoy?": llama a state.on_tick(world) y
recibe el siguiente estado. Cada fase sabe qué hacer y a cuál pasar. Una fase
nueva es una clase nueva; el tick no se toca (OCP).

Contrato común (LSP): on_tick nunca lanza excepciones y siempre devuelve un
RoundState (puede ser él mismo). PHASE, remaining_s y winner alimentan el
mensaje `state` del protocolo.

Los plazos se cuentan en TICKS enteros, no restando segundos con decimales:
3.0 - 90 * (1/30) no da exactamente 0 en coma flotante, y la fase duraría un
tick de más o de menos según el redondeo.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from server.config import Config
from server.domain.physics import Physics
from server.domain.world import World


@dataclass(frozen=True)
class RoundContext:
    """Lo que todas las fases necesitan; se pasa de una fase a la siguiente."""
    config: Config
    physics: Physics

    def ticks_for(self, seconds: float) -> int:
        return round(seconds * self.config.TICK_HZ)


class RoundState(ABC):
    PHASE: str

    def __init__(self, ctx: RoundContext) -> None:
        self._ctx = ctx

    @abstractmethod
    def on_tick(self, world: World) -> "RoundState": ...

    @property
    def remaining_s(self) -> float | None:
        """Segundos que le quedan a la fase; None si no tiene plazo."""
        return None

    @property
    def winner(self) -> int | None:
        return None


class _TimedState(RoundState):
    """Base para fases con plazo: comparten la cuenta de ticks."""

    def __init__(self, ctx: RoundContext, seconds: float) -> None:
        super().__init__(ctx)
        self._ticks_left = ctx.ticks_for(seconds)

    @property
    def remaining_s(self) -> float:
        return self._ticks_left / self._ctx.config.TICK_HZ


class Waiting(RoundState):
    """Espera jugadores. Con el mínimo reunido abre una sala de espera de
    LOBBY_WAIT_S para que lleguen más; si se llena, arranca de inmediato.

    `lobby=False` (tras un fin de ronda) arranca en cuanto hay mínimo: el
    grupo ya está reunido y esperar otra vez solo haría perder tiempo.
    """
    PHASE = "waiting"

    def __init__(self, ctx: RoundContext, lobby: bool = True) -> None:
        super().__init__(ctx)
        self._lobby_ticks = ctx.ticks_for(ctx.config.LOBBY_WAIT_S) if lobby else 0
        self._ticks_left: int | None = None   # None = aún no se reúne el mínimo

    @property
    def remaining_s(self) -> float | None:
        """Segundos para que arranque, solo mientras corre la sala de espera."""
        if self._ticks_left is None:
            return None
        return self._ticks_left / self._ctx.config.TICK_HZ

    def on_tick(self, world: World) -> RoundState:
        cfg = self._ctx.config
        players = world.player_count()
        if players < cfg.MIN_PLAYERS_TO_START:
            self._ticks_left = None      # alguien se fue: la sala empieza de cero
            return self
        if players >= cfg.MAX_ACTIVE_PLAYERS:
            return self._start(world)    # sala llena: no hay a quién esperar
        if self._ticks_left is None:
            self._ticks_left = self._lobby_ticks
        if self._ticks_left > 0:
            self._ticks_left -= 1
            return self
        return self._start(world)

    def _start(self, world: World) -> RoundState:
        world.start_round()
        return Countdown(self._ctx)


class Countdown(_TimedState):
    PHASE = "countdown"

    def __init__(self, ctx: RoundContext) -> None:
        super().__init__(ctx, ctx.config.COUNTDOWN_S)

    def on_tick(self, world: World) -> RoundState:
        # Alguien se fue durante la cuenta y ya no alcanza: se cancela.
        if len(world.alive_discs()) < self._ctx.config.MIN_PLAYERS_TO_START:
            world.end_round()
            return Waiting(self._ctx)
        self._ticks_left -= 1
        if self._ticks_left > 0:
            return self
        return Playing(self._ctx)


class Playing(RoundState):
    PHASE = "playing"

    def on_tick(self, world: World) -> RoundState:
        self._ctx.physics.step(world.alive_discs(), self._ctx.config.TICK_DT)
        world.eliminate_outside()
        alive = world.alive_discs()
        # RF-04 / RF-10: la ronda termina cuando queda uno (o ninguno, si los
        # dos últimos cayeron en el mismo tick: empate, nadie suma).
        if len(alive) > 1:
            return self
        winner = alive[0].id if alive else None
        world.award(winner)
        return RoundOver(self._ctx, winner)


class RoundOver(_TimedState):
    PHASE = "round_over"

    def __init__(self, ctx: RoundContext, winner: int | None) -> None:
        super().__init__(ctx, ctx.config.RESULTS_S)
        self._winner = winner

    @property
    def winner(self) -> int | None:
        return self._winner

    def on_tick(self, world: World) -> RoundState:
        self._ticks_left -= 1
        if self._ticks_left > 0:
            return self
        world.end_round()
        return Waiting(self._ctx, lobby=False)   # el grupo ya está reunido
