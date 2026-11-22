"""Pruebas de Metrics.snapshot y de la ruta dinámica /metrics."""

from server.adapters.http_handshake import HttpHandshake, HttpRequest, StaticFile
from server.config import Config
from server.metrics import Metrics

CFG = Config()


def test_snapshot_sin_muestras_no_inventa_percentiles():
    snap = Metrics(300).snapshot()
    assert snap["tick_ms"] == {"samples": 0, "p50": None, "p99": None, "max": None}


def test_percentiles_por_rango_mas_cercano():
    m = Metrics(300)
    for ms in range(1, 101):          # 1..100 ms
        m.record_tick(ms / 1000)
    t = m.snapshot()["tick_ms"]
    assert (t["p50"], t["p99"], t["max"]) == (50.0, 99.0, 100.0)


def test_ventana_descarta_muestras_viejas():
    m = Metrics(3)
    for s in (0.9, 0.001, 0.002, 0.003):
        m.record_tick(s)
    assert m.snapshot()["tick_ms"]["max"] == 3.0   # el 0.9 ya salió de la ventana


def test_contadores_aparecen_en_el_snapshot():
    m = Metrics(10)
    m.bytes_sent, m.mailbox_overwrites, m.inputs_dropped_queue = 123, 4, 5
    snap = m.snapshot()
    assert (snap["bytes_sent"], snap["mailbox_overwrites"], snap["inputs_dropped_queue"]) == (123, 4, 5)


def test_ruta_dinamica_se_calcula_en_cada_peticion():
    calls = []

    def page() -> StaticFile:
        calls.append(1)
        return StaticFile(b'{"n":%d}' % len(calls), "application/json")

    hs = HttpHandshake({}, "/ws", CFG.MAX_HTTP_HEADER_BYTES, CFG.READ_CHUNK_BYTES, dynamic={"/metrics": page})
    req = HttpRequest("GET", "/metrics", "HTTP/1.1", {})
    first, _ = hs.respond(req)
    second, _ = hs.respond(req)
    assert first.endswith(b'{"n":1}') and second.endswith(b'{"n":2}')
    assert b"application/json" in first


def test_rtt_aparece_en_el_snapshot_y_la_ventana_es_acotada():
    m = Metrics(3)
    assert m.snapshot()["rtt_ms"]["p50"] is None
    for s in (0.9, 0.010, 0.020, 0.030):    # la ventana es 3: el 0.9 sale
        m.record_rtt(s)
    rtt = m.snapshot()["rtt_ms"]
    assert (rtt["samples"], rtt["p50"], rtt["max"]) == (3, 20.0, 30.0)


def test_contadores_de_timeouts_en_el_snapshot():
    m = Metrics(3)
    m.handshake_timeouts, m.idle_timeouts, m.write_timeouts = 1, 2, 3
    snap = m.snapshot()
    assert (snap["handshake_timeouts"], snap["idle_timeouts"], snap["write_timeouts"]) == (1, 2, 3)
