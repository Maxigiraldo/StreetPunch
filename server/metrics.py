"""Contadores del servidor (RNF-13). El reporte por segundo llega en P7.

Varias tareas incrementan estos contadores. No necesitan lock: `x += 1` sin
un `await` en medio se ejecuta completo antes de que otra tarea pueda correr,
porque en asyncio hay un solo hilo y el cambio de tarea solo ocurre en un await.
"""

from collections import deque
from dataclasses import dataclass, field


@dataclass
class Metrics:
    window: int
    connections_active: int = 0
    connections_total: int = 0
    connections_rejected: int = 0     # tope MAX_CONNECTIONS alcanzado
    inputs_dropped_queue: int = 0     # cola de entradas llena
    inputs_dropped_rate: int = 0      # más de MAX_INPUTS_PER_CLIENT_PER_TICK
    mailbox_overwrites: int = 0       # estado reemplazado sin enviarse (cliente lento)
    tick_overruns: int = 0            # ticks que tardaron más que su presupuesto
    tick_durations: deque[float] = field(init=False)

    def __post_init__(self) -> None:
        self.tick_durations = deque(maxlen=self.window)

    def record_tick(self, seconds: float) -> None:
        self.tick_durations.append(seconds)
