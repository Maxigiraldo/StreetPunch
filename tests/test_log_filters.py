import logging

from server.log_filters import RateLimitFilter

NEEDLE = "out of system resource"


def record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("asyncio", logging.ERROR, __file__, 1, msg, None, None)


def test_deja_pasar_uno_por_intervalo_y_cuenta_los_suprimidos():
    now = [0.0]
    flt = RateLimitFilter(NEEDLE, 5.0, clock=lambda: now[0])
    assert flt.filter(record(f"socket.accept() {NEEDLE}"))            # el primero pasa
    results = []
    for _ in range(100):                                               # una avalancha en el mismo instante
        results.append(flt.filter(record(f"socket.accept() {NEEDLE}")))
    assert not any(results)
    now[0] = 5.0
    again = record(f"socket.accept() {NEEDLE}")
    assert flt.filter(again) and "+100 iguales suprimidos" in again.getMessage()


def test_no_toca_otros_mensajes():
    flt = RateLimitFilter(NEEDLE, 5.0, clock=lambda: 0.0)
    assert all(flt.filter(record("error atendiendo una conexión")) for _ in range(50))
