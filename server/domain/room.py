"""Una sala: su mundo y su fase (patrón State) juntos.

Cada sala es una partida independiente: tiene su propio `World` (jugadores,
discos, marcador) y su propia fase. El tick avanza todas las salas, una tras
otra, en el mismo hilo: sigue habiendo UN solo escritor.
"""

from dataclasses import dataclass

from server.domain.round_states import RoundState
from server.domain.world import World


@dataclass
class Room:
    id: int
    name: str
    world: World
    state: RoundState

    def step(self) -> None:
        """Un tick de esta sala: la fase decide qué pasa y cuál es la siguiente."""
        self.state = self.state.on_tick(self.world)

    @property
    def phase(self) -> str:
        return self.state.PHASE
