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
    """Sala de espera. La ronda NO arranca sola: la inicia el anfitrión (el
    primero en llegar) cuando ve en la lista que ya están todos, y solo si
    hay MIN_PLAYERS_TO_START. Antes arrancaba al reunir el mínimo y quien
    llegaba segundo jugaba solo contra el primero mientras el resto esperaba;
    cualquier regla automática de "cuántos esperar" repetía ese problema."""
    PHASE = "waiting"

    def on_tick(self, world: World) -> RoundState:
        # take_start_request se llama SIEMPRE: una orden dada sin mínimo de
        # jugadores se descarta en vez de quedar guardada y disparar la ronda
        # cuando llegue el siguiente.
        requested = world.take_start_request()
        if requested and world.player_count() >= self._ctx.config.MIN_PLAYERS_TO_START:
            world.start_round()
            return Countdown(self._ctx)
        return self


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
        return Waiting(self._ctx)
