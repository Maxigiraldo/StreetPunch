"""Comandos de membresía: entrar y salir de la sesión (conectarse y desconectarse).

Registran al jugador en el vestíbulo (no en una sala: eso lo hacen los comandos
de sala del dominio) y además (de)suscriben al observador. Como el tick es el
único que los ejecuta, el vestíbulo y la lista de observadores tienen un solo
escritor.

Viajan por una cola distinta a la de entradas, que NUNCA descarta: perder un
Disconnect dejaría un jugador fantasma y una ClientSession que no se libera
nunca (fuga de memoria). No crece sin límite, porque cada conexión aporta como
máximo un Connect y un Disconnect, y las conexiones están topadas por el
semáforo.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.application.ports import SnapshotObserver
from server.domain.commands import JoinCommand

if TYPE_CHECKING:   # solo para el tipo: evita un import circular en ejecución
    from server.application.game_session import GameSession


class MembershipCommand(ABC):
    @abstractmethod
    def execute(self, session: GameSession) -> None: ...


@dataclass(frozen=True)
class Connect(MembershipCommand):
    join: JoinCommand
    observer: SnapshotObserver

    def execute(self, session: GameSession) -> None:
        session.subscribe(self.join.player_id, self.join.name, self.observer)


@dataclass(frozen=True)
class Disconnect(MembershipCommand):
    player_id: int

    def execute(self, session: GameSession) -> None:
        session.unsubscribe(self.player_id)
