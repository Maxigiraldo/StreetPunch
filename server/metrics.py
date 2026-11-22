"""Contadores del servidor (RNF-13). Se exponen en GET /metrics y los lee tools/monitor.py.

Varias tareas incrementan estos contadores. No necesitan lock: `x += 1` sin
un `await` en medio se ejecuta completo antes de que otra tarea pueda correr,
porque en asyncio hay un solo hilo y el cambio de tarea solo ocurre en un await.
"""

import math
import time
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
    handshake_timeouts: int = 0       # saludo HTTP o hello que no llegó a tiempo
    idle_timeouts: int = 0            # clientes sin tráfico (ni pong) en IDLE_TIMEOUT_S
    write_timeouts: int = 0           # clientes que no leen: drain() no terminó
    bytes_sent: int = 0               # bytes entregados al transporte (tramas completas)
    frames_sent: int = 0
    inputs_applied: int = 0           # entradas que el tick sí ejecutó
    started_at: float = field(default_factory=time.monotonic)
    tick_durations: deque[float] = field(init=False)
    rtt_samples: deque[float] = field(init=False)   # ping -> pong de clientes reales

    def __post_init__(self) -> None:
        self.tick_durations = deque(maxlen=self.window)
        # Los pings salen cada PING_INTERVAL_S por cliente: con 100 clientes
        # son ~10/s, así que esta ventana cubre ~1 min. Misma cota de memoria.
        self.rtt_samples = deque(maxlen=self.window)

    def record_rtt(self, seconds: float) -> None:
        self.rtt_samples.append(seconds)

    def record_tick(self, seconds: float) -> None:
        self.tick_durations.append(seconds)

    def snapshot(self) -> dict:
        """Foto de los contadores para /metrics. Sin await: es consistente.

        Los percentiles se calculan aquí (una vez por consulta, ~300 números)
        y no en cada tick, para no gastar presupuesto del tick en estadística.
        """
        ticks = sorted(self.tick_durations)
        rtts = sorted(self.rtt_samples)
        return {
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "connections_active": self.connections_active,
            "connections_total": self.connections_total,
            "connections_rejected": self.connections_rejected,
            "inputs_applied": self.inputs_applied,
            "inputs_dropped_queue": self.inputs_dropped_queue,
            "inputs_dropped_rate": self.inputs_dropped_rate,
            "mailbox_overwrites": self.mailbox_overwrites,
            "tick_overruns": self.tick_overruns,
            "handshake_timeouts": self.handshake_timeouts,
            "idle_timeouts": self.idle_timeouts,
            "write_timeouts": self.write_timeouts,
            "bytes_sent": self.bytes_sent,
            "frames_sent": self.frames_sent,
            "tick_ms": {
                "samples": len(ticks),
                "p50": _percentile_ms(ticks, 0.50),
                "p99": _percentile_ms(ticks, 0.99),
                "max": _percentile_ms(ticks, 1.0),
            },
            "rtt_ms": {
                "samples": len(rtts),
                "p50": _percentile_ms(rtts, 0.50),
                "p99": _percentile_ms(rtts, 0.99),
                "max": _percentile_ms(rtts, 1.0),
            },
        }


def _percentile_ms(sorted_seconds: list[float], fraction: float) -> float | None:
    """Percentil por rango más cercano; None si todavía no hay muestras."""
    if not sorted_seconds:
        return None
    index = max(0, math.ceil(fraction * len(sorted_seconds)) - 1)
    return round(sorted_seconds[index] * 1000, 3)
