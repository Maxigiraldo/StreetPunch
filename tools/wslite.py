"""Cliente WebSocket mínimo con asyncio, solo para las herramientas de carga.

No importa nada de `server/`: se comporta como un cliente externo cualquiera.
Cubre lo justo de RFC 6455: saludo, tramas de texto/ping/pong/close, el
enmascarado obligatorio del cliente y longitudes de 7, 16 y 64 bits.
"""

import asyncio
import base64
import json
import os
import socket
import struct

OP_TEXT, OP_CLOSE, OP_PING, OP_PONG = 0x1, 0x8, 0x9, 0xA
HOST = "127.0.0.1"
READ_BYTES = 65536
CONNECT_TIMEOUT_S = 5.0
CODE_SIZE = 2          # un close lleva el código en los 2 primeros bytes del payload
LEN_16, LEN_64 = 126, 127


class HandshakeFailed(Exception):
    pass


class WsConn:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, leftover: bytes) -> None:
        self.reader, self.writer = reader, writer
        self._buf = bytearray(leftover)

    async def send(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)   # el cliente SIEMPRE enmascara (RFC 6455 §5.3)
        n = len(payload)
        if n < LEN_16:
            head = bytes([0x80 | opcode, 0x80 | n])
        else:
            head = bytes([0x80 | opcode, 0x80 | LEN_16]) + struct.pack("!H", n)
        self.writer.write(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        await self.writer.drain()

    async def send_json(self, obj: dict) -> None:
        await self.send(OP_TEXT, json.dumps(obj, separators=(",", ":")).encode())

    async def recv(self) -> tuple[int, bytes]:
        """Siguiente trama completa. Lanza IncompleteReadError si el TCP se cierra."""
        while True:
            frame = self._parse()
            if frame is not None:
                return frame
            data = await self.reader.read(READ_BYTES)
            if not data:
                raise asyncio.IncompleteReadError(bytes(self._buf), None)
            self._buf += data

    def _parse(self) -> tuple[int, bytes] | None:
        buf = self._buf
        if len(buf) < 2:
            return None
        length, offset = buf[1] & 0x7F, 2
        if length == LEN_16:
            if len(buf) < 4:
                return None
            length, offset = struct.unpack("!H", buf[2:4])[0], 4
        elif length == LEN_64:
            if len(buf) < 10:
                return None
            length, offset = struct.unpack("!Q", buf[2:10])[0], 10
        if len(buf) < offset + length:
            return None
        opcode, payload = buf[0] & 0x0F, bytes(buf[offset:offset + length])
        del buf[:offset + length]
        return opcode, payload

    @staticmethod
    def close_code(payload: bytes) -> int | None:
        return struct.unpack("!H", payload[:CODE_SIZE])[0] if len(payload) >= CODE_SIZE else None

    def abort(self) -> None:
        """Corte brusco (RST), como un cable desenchufado."""
        self.writer.transport.abort()

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except OSError:
            pass


async def connect(port: int, rcvbuf: int | None = None) -> WsConn:
    """Abre el TCP y hace el saludo HTTP de WebSocket.

    `rcvbuf` fija SO_RCVBUF ANTES de conectar: con un búfer de recepción
    diminuto, un cliente que no lee llena el camino al servidor muy rápido.
    """
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        if rcvbuf is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        await asyncio.wait_for(loop.sock_connect(sock, (HOST, port)), CONNECT_TIMEOUT_S)
        reader, writer = await asyncio.open_connection(sock=sock)
    except BaseException:
        sock.close()
        raise
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write((f"GET /ws HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                  f"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                  f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
    head = b""
    try:
        while b"\r\n\r\n" not in head:
            chunk = await asyncio.wait_for(reader.read(READ_BYTES), CONNECT_TIMEOUT_S)
            if not chunk:
                raise HandshakeFailed("el servidor cerró durante el saludo")
            head += chunk
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise HandshakeFailed(head.split(b"\r\n")[0].decode(errors="replace"))
    except BaseException:
        writer.transport.abort()
        raise
    return WsConn(reader, writer, head.split(b"\r\n\r\n", 1)[1])
