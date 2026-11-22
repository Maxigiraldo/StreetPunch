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

from server.domain.vector import Vec2
from server.domain.world import World


class Command(ABC):
    @abstractmethod
    def execute(self, world: World) -> None: ...


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
class MoveCommand(Command):
    player_id: int
    dx: int
    dy: int

    def execute(self, world: World) -> None:
        disc = world.disc(self.player_id)
        if disc is None:  # espectador o ya se fue: se ignora
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
