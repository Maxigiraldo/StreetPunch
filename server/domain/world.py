"""Mundo del juego: registro de jugadores, discos de la ronda y marcador.

REGLA DE CONCURRENCIA: solo el tick toca este objeto (único escritor). Por eso
no lleva ningún lock: en un solo hilo con asyncio, otra tarea solo puede
intercalarse en un `await`, y aquí no hay ninguno. Las demás tareas piden
cambios mediante comandos (server/domain/commands.py) que el tick ejecuta.
"""

from dataclasses import dataclass

from server.config import Config
from server.domain.arena import Arena
from server.domain.disc import Disc


ROLE_PLAYER = "player"
ROLE_SPECTATOR = "spectator"


@dataclass(frozen=True)
class Player:
    id: int
    name: str
    color: str


class World:
    def __init__(self, config: Config, arena: Arena) -> None:
        self._cfg = config
        self.arena = arena
        # El orden de inserción del dict ES la cola para jugar: los primeros
        # MAX_ACTIVE_PLAYERS entran a la siguiente ronda.
        self.players: dict[int, Player] = {}
        # Discos de la ronda actual (vivos y eliminados). Vacío fuera de ronda.
        self.discs: dict[int, Disc] = {}
        self.scores: dict[int, int] = {}

    # ---------------- registro ----------------

    def add_player(self, player_id: int, name: str) -> Player:
        colors = self._cfg.PLAYER_COLORS
        player = Player(player_id, name, colors[player_id % len(colors)])
        self.players[player_id] = player
        return player

    def remove_player(self, player_id: int) -> None:
        # pop con valor por defecto: un LeaveCommand repetido no falla.
        self.players.pop(player_id, None)
        self.discs.pop(player_id, None)   # RF-04: su disco desaparece
        self.scores.pop(player_id, None)

    def player_count(self) -> int:
        return len(self.players)

    def disc(self, player_id: int) -> Disc | None:
        return self.discs.get(player_id)

    def is_playing(self, player_id: int) -> bool:
        """Jugador activo en la ronda actual (recibe a 30 Hz)."""
        return player_id in self.discs

    def role_of(self, player_id: int) -> str:
        """Rol con los nombres del protocolo; la aplicación lo usa como clave
        del registro de políticas de envío."""
        return ROLE_PLAYER if self.is_playing(player_id) else ROLE_SPECTATOR

    # ---------------- ronda ----------------

    def start_round(self) -> None:
        """Coloca a los primeros de la cola y los manda al final de ella: así
        los espectadores en espera entran en la ronda siguiente (RF-03)."""
        chosen = list(self.players)[: self._cfg.MAX_ACTIVE_PLAYERS]
        points = self.arena.spawn_points(len(chosen), self._cfg.SPAWN_RING_FRACTION)
        self.discs = {}
        for player_id, pos in zip(chosen, points):
            # Cada disco nace mirando al centro: una embestida sin moverse
            # va hacia la pelea, no hacia el vacío.
            self.discs[player_id] = Disc(player_id, pos, facing=(-pos).normalized())
            self.players[player_id] = self.players.pop(player_id)  # al final de la cola

    def end_round(self) -> None:
        self.discs = {}

    def alive_discs(self) -> list[Disc]:
        return [d for d in self.discs.values() if d.alive]

    def eliminate_outside(self) -> None:
        """RF-10: quien sale del borde queda eliminado; su disco se queda en
        la lista con alive=False para que el cliente lo dibuje apagado."""
        for disc in self.alive_discs():
            if not self.arena.contains(disc):
                disc.alive = False

    def award(self, winner_id: int | None) -> None:
        if winner_id is not None:
            self.scores[winner_id] = self.scores.get(winner_id, 0) + 1
