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
        # Todos los conectados, en ORDEN DE LLEGADA (nunca se reordena). La
        # cola para jugar se calcula aparte, en queue(): reordenar este dict
        # al empezar la ronda fallaba, porque los que llegaban DESPUÉS
        # quedaban detrás de quienes ya habían jugado y el mismo grupo volvía
        # a ser el primero de la cola (visto con 50 bots).
        self.players: dict[int, Player] = {}
        # id -> (número de la última ronda que jugó, su puesto en ESA ronda). El puesto
        # es lo que hace rotar la cola: ver queue().
        self._turn: dict[int, tuple[int, int]] = {}
        self._round_no = 0
        # Discos de la ronda actual (vivos y eliminados). Vacío fuera de ronda.
        self.discs: dict[int, Disc] = {}
        self.scores: dict[int, int] = {}
        # El anfitrión pidió iniciar. Lo consume la sala de espera (Waiting).
        self._start_requested = False

    # ---------------- registro ----------------

    def add_player(self, player_id: int, name: str) -> Player:
        colors = self._cfg.PLAYER_COLORS
        player = Player(player_id, name, colors[player_id % len(colors)])
        self.players[player_id] = player
        return player

    def remove_player(self, player_id: int) -> None:
        # pop con valor por defecto: un LeaveCommand repetido no falla.
        self.players.pop(player_id, None)
        self._turn.pop(player_id, None)
        self.discs.pop(player_id, None)   # RF-04: su disco desaparece
        self.scores.pop(player_id, None)

    def host(self) -> int | None:
        """Anfitrión de la sala: el que lleva más tiempo conectado (el primero
        en `players`, que nunca se reordena). Se DEDUCE, no se guarda: si el
        anfitrión se va, el siguiente lo es sin ningún código de traspaso."""
        return next(iter(self.players), None)

    def request_start(self, player_id: int) -> None:
        """Solo el anfitrión puede iniciar: la orden de otro se ignora."""
        if player_id == self.host():
            self._start_requested = True

    def take_start_request(self) -> bool:
        requested, self._start_requested = self._start_requested, False
        return requested

    def queue(self) -> list[int]:
        """Orden de turno. Se ordena por (última ronda jugada, puesto que tuvo en ella):
          * quienes NUNCA han jugado van primero, por orden de llegada;
          * luego los que llevan más rondas sin jugar;
          * y a igualdad, en el MISMO orden en que estaban en la cola de esa ronda.
        Lo último es lo que rota. Desempatar por orden de llegada era un error: la
        llegada no cambia nunca, así que los primeros ganaban todos los empates y
        jugaban TODAS las rondas (con 14 jugadores y 12 plazas, el anfitrión jugó
        42 de 42 y los 11 a 14 solo la mitad). Con el puesto de la ronda anterior el
        que se quedó fuera pasa adelante y los que jugaron dan la vuelta, como una
        cola circular."""
        arrival = {pid: i for i, pid in enumerate(self.players)}
        return sorted(self.players, key=lambda pid: self._turn.get(pid, (0, arrival[pid])))

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
        """Coloca a los primeros de la cola y anota que jugaron esta ronda: así
        pasan al final de la cola y los que esperan entran después (RF-03)."""
        chosen = self.queue()[: self._cfg.MAX_ACTIVE_PLAYERS]
        self._round_no += 1
        points = self.arena.spawn_points(len(chosen), self._cfg.SPAWN_RING_FRACTION)
        self.discs = {}
        for slot, (player_id, pos) in enumerate(zip(chosen, points)):
            # Cada disco nace mirando al centro: una embestida sin moverse
            # va hacia la pelea, no hacia el vacío.
            self.discs[player_id] = Disc(player_id, pos, facing=(-pos).normalized())
            self._turn[player_id] = (self._round_no, slot)

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
