"""Saludo HTTP: Sec-WebSocket-Accept, validación del upgrade, archivos
estáticos y lectura incremental de la cabecera."""

import asyncio
import base64

import pytest

from server.adapters.http_handshake import (
    HttpError, HttpHandshake, StaticFile, accept_key, parse_request, read_head,
)
from server.config import Config

CFG = Config()
KEY = "dGhlIHNhbXBsZSBub25jZQ=="   # clave de ejemplo del RFC 6455 §1.3


def handshake() -> HttpHandshake:
    static = {"/": StaticFile(b"<html>hola</html>", "text/html; charset=utf-8")}
    return HttpHandshake(static, "/ws", CFG.MAX_HTTP_HEADER_BYTES, CFG.READ_CHUNK_BYTES)


def ws_request(**override: str) -> bytes:
    headers = {
        "Host": "localhost:8080",
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Key": KEY,
        "Sec-WebSocket-Version": "13",
    }
    headers.update(override)
    lines = ["GET /ws HTTP/1.1"] + [f"{k}: {v}" for k, v in headers.items() if v is not None]
    return "\r\n".join(lines).encode("ascii")


def status_of(raw: bytes) -> int:
    try:
        response, _ = handshake().respond(parse_request(raw))
    except HttpError as err:
        return err.status.value
    return int(response.split(b" ")[1])


def test_accept_key_ejemplo_del_rfc():
    # Valor publicado en RFC 6455 §1.3 para esa misma clave.
    assert accept_key(KEY) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_upgrade_valido_responde_101():
    response, upgraded = handshake().respond(parse_request(ws_request()))
    assert upgraded
    assert response.startswith(b"HTTP/1.1 101 Switching Protocols\r\n")
    assert b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n" in response
    assert response.endswith(b"\r\n\r\n")


def test_cabeceras_sin_distinguir_mayusculas_y_connection_con_varios_tokens():
    raw = ws_request(Upgrade="WebSocket", Connection="keep-alive, Upgrade").replace(
        b"Sec-WebSocket-Key", b"sec-websocket-key")
    assert status_of(raw) == 101


def test_get_raiz_sirve_el_cliente():
    response, upgraded = handshake().respond(parse_request(b"GET / HTTP/1.1\r\nHost: x"))
    assert not upgraded
    assert response.startswith(b"HTTP/1.1 200 OK")
    assert response.endswith(b"<html>hola</html>")


@pytest.mark.parametrize("raw, status", [
    (b"GET /../../etc/passwd HTTP/1.1\r\nHost: x", 404),     # fuera de la lista blanca
    (b"POST / HTTP/1.1\r\nHost: x", 405),
    (b"basura", 400),
    (b"GET / HTTP/1.1\r\nsin-dos-puntos", 400),
    (ws_request(**{"Sec-WebSocket-Version": "8"}), 426),
    (ws_request(**{"Sec-WebSocket-Key": None}), 400),
    (ws_request(**{"Sec-WebSocket-Key": base64.b64encode(b"corta").decode()}), 400),
    (ws_request(**{"Sec-WebSocket-Key": "no es base64!"}), 400),
    (ws_request(Upgrade=None), 400),
    (ws_request(Connection="keep-alive"), 400),
    (ws_request(Host=None), 400),
])
def test_peticiones_invalidas(raw, status):
    assert status_of(raw) == status


def test_426_indica_la_version_soportada():
    with pytest.raises(HttpError) as exc:
        handshake().respond(parse_request(ws_request(**{"Sec-WebSocket-Version": "8"})))
    assert exc.value.extra_headers == {"Sec-WebSocket-Version": "13"}


# ---------------- lectura incremental de la cabecera ----------------

async def read_with(data: bytes, read_chunk: int):
    """Simula el socket: todo `data` ya llegó; el servidor lo lee de a
    `read_chunk` bytes por read()."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return await read_head(reader, CFG.MAX_HTTP_HEADER_BYTES, read_chunk)


FIRST_FRAME = b"\x81\x85FRAME"


def test_cabecera_en_pedazos_de_1_byte():
    """Con lecturas de 1 byte, read_head se detiene justo al final de la
    cabecera: no consume nada de lo que sigue (queda en el stream)."""
    head, leftover = asyncio.run(read_with(ws_request() + b"\r\n\r\n" + FIRST_FRAME, 1))
    assert head == ws_request()
    assert leftover == b""


def test_sobrante_se_devuelve_con_lecturas_grandes():
    """El primer frame WebSocket puede venir pegado a la cabecera en el mismo
    read(): esos bytes deben devolverse para el FrameReader, no perderse."""
    _, leftover = asyncio.run(read_with(ws_request() + b"\r\n\r\n" + FIRST_FRAME, CFG.READ_CHUNK_BYTES))
    assert leftover == FIRST_FRAME


HUGE = b"GET / HTTP/1.1\r\nX: " + b"a" * (CFG.MAX_HTTP_HEADER_BYTES + 10) + b"\r\n\r\n"


@pytest.mark.parametrize("read_chunk", [1, len(HUGE)])
def test_cabecera_demasiado_grande_431(read_chunk):
    """Dos caminos distintos: de a 1 byte, el búfer pasa el tope antes de ver
    el fin; con un solo read(), la cabecera llega COMPLETA y aun así debe
    rechazarse por su tamaño."""
    with pytest.raises(HttpError) as exc:
        asyncio.run(read_with(HUGE, read_chunk))
    assert exc.value.status == 431


def test_cliente_cierra_a_mitad_del_saludo():
    with pytest.raises(ConnectionError):
        asyncio.run(read_with(b"GET / HTTP/1.1\r\nHo", 4))
