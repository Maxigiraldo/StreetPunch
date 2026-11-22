"""Cliente WebSocket mínimo para las pruebas de extremo a extremo.

Se escribe aparte del código del servidor (no reutiliza FrameWriter ni
FrameReader), para que un error compartido no pase inadvertido.
"""

import asyncio
import dataclasses
import base64
import json
import os
import struct
from contextlib import asynccontextmanager

from server.config import Config
from server.main import build


class WsClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer

    @classmethod
    async def connect(cls, port: int) -> "WsClient":
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write((
            "GET /ws HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        head = await reader.readuntil(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 101"), head
        return cls(reader, writer)

    def send_raw(self, data: bytes) -> None:
        self.writer.write(data)

    def send_frame(self, opcode: int, payload: bytes, masked: bool = True) -> None:
        mask = os.urandom(4)
        n = len(payload)
        bit = 0x80 if masked else 0
        if n < 126:
            header = bytes([0x80 | opcode, bit | n])
        else:
            header = bytes([0x80 | opcode, bit | 126]) + struct.pack("!H", n)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if masked else payload
        self.send_raw(header + (mask if masked else b"") + body)

    def send_json(self, obj: object) -> None:
        self.send_frame(0x1, json.dumps(obj).encode())

    async def recv_frame(self) -> tuple[int, bytes]:
        b0, b1 = await self.reader.readexactly(2)
        n = b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", await self.reader.readexactly(2))
        elif n == 127:
            (n,) = struct.unpack("!Q", await self.reader.readexactly(8))
        return b0 & 0x0F, await self.reader.readexactly(n)

    async def recv_json(self) -> dict:
        opcode, payload = await self.recv_frame()
        assert opcode == 0x1, (opcode, payload)
        return json.loads(payload)

    async def recv_state(self) -> dict:
        """Siguiente `state`, saltando welcome y roster (que llegan antes)."""
        while (msg := await self.recv_json())["t"] != "state":
            pass
        return msg

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except ConnectionError:
            pass


@asynccontextmanager
async def running_server(config: Config | None = None, static=None):
    """Servidor real en un puerto libre (PORT=0: el kernel elige uno)."""
    # Las pruebas no esperan la sala de espera de 5 s (LOBBY_WAIT_S): la de la
    # sala tiene sus propias pruebas en test_round_states.py.
    app = build(dataclasses.replace(config or Config(PORT=0), LOBBY_WAIT_S=0.0), static or {})
    task = asyncio.create_task(app.run())
    await asyncio.sleep(0)   # dejar que start_server arranque
    try:
        yield app
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
