"""Framing WebSocket: lecturas parciales, varias tramas juntas, longitudes de
7/16/64 bits, máscara obligatoria y tope de tamaño (AGENTS.md §7)."""

import random
import struct

import pytest

from server.adapters.ws_frames import (
    CloseCode, Frame, FrameReader, FrameWriter, Opcode, ProtocolError,
)
from server.config import Config

CFG = Config()
MASK = b"\x37\xfa\x21\x3d"


def client_frame(opcode: int, payload: bytes, *, fin: bool = True, masked: bool = True,
                 rsv: int = 0, mask: bytes = MASK) -> bytes:
    """Construye una trama como la mandaría un navegador.

    Se arma a mano y de forma independiente de FrameWriter: si ambos
    compartieran el código, un error en él pasaría inadvertido en las pruebas.
    """
    b0 = (0x80 if fin else 0) | rsv | opcode
    mask_bit = 0x80 if masked else 0
    n = len(payload)
    if n < 126:
        header = bytes([b0, mask_bit | n])
    elif n < 65536:
        header = bytes([b0, mask_bit | 126]) + struct.pack("!H", n)
    else:
        header = bytes([b0, mask_bit | 127]) + struct.pack("!Q", n)
    if not masked:
        return header + payload
    body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return header + mask + body


def drain(reader: FrameReader) -> list[Frame]:
    frames = []
    while (f := reader.next_frame()) is not None:
        frames.append(f)
    return frames


def expect_close(reader: FrameReader, code: CloseCode) -> None:
    with pytest.raises(ProtocolError) as exc:
        drain(reader)
    assert exc.value.close_code == code


# ---------------- lecturas parciales ----------------

def test_trama_simple():
    r = FrameReader(CFG.MAX_FRAME_BYTES)
    r.feed(client_frame(Opcode.TEXT, b'{"t":"hello"}'))
    assert drain(r) == [Frame(Opcode.TEXT, b'{"t":"hello"}')]


@pytest.mark.parametrize("size", [0, 5, 125, 126, 1000, CFG.MAX_FRAME_BYTES])
def test_trama_en_pedazos_de_1_byte(size):
    """Peor caso de TCP: cada read() trae un solo byte. No debe salir nada
    hasta el último byte, y entonces sale la trama exacta."""
    payload = bytes(i % 251 for i in range(size))
    data = client_frame(Opcode.TEXT, payload)
    r = FrameReader(CFG.MAX_FRAME_BYTES)
    for byte in data[:-1]:
        r.feed(bytes([byte]))
        assert r.next_frame() is None
    r.feed(data[-1:])
    assert drain(r) == [Frame(Opcode.TEXT, payload)]


def test_varias_tramas_en_un_mismo_bloque():
    data = (client_frame(Opcode.TEXT, b"uno")
            + client_frame(Opcode.PING, b"p")
            + client_frame(Opcode.TEXT, b"x" * 300)
            + client_frame(Opcode.CLOSE, struct.pack("!H", 1000)))
    r = FrameReader(CFG.MAX_FRAME_BYTES)
    r.feed(data)
    assert [f.opcode for f in drain(r)] == [Opcode.TEXT, Opcode.PING, Opcode.TEXT, Opcode.CLOSE]


def test_trama_y_media_deja_el_resto_para_despues():
    a, b = client_frame(Opcode.TEXT, b"primera"), client_frame(Opcode.TEXT, b"segunda")
    r = FrameReader(CFG.MAX_FRAME_BYTES)
    r.feed(a + b[:3])
    assert drain(r) == [Frame(Opcode.TEXT, b"primera")]
    r.feed(b[3:])
    assert drain(r) == [Frame(Opcode.TEXT, b"segunda")]


def test_cortes_al_azar_dan_siempre_las_mismas_tramas():
    rng = random.Random(42)
    payloads = [bytes(rng.randrange(256) for _ in range(rng.choice([0, 7, 125, 126, 900])))
                for _ in range(30)]
    stream = b"".join(client_frame(Opcode.TEXT, p, mask=bytes(rng.randrange(256) for _ in range(4)))
                      for p in payloads)
    for _ in range(20):
        r, got, i = FrameReader(CFG.MAX_FRAME_BYTES), [], 0
        while i < len(stream):
            step = rng.randint(1, 64)
            r.feed(stream[i:i + step])
            got += drain(r)
            i += step
        assert [f.payload for f in got] == payloads


# ---------------- longitudes ----------------

def test_longitud_126_usa_16_bits():
    data = client_frame(Opcode.TEXT, b"a" * 126)
    assert data[1] & 0x7F == 126
    r = FrameReader(CFG.MAX_FRAME_BYTES)
    r.feed(data)
    assert drain(r)[0].payload == b"a" * 126


def test_longitud_127_usa_64_bits():
    size = 70_000   # > 65535: obliga a la longitud de 64 bits
    data = client_frame(Opcode.TEXT, b"b" * size)
    assert data[1] & 0x7F == 127
    r = FrameReader(max_payload=size)
    r.feed(data)
    assert drain(r)[0].payload == b"b" * size


def test_longitud_64_con_bit_alto_se_rechaza():
    header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", 1 << 63)
    r = FrameReader(CFG.MAX_FRAME_BYTES)
    r.feed(header)
    expect_close(r, CloseCode.PROTOCOL_ERROR)


# ---------------- reglas que cierran la conexión ----------------

def test_trama_sin_mascara_cierra_1002():
    r = FrameReader(CFG.MAX_FRAME_BYTES)
    r.feed(client_frame(Opcode.TEXT, b"hola", masked=False))
    expect_close(r, CloseCode.PROTOCOL_ERROR)


def test_trama_de_5kb_se_rechaza_con_1009():
    r = FrameReader(CFG.MAX_FRAME_BYTES)
    r.feed(client_frame(Opcode.TEXT, b"z" * 5 * 1024))
    expect_close(r, CloseCode.MESSAGE_TOO_BIG)


def test_tope_se_aplica_antes_de_recibir_el_payload():
    """Solo llega la cabecera que anuncia 2^62 bytes: se rechaza YA, sin
    esperar (ni reservar) el payload."""
    r = FrameReader(CFG.MAX_FRAME_BYTES)
    r.feed(bytes([0x81, 0x80 | 127]) + struct.pack("!Q", 1 << 62))
    expect_close(r, CloseCode.MESSAGE_TOO_BIG)


def test_trama_binaria_cierra_1003():
    r = FrameReader(CFG.MAX_FRAME_BYTES)
    r.feed(client_frame(Opcode.BINARY, b"\x00\x01"))
    expect_close(r, CloseCode.UNSUPPORTED_DATA)


@pytest.mark.parametrize("data", [
    client_frame(Opcode.TEXT, b"parte", fin=False),           # fragmento inicial
    client_frame(Opcode.CONTINUATION, b"resto"),             # continuación
    client_frame(Opcode.TEXT, b"x", rsv=0x40),               # bit RSV1 sin extensión
    client_frame(0x3, b"x"),                                 # opcode reservado
    client_frame(Opcode.PING, b"p" * 126),                   # control > 125
    client_frame(Opcode.CLOSE, b"\x03"),                     # close de 1 byte
])
def test_violaciones_de_framing_cierran_1002(data):
    r = FrameReader(CFG.MAX_FRAME_BYTES)
    r.feed(data)
    expect_close(r, CloseCode.PROTOCOL_ERROR)


# ---------------- FrameWriter ----------------

@pytest.mark.parametrize("size, len_byte, header_len", [(5, 5, 2), (126, 126, 4), (70_000, 127, 10)])
def test_writer_elige_la_longitud_correcta(size, len_byte, header_len):
    data = FrameWriter.text(b"q" * size)
    assert data[0] == 0x81                 # FIN + texto
    assert data[1] == len_byte             # sin bit de máscara: el servidor no enmascara
    assert len(data) == header_len + size
    if len_byte == 126:
        assert struct.unpack("!H", data[2:4])[0] == size
    if len_byte == 127:
        assert struct.unpack("!Q", data[2:10])[0] == size


def test_writer_close_lleva_codigo():
    data = FrameWriter.close(CloseCode.GOING_AWAY, "apagado")
    assert data[0] == 0x88
    assert struct.unpack("!H", data[2:4])[0] == 1001
    assert data[4:] == b"apagado"


def test_writer_pong_repite_payload_del_ping():
    assert FrameWriter.pong(b"abc") == b"\x8a\x03abc"


def test_writer_rechaza_control_grande():
    with pytest.raises(ValueError):
        FrameWriter.ping(b"x" * 126)
