"""Codec: registro de fábricas, validación de todo lo que manda el cliente y
formato de salida."""

import json

import pytest

from server.adapters.codec import JsonFrameEncoder, MessageError, game_codec, hello_codec
from server.adapters.ws_frames import CloseCode
from server.application.ports import DiscView, Snapshot, WelcomeInfo
from server.config import Config
from server.domain.commands import ChargeCommand, JoinCommand, MoveCommand

CFG = Config()
HELLO, GAME = hello_codec(CFG), game_codec()


def dec(codec, obj) -> object:
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return codec.decode(raw, 7)


def error_of(codec, obj) -> tuple[str, int]:
    with pytest.raises(MessageError) as exc:
        dec(codec, obj)
    return exc.value.code, exc.value.close_code


def test_hello_valido_quita_espacios():
    assert dec(HELLO, {"t": "hello", "name": "  Ana "}) == JoinCommand(7, "Ana")


@pytest.mark.parametrize("name", ["", "   ", "x" * 17, 5, None, "a\nb", "\x00"])
def test_nombres_invalidos(name):
    assert error_of(HELLO, {"t": "hello", "name": name}) == ("bad_name", CloseCode.POLICY_VIOLATION)


def test_antes_de_hello_solo_hello():
    assert error_of(HELLO, {"t": "input", "dx": 1, "dy": 0})[0] == "not_hello"


def test_segundo_hello_es_error():
    assert error_of(GAME, {"t": "hello", "name": "otra"})[0] == "not_hello"


def test_input_y_charge_validos():
    assert dec(GAME, {"t": "input", "dx": -1, "dy": 1}) == MoveCommand(7, -1, 1)
    assert dec(GAME, {"t": "charge", "on": False}) == ChargeCommand(7, False)


def test_campos_extra_se_ignoran_y_nunca_hay_posiciones():
    cmd = dec(GAME, {"t": "input", "dx": 0, "dy": 1, "x": 9999, "y": 9999, "seq": 3})
    assert cmd == MoveCommand(7, 0, 1)


@pytest.mark.parametrize("msg", [
    {"t": "input", "dx": 2, "dy": 0},
    {"t": "input", "dx": True, "dy": 0},      # bool es int en Python: debe rechazarse
    {"t": "input", "dx": 1.0, "dy": 0},
    {"t": "input", "dx": "1", "dy": 0},
    {"t": "input", "dy": 0},
    {"t": "charge", "on": 1},
    {"t": "charge"},
    {"t": "fly"},
    {"t": [1]},                               # tipo no hasheable como clave
    {"sin": "t"},
    [1, 2, 3],
    "texto",
])
def test_mensajes_malformados_bad_message(msg):
    assert error_of(GAME, msg) == ("bad_message", CloseCode.POLICY_VIOLATION)


@pytest.mark.parametrize("raw", [b"{no json", b"", b"NaN"])
def test_json_invalido(raw):
    assert error_of(GAME, raw)[0] == "bad_message"


def test_json_anidado_profundo_no_tumba_la_sesion():
    """4 KB de corchetes: json.loads lanza RecursionError, no ValueError."""
    bomb = b"[" * (CFG.MAX_FRAME_BYTES // 2) + b"]" * (CFG.MAX_FRAME_BYTES // 2)
    assert error_of(GAME, bomb)[0] == "bad_message"


def test_utf8_invalido_cierra_1007():
    assert error_of(GAME, b"\xff\xfe")[1] == CloseCode.INVALID_PAYLOAD


# ---------------- salida ----------------

def unframe(data: bytes) -> dict:
    """Las tramas del servidor no van enmascaradas; para leerlas con
    FrameReader (que exige máscara) se arma aquí a mano: 2 bytes de cabecera
    si el payload es < 126, 4 si no."""
    n = data[1] & 0x7F
    start = 2 if n < 126 else 4
    return json.loads(data[start:])


def test_estado_formato_del_protocolo():
    snap = Snapshot(12, "countdown", 2.5, None, {7: 3},
                    (DiscView(7, 10.04, -3.06, 0.333, True), DiscView(9, 0, 0, 0, False)))
    msg = unframe(JsonFrameEncoder(CFG).encode_state(snap))
    assert msg == {"t": "state", "tick": 12, "phase": "countdown", "ms": 2500, "winner": None,
                   "scores": {"7": 3}, "d": [[7, 10.0, -3.1, 0.33, 1], [9, 0, 0, 0, 0]]}


def test_estado_sin_plazo_ms_null():
    snap = Snapshot(1, "playing", None, None, {}, ())
    assert unframe(JsonFrameEncoder(CFG).encode_state(snap))["ms"] is None


def test_welcome_formato():
    w = WelcomeInfo(7, "#e4572e", "spectator", 300.0, 24.0)
    assert unframe(JsonFrameEncoder(CFG).encode_welcome(w)) == {
        "t": "welcome", "id": 7, "color": "#e4572e", "role": "spectator",
        "arena": {"radius": 300.0, "disc_radius": 24.0}}


def test_salida_es_trama_de_texto_sin_mascara():
    data = JsonFrameEncoder(CFG).encode_error("bad_name")
    assert data[0] == 0x81 and not data[1] & 0x80
