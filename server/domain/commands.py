"""Comandos del juego (patrón Command).

Un comando separa CUÁNDO llega una entrada (en la tarea `reader` de un
cliente, en cualquier momento) de CUÁNDO se aplica (dentro del tick). Es la
base de la regla del único escritor: el reader solo crea el comando y lo deja
en la cola; únicamente el tick llama a execute().

Todos cumplen el mismo contrato (LSP): execute(world) no devuelve nada y no
lanza excepciones aunque el jugador ya no exista (pudo irse en este mismo tick).

El id del jugador viene de fuera: lo asigna la capa de aplicación al aceptar
la conexión. Un contador de ids no es estado del juego; el registro sí, y ese
solo cambia aquí dentro.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from typing import TYPE_CHECKING

from server.domain.vector import Vec2
from server.domain.world import World

if TYPE_CHECKING:   # solo para el tipo: evita un import circular
    from server.domain.hall import Hall


class Command(ABC):
    """Una orden DENTRO de una sala: `execute` recibe el mundo de esa sala."""
    player_id: int

    @abstractmethod
    def execute(self, world: World) -> None: ...

    def run(self, hall: "Hall") -> None:
        """Lo llama el tick. Busca la sala del jugador y ejecuta ahí; si está
        en el vestíbulo (o ya se fue) la orden se ignora.
        Es doble despacho: el tick no distingue tipos de comando."""
        world = hall.world_of(self.player_id)
        if world is not None:
            self.execute(world)


class HallCommand(ABC):
    """Una orden sobre las SALAS (crear, unirse, salir): recibe el vestíbulo.
    Viaja por la misma cola acotada que las entradas, así que un cliente que
    inunde con crear/salir queda limitado igual que uno que inunde con movimientos."""
    player_id: int

    @abstractmethod
    def execute(self, hall: "Hall") -> None: ...

    def run(self, hall: "Hall") -> None:
        self.execute(hall)


@dataclass(frozen=True)
class JoinCommand(Command):
    player_id: int
    name: str

    def execute(self, world: World) -> None:
        # Entra a la cola; jugará cuando empiece una ronda (nunca a mitad).
        world.add_player(self.player_id, self.name)


@dataclass(frozen=True)
class LeaveCommand(Command):
    player_id: int

    def execute(self, world: World) -> None:
        world.remove_player(self.player_id)


@dataclass(frozen=True)
class StartCommand(Command):
    """El anfitrión de la sala pide iniciar la ronda."""
    player_id: int

    def execute(self, world: World) -> None:
        world.request_start(self.player_id)


@dataclass(frozen=True)
class CreateRoomCommand(HallCommand):
    player_id: int

    def execute(self, hall: "Hall") -> None:
        hall.create_room(self.player_id)


@dataclass(frozen=True)
class JoinRoomCommand(HallCommand):
    player_id: int
    room_id: int

    def execute(self, hall: "Hall") -> None:
        hall.join_room(self.player_id, self.room_id)


@dataclass(frozen=True)
class LeaveRoomCommand(HallCommand):
    player_id: int

    def execute(self, hall: "Hall") -> None:
        hall.leave_room(self.player_id)


@dataclass(frozen=True)
class MoveCommand(Command):
    player_id: int
    dx: int
    dy: int

    def execute(self, world: World) -> None:
        disc = world.disc(self.player_id)
        if disc is None:  # no hay ronda en marcha o ya se fue: se ignora
            return
        disc.set_move(Vec2(self.dx, self.dy))


@dataclass(frozen=True)
class ChargeCommand(Command):
    player_id: int
    on: bool

    def execute(self, world: World) -> None:
        disc = world.disc(self.player_id)
        if disc is None:
            return
        # Dos acciones, elegidas por un booleano del mensaje. Es un `if` de
        # dato, no un `if` sobre tipos o fases.
        if self.on:
            disc.start_charge()
        else:
            disc.release_charge()
