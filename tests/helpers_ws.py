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

    async def recv_type(self, kind: str) -> dict:
        """Siguiente mensaje de ese tipo, saltando los demás (welcome, rooms, roster...)."""
        while (msg := await self.recv_json())["t"] != kind:
            pass
        return msg

    async def recv_state(self) -> dict:
        """Siguiente `state`, saltando welcome, rooms, room y roster (que llegan antes)."""
        return await self.recv_type("state")

    async def hello(self, name: str) -> dict:
        self.send_json({"t": "hello", "name": name})
        return await self.recv_type("welcome")

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except ConnectionError:
            pass


async def make_room(port: int, players: int, names: tuple[str, ...] = ()) -> list["WsClient"]:
    """Un anfitrión que crea una sala y `players - 1` que se unen; el primero devuelto
    es el anfitrión. Cuando regresa, todos ya están en la sala."""
    clients: list[WsClient] = []
    host = await WsClient.connect(port)
    await host.hello(names[0] if names else "host")
    host.send_json({"t": "create"})
    room_id = (await host.recv_type("room"))["id"]
    clients.append(host)
    for i in range(1, players):
        guest = await WsClient.connect(port)
        await guest.hello(names[i] if len(names) > i else f"j{i}")
        guest.send_json({"t": "join", "room": room_id})
        assert (await guest.recv_type("room"))["id"] == room_id
        clients.append(guest)
    return clients


@asynccontextmanager
async def running_server(config: Config | None = None, static=None):
    """Servidor real en un puerto libre (PORT=0: el kernel elige uno)."""
    app = build(config or Config(PORT=0), static or {})
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
