"""Frecuencia de envío por FASE de la sala (patrón Strategy).

GameSession elige la política con un registro {fase: política}, sin
`if fase == ...`. Mientras se juega, 30 Hz; en espera, cuenta atrás y
resultados nada se mueve y basta una fracción. Una política nueva es una clase
nueva; el tick no se toca.
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
