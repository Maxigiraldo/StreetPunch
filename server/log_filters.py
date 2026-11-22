"""Filtros de logging.

Por qué existe: con el servidor sin descriptores libres (EMFILE), asyncio
registra "socket.accept() out of system resource" con traceback en CADA
intento, y su bucle de aceptación (`for _ in range(backlog)`, sin `break`
tras el error) repite el intento hasta `backlog` veces por pasada. Medido con
`ulimit -n 64` y 120 clientes: 83 768 errores y 37 MB de log en 35 s. El
módulo `logging` escribe de forma SÍNCRONA: ese volumen frena el event loop
(y con él el tick de todos los jugadores) en cuanto el destino se pone lento.
"""

import logging
import time
from collections.abc import Callable


class RateLimitFilter(logging.Filter):
    """Deja pasar a lo sumo un registro cada `interval_s` entre los que
    contienen `needle`; los demás se descartan y se cuentan. Al volver a dejar
    pasar uno, el mensaje avisa cuántos se suprimieron. El resto de registros
    no se toca."""

    def __init__(self, needle: str, interval_s: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self._needle = needle
        self._interval_s = interval_s
        self._clock = clock
        self._last_pass: float | None = None
        self._suppressed = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if self._needle not in record.getMessage():
            return True
        now = self._clock()
        if self._last_pass is not None and now - self._last_pass < self._interval_s:
            self._suppressed += 1
            return False
        if self._suppressed:
            record.msg = f"{record.getMessage()} (+{self._suppressed} iguales suprimidos)"
            record.args = None
            self._suppressed = 0
        # Sin traceback tras el primero: ya está claro de qué se trata y lo
        # que pesa del mensaje es justo ese volcado.
        if self._last_pass is not None:
            record.exc_info = None
            record.exc_text = None
        self._last_pass = now
        return True
