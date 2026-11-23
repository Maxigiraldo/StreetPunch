"""Mundo de UNA sala: sus jugadores, los discos de la ronda y el marcador.

REGLA DE CONCURRENCIA: solo el tick toca este objeto (único escritor). Por eso
no lleva ningún lock: en un solo hilo con asyncio, otra tarea solo puede
intercalarse en un `await`, y aquí no hay ninguno. Las demás tareas piden
cambios mediante comandos (server/domain/commands.py) que el tick ejecuta.
"""

from dataclasses import dataclass

from server.config import Config
from server.domain.arena import Arena
from server.domain.disc import Disc


@dataclass(frozen=True)
class Player:
    id: int
    name: str
    color: str


def color_for(config: Config, player_id: int) -> str:
    """Color de un jugador: depende solo de su id, así que es el mismo en el
    vestíbulo y en cualquier sala, y todas las pestañas lo ven igual."""
    colors = config.PLAYER_COLORS
    return colors[player_id % len(colors)]


class World:
    def __init__(self, config: Config, arena: Arena) -> None:
        self._cfg = config
        self.arena = arena
        # Los jugadores de ESTA sala, en orden de llegada (nunca se reordena).
        # El anfitrión es el primero. Todos juegan cada ronda: ya no hay cola.
        self.players: dict[int, Player] = {}
        # Discos de la ronda actual (vivos y eliminados). Vacío fuera de ronda.
        self.discs: dict[int, Disc] = {}
        self.scores: dict[int, int] = {}
        # El anfitrión pidió iniciar. Lo consume la sala de espera (Waiting).
        self._start_requested = False

    # ---------------- registro ----------------

    def add_player(self, player_id: int, name: str) -> Player:
        player = Player(player_id, name, color_for(self._cfg, player_id))
        self.players[player_id] = player
        return player

    def remove_player(self, player_id: int) -> None:
        # pop con valor por defecto: un LeaveCommand repetido no falla.
        self.players.pop(player_id, None)
        self.discs.pop(player_id, None)   # RF-04: su disco desaparece
        self.scores.pop(player_id, None)

    def host(self) -> int | None:
        """Anfitrión de la sala: el que lleva más tiempo en ella (el primero en
        `players`, que nunca se reordena). Se DEDUCE, no se guarda: si el
        anfitrión se va, el siguiente lo es sin ningún código de traspaso."""
        return next(iter(self.players), None)

    def request_start(self, player_id: int) -> None:
        """Solo el anfitrión puede iniciar: la orden de otro se ignora."""
        if player_id == self.host():
            self._start_requested = True

    def take_start_request(self) -> bool:
        requested, self._start_requested = self._start_requested, False
        return requested

    def player_count(self) -> int:
        return len(self.players)

    def disc(self, player_id: int) -> Disc | None:
        return self.discs.get(player_id)

    # ---------------- ronda ----------------

    def start_round(self) -> None:
        """Coloca a TODOS los de la sala: el tope de jugadores lo impone el
        Hall al dejar entrar, así que no hay a quién dejar fuera."""
        points = self.arena.spawn_points(len(self.players), self._cfg.SPAWN_RING_FRACTION)
        self.discs = {}
        for player_id, pos in zip(self.players, points):
            # Cada disco nace mirando al centro: una embestida sin moverse
            # va hacia la pelea, no hacia el vacío.
            self.discs[player_id] = Disc(player_id, pos, facing=(-pos).normalized())

    def end_round(self) -> None:
        self.discs = {}
        self._start_requested = False   # una orden dada durante la ronda no cuenta para la siguiente

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
