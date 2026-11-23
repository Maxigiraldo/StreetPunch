"""El vestíbulo (Hall): quién está conectado, qué salas hay y quién está en cuál.

Aquí viven las reglas de las salas, sin red:
  * cualquiera crea una sala y es su anfitrión;
  * se entra a una sala solo si NO está llena y NO está jugando (en espera);
  * una sala vacía desaparece;
  * un jugador está en el vestíbulo (sin sala) o en exactamente una sala.

REGLA DE CONCURRENCIA: igual que `World`, solo el tick toca este objeto. Los
rechazos (sala llena, etc.) no son excepciones: quedan en `rejections` y el
tick los entrega al cliente que los provocó.
"""

from dataclasses import dataclass

from server.config import Config
from server.domain.arena import Arena
from server.domain.room import Room
from server.domain.round_states import RoundContext, Waiting
from server.domain.world import World, color_for

# Códigos de rechazo (los de docs/protocolo.md).
ALREADY_IN_ROOM = "already_in_room"
NOT_IN_ROOM = "not_in_room"
NO_SUCH_ROOM = "no_room"
ROOM_FULL = "room_full"
ROOM_BUSY = "room_busy"
TOO_MANY_ROOMS = "too_many_rooms"


@dataclass(frozen=True)
class RoomInfo:
    """Lo que el vestíbulo muestra de cada sala (se compara entre ticks: frozen)."""
    id: int
    name: str
    players: int
    max_players: int
    phase: str
    host: str          # nombre del anfitrión
    open: bool         # se puede entrar ahora: hay sitio y está en espera


class Hall:
    def __init__(self, config: Config, arena: Arena, ctx: RoundContext) -> None:
        self._cfg = config
        self._arena = arena
        self._ctx = ctx
        self.rooms: dict[int, Room] = {}
        self._names: dict[int, str] = {}        # todos los conectados con hello hecho
        self._room_of: dict[int, int] = {}      # jugador -> sala (ausente = en el vestíbulo)
        self._next_room_id = 1
        self.rejections: list[tuple[int, str]] = []   # (jugador, código) pendientes de avisar

    # ---------------- conexión ----------------

    def connect(self, player_id: int, name: str) -> None:
        self._names[player_id] = name

    def disconnect(self, player_id: int) -> None:
        # Repetible: un Disconnect de quien nunca saludó no hace nada.
        self.leave_room(player_id, silent=True)
        self._names.pop(player_id, None)

    def is_connected(self, player_id: int) -> bool:
        return player_id in self._names

    def name_of(self, player_id: int) -> str:
        return self._names[player_id]

    def color_of(self, player_id: int) -> str:
        return color_for(self._cfg, player_id)

    def lobby_members(self) -> list[int]:
        return [pid for pid in self._names if pid not in self._room_of]

    # ---------------- salas ----------------

    def room_of(self, player_id: int) -> Room | None:
        room_id = self._room_of.get(player_id)
        return self.rooms.get(room_id) if room_id is not None else None

    def world_of(self, player_id: int) -> World | None:
        room = self.room_of(player_id)
        return room.world if room else None

    def create_room(self, player_id: int) -> None:
        if not self._can_act(player_id):
            return
        if player_id in self._room_of:
            return self._reject(player_id, ALREADY_IN_ROOM)
        if len(self.rooms) >= self._cfg.MAX_ROOMS:
            return self._reject(player_id, TOO_MANY_ROOMS)
        room_id = self._next_room_id
        self._next_room_id += 1
        room = Room(room_id, f"Sala {room_id}", World(self._cfg, self._arena), Waiting(self._ctx))
        self.rooms[room_id] = room
        self._enter(player_id, room)

    def join_room(self, player_id: int, room_id: int) -> None:
        if not self._can_act(player_id):
            return
        if player_id in self._room_of:
            return self._reject(player_id, ALREADY_IN_ROOM)
        room = self.rooms.get(room_id)
        if room is None:
            return self._reject(player_id, NO_SUCH_ROOM)
        if room.world.player_count() >= self._cfg.MAX_ROOM_PLAYERS:
            return self._reject(player_id, ROOM_FULL)
        if room.phase != Waiting.PHASE:
            return self._reject(player_id, ROOM_BUSY)
        self._enter(player_id, room)

    def leave_room(self, player_id: int, silent: bool = False) -> None:
        room = self.room_of(player_id)
        if room is None:
            if not silent and self.is_connected(player_id):
                self._reject(player_id, NOT_IN_ROOM)
            return
        room.world.remove_player(player_id)
        del self._room_of[player_id]
        if room.world.player_count() == 0:
            del self.rooms[room.id]            # sala vacía: desaparece

    def step(self) -> None:
        """Un tick de cada sala."""
        for room in list(self.rooms.values()):
            room.step()

    def summary(self) -> tuple[RoomInfo, ...]:
        """Lista de salas para el vestíbulo, ordenada por id."""
        cfg = self._cfg
        return tuple(
            RoomInfo(r.id, r.name, r.world.player_count(), cfg.MAX_ROOM_PLAYERS, r.phase,
                     self._names[r.world.host()],
                     r.world.player_count() < cfg.MAX_ROOM_PLAYERS and r.phase == Waiting.PHASE)
            for r in sorted(self.rooms.values(), key=lambda r: r.id))

    def drain_rejections(self) -> list[tuple[int, str]]:
        out, self.rejections = self.rejections, []
        return out

    # ---------------- internos ----------------

    def _can_act(self, player_id: int) -> bool:
        # Una orden de alguien que ya se fue (o aún no saludó) se ignora sin ruido.
        return self.is_connected(player_id)

    def _enter(self, player_id: int, room: Room) -> None:
        room.world.add_player(player_id, self._names[player_id])
        self._room_of[player_id] = room.id

    def _reject(self, player_id: int, code: str) -> None:
        self.rejections.append((player_id, code))
