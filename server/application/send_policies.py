"""Frecuencia de envío por rol (patrón Strategy).

GameSession elige la política con un registro {rol: política}, sin `if rol ==`.
Una política nueva (p. ej. "cada 2 ticks para conexiones lentas") es una
clase nueva; el tick no se toca.
"""

from abc import ABC, abstractmethod


class SendPolicy(ABC):
    @abstractmethod
    def should_send(self, tick: int) -> bool: ...


class EveryTick(SendPolicy):
    def should_send(self, tick: int) -> bool:
        return True


class EveryNthTick(SendPolicy):
    def __init__(self, n: int) -> None:
        self._n = n

    def should_send(self, tick: int) -> bool:
        return tick % self._n == 0
