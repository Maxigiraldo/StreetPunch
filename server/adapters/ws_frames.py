"""Framing WebSocket (RFC 6455 §5) implementado a mano.

Formato de una trama (los bits más significativos primero):

     0               1               2               3
    +-+-+-+-+-------+-+-------------+-------------------------------+
    |F|R|R|R| opcode|M| longitud(7) |  longitud extendida (16 o 64) |
    |I|S|S|S|  (4)  |A|             |   si longitud(7) = 126 o 127  |
    |N|V|V|V|       |S|             |                               |
    +-+-+-+-+-------+-+-------------+-------------------------------+
    | clave de máscara (4 bytes, solo si MASK = 1) |    payload ...  |
    +----------------------------------------------+-----------------+

TCP es un FLUJO de bytes, no de mensajes: un read() puede traer media trama,
una trama y media o diez tramas. Por eso FrameReader acumula en un búfer y solo
entrega una trama cuando tiene todos sus bytes.

Lo que NO soportamos (decisión del protocolo, ver docs/protocolo.md):
fragmentación (FIN=0 / opcode 0), tramas binarias y extensiones (bits RSV).
"""

import struct
from dataclasses import dataclass
from enum import IntEnum


class Opcode(IntEnum):
    CONTINUATION = 0x0
    TEXT = 0x1
    BINARY = 0x2
    CLOSE = 0x8
    PING = 0x9
    PONG = 0xA


class CloseCode(IntEnum):
    """Códigos de cierre (RFC 6455 §7.4.1). Son parte del protocolo, no
    valores ajustables: por eso viven aquí y no en config.py."""
    NORMAL = 1000
    GOING_AWAY = 1001          # el servidor se apaga
    PROTOCOL_ERROR = 1002      # framing inválido
    UNSUPPORTED_DATA = 1003    # trama binaria
    INVALID_PAYLOAD = 1007     # texto que no es UTF-8 válido (lo detecta el codec)
    POLICY_VIOLATION = 1008    # mensaje del juego inválido
    MESSAGE_TOO_BIG = 1009
    TRY_AGAIN_LATER = 1013     # servidor lleno


# Constantes de formato del RFC: posiciones de bits, no valores ajustables.
_FIN = 0x80
_RSV = 0x70
_OPCODE = 0x0F
_MASK = 0x80
_LEN7 = 0x7F
_LEN16_MARK = 126              # "la longitud real viene en los 2 bytes siguientes"
_LEN64_MARK = 127              # "... en los 8 bytes siguientes"
_MAX_CONTROL_PAYLOAD = 125     # §5.5: las tramas de control caben en la longitud de 7 bits
_MASK_KEY_LEN = 4

_CONTROL = {Opcode.CLOSE, Opcode.PING, Opcode.PONG}


@dataclass(frozen=True)
class Frame:
    opcode: Opcode
    payload: bytes


class ProtocolError(Exception):
    """Violación del protocolo: la conexión debe cerrarse con close_code."""

    def __init__(self, close_code: CloseCode, reason: str) -> None:
        super().__init__(reason)
        self.close_code = close_code
        self.reason = reason


class FrameReader:
    """Arma tramas a partir de pedazos de bytes de cualquier tamaño.

    Uso: después de cada read() del socket, feed(datos) y luego llamar a
    next_frame() HASTA que devuelva None (puede haber varias tramas listas).
    Si lo que llega viola el protocolo, next_frame() lanza ProtocolError.

    Memoria acotada: el búfer guarda como máximo lo que entregó el último
    read() más una trama incompleta. Una trama no puede pasar de max_payload,
    porque se rechaza en cuanto se conoce su longitud.
    """

    def __init__(self, max_payload: int) -> None:
        self._max_payload = max_payload
        self._buf = bytearray()

    def feed(self, data: bytes) -> None:
        self._buf += data

    def next_frame(self) -> Frame | None:
        buf = self._buf
        if len(buf) < 2:
            return None

        b0, b1 = buf[0], buf[1]
        opcode = self._check_opcode(b0)
        if not b1 & _MASK:
            # §5.1: el servidor DEBE cerrar si una trama del cliente no viene
            # enmascarada. La máscara existe para que proxies intermedios no
            # confundan el payload con HTTP (ataques de cache poisoning).
            raise ProtocolError(CloseCode.PROTOCOL_ERROR, "trama del cliente sin máscara")

        length = b1 & _LEN7
        offset = 2
        if opcode in _CONTROL and length > _MAX_CONTROL_PAYLOAD:
            raise ProtocolError(CloseCode.PROTOCOL_ERROR, "trama de control mayor a 125 bytes")
        if length == _LEN16_MARK:
            if len(buf) < offset + 2:
                return None
            (length,) = struct.unpack_from("!H", buf, offset)   # entero de 16 bits, big-endian
            offset += 2
        elif length == _LEN64_MARK:
            if len(buf) < offset + 8:
                return None
            (length,) = struct.unpack_from("!Q", buf, offset)   # entero de 64 bits, big-endian
            offset += 8
            if length >> 63:
                raise ProtocolError(CloseCode.PROTOCOL_ERROR, "bit más significativo de la longitud en 1")

        # Se revisa AQUÍ, apenas se conoce la longitud anunciada y antes de
        # esperar o reservar el payload. Si esperáramos a "tener todo", un
        # cliente que anuncie 2^62 bytes nos haría acumular sin límite.
        if length > self._max_payload:
            raise ProtocolError(CloseCode.MESSAGE_TOO_BIG, f"trama de {length} bytes")

        end = offset + _MASK_KEY_LEN + length
        if len(buf) < end:
            return None     # trama incompleta: el resto llegará en otro read()

        mask = bytes(buf[offset:offset + _MASK_KEY_LEN])
        payload = _unmask(bytes(buf[offset + _MASK_KEY_LEN:end]), mask)
        del buf[:end]       # consumir solo esta trama; lo que sigue puede ser la próxima

        if opcode == Opcode.CLOSE and len(payload) == 1:
            # §5.5.1: el cuerpo de un close es vacío o código (2 bytes) + motivo.
            raise ProtocolError(CloseCode.PROTOCOL_ERROR, "close con cuerpo de 1 byte")
        return Frame(opcode, payload)

    @staticmethod
    def _check_opcode(b0: int) -> Opcode:
        if b0 & _RSV:
            # Los bits RSV solo se usan si se negoció una extensión; no negociamos ninguna.
            raise ProtocolError(CloseCode.PROTOCOL_ERROR, "bits RSV activados")
        try:
            opcode = Opcode(b0 & _OPCODE)
        except ValueError:
            raise ProtocolError(CloseCode.PROTOCOL_ERROR, "opcode desconocido") from None
        if opcode == Opcode.BINARY:
            raise ProtocolError(CloseCode.UNSUPPORTED_DATA, "tramas binarias no soportadas")
        if opcode == Opcode.CONTINUATION or not b0 & _FIN:
            raise ProtocolError(CloseCode.PROTOCOL_ERROR, "fragmentación no soportada")
        return opcode


def _unmask(payload: bytes, mask: bytes) -> bytes:
    """XOR de cada byte con mask[i % 4] (§5.3).

    En vez de recorrer byte a byte en Python (lento), se repite la clave hasta
    el largo del payload y se hace un solo XOR entre dos enteros grandes.
    """
    n = len(payload)
    if n == 0:
        return b""
    key = (mask * (n // _MASK_KEY_LEN + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


class FrameWriter:
    """Codifica tramas del servidor. El servidor NUNCA enmascara (§5.1).

    Cada método devuelve la trama completa en un solo objeto bytes, para que
    el writer la escriba con UNA llamada a write() (AGENTS.md §4.4). Así dos
    tramas nunca pueden quedar entremezcladas en el socket.
    """

    @staticmethod
    def encode(opcode: Opcode, payload: bytes) -> bytes:
        n = len(payload)
        if opcode in _CONTROL and n > _MAX_CONTROL_PAYLOAD:
            raise ValueError("una trama de control no puede pasar de 125 bytes")
        b0 = _FIN | opcode
        if n < _LEN16_MARK:
            header = struct.pack("!BB", b0, n)
        elif n <= 0xFFFF:
            header = struct.pack("!BBH", b0, _LEN16_MARK, n)
        else:
            header = struct.pack("!BBQ", b0, _LEN64_MARK, n)
        return header + payload

    @classmethod
    def text(cls, data: bytes) -> bytes:
        """`data` ya viene codificado en UTF-8 (lo hace el codec una vez por tick)."""
        return cls.encode(Opcode.TEXT, data)

    @classmethod
    def ping(cls, payload: bytes = b"") -> bytes:
        return cls.encode(Opcode.PING, payload)

    @classmethod
    def pong(cls, payload: bytes) -> bytes:
        # §5.5.3: el pong debe llevar el mismo payload que el ping.
        return cls.encode(Opcode.PONG, payload)

    @classmethod
    def close(cls, code: CloseCode, reason: str = "") -> bytes:
        body = struct.pack("!H", code) + reason.encode("utf-8")
        return cls.encode(Opcode.CLOSE, body[:_MAX_CONTROL_PAYLOAD])
