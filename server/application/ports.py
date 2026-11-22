"""Puertos (interfaces) de la capa de aplicación.

GameSession solo conoce estas interfaces, nunca un socket ni un WebSocket. Los
adaptadores las implementan, y las pruebas usan implementaciones falsas.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class DiscView:
    id: int
    x: float
    y: float
    charge: float      # fracción 0..1
    alive: bool


@dataclass(frozen=True)
class Snapshot:
    """Foto inmutable del juego en un tick: lo que dice el mensaje `state`."""
    tick: int
    phase: str
    remaining_s: float | None
    winner: int | None
    scores: dict[int, int]
    discs: tuple[DiscView, ...]


@dataclass(frozen=True)
class WelcomeInfo:
    id: int
    color: str
    role: str
    arena_radius: float
    disc_radius: float
    max_players: int           # jugadores por ronda: el cliente calcula en qué turno le toca
    dash_cooldown_s: float     # para mostrar el enfriamiento de la embestida


@dataclass(frozen=True)
class RosterEntry:
    id: int
    name: str
    color: str


class SnapshotObserver(ABC):
    """Quien recibe lo que publica el tick (patrón Observer).

    REGLA: las implementaciones NO pueden hacer await ni E/S aquí; solo dejar
    los bytes y avisar. El tick llama a estos métodos para cada cliente y no
    puede esperar a ninguno (head-of-line blocking).
    """

    @abstractmethod
    def on_snapshot(self, data: bytes) -> None: ...

    @abstractmethod
    def on_welcome(self, data: bytes) -> None: ...

    @abstractmethod
    def on_roster(self, data: bytes) -> None: ...


class SnapshotEncoder(ABC):
    """Convierte la foto a bytes listos para el cable. GameSession no sabe si
    es JSON ni si va en tramas WebSocket."""

    @abstractmethod
    def encode_state(self, snapshot: Snapshot) -> bytes: ...

    @abstractmethod
    def encode_welcome(self, welcome: WelcomeInfo) -> bytes: ...

    @abstractmethod
    def encode_roster(self, entries: tuple[RosterEntry, ...]) -> bytes: ...
