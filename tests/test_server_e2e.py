"""Extremo a extremo con sockets reales en localhost: listener, saludo,
reader, writer, buzón y tick funcionando juntos."""

import asyncio
import json
import struct

from helpers_ws import WsClient, running_server

from server.adapters.http_handshake import StaticFile
from server.config import Config

CLOSE = 0x8


async def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "no se cumplió a tiempo"
        await asyncio.sleep(0.01)


async def expect_close(client: WsClient, code: int, error: str | None = None) -> None:
    if error is not None:
        assert await client.recv_json() == {"t": "error", "code": error}
    opcode, payload = await client.recv_frame()
    assert opcode == CLOSE
    assert struct.unpack("!H", payload[:2])[0] == code
    assert await client.reader.read() == b""     # y el servidor cerró el TCP


def test_dos_jugadores_reciben_estados_a_30hz():
    """Criterio de P4: un cliente de terminal recibe estados a 30 Hz."""
    async def go():
        async with running_server() as app:
            a, b = await WsClient.connect(app.listener.port), await WsClient.connect(app.listener.port)
            a.send_json({"t": "hello", "name": "Ana"})
            b.send_json({"t": "hello", "name": "Beto"})
            welcome = await a.recv_json()
            assert welcome["t"] == "welcome" and welcome["color"].startswith("#")

            # Contar durante 1 s una vez que la ronda empezó (los dos juegan).
            loop = asyncio.get_running_loop()
            while (await a.recv_state())["phase"] == "waiting":
                pass
            ticks, end = [], loop.time() + 1.0
            while loop.time() < end:
                msg = await a.recv_state()
                ticks.append(msg["tick"])
            assert ticks == sorted(ticks) and len(set(ticks)) == len(ticks)
            assert 25 <= len(ticks) <= 33, len(ticks)
            await a.close()
            await b.close()
    asyncio.run(go())


def test_input_mueve_el_disco_en_el_servidor():
    async def go():
        async with running_server() as app:
            a, b = await WsClient.connect(app.listener.port), await WsClient.connect(app.listener.port)
            a.send_json({"t": "hello", "name": "Ana"})
            b.send_json({"t": "hello", "name": "Beto"})
            my_id = (await a.recv_json())["id"]
            while (await a.recv_state())["phase"] != "playing":
                pass
            start = app.game.world.disc(my_id).pos
            a.send_json({"t": "input", "dx": 0, "dy": 1})
            await asyncio.sleep(0.3)
            assert app.game.world.disc(my_id).pos.y > start.y
            await a.close()
            await b.close()
    asyncio.run(go())


def test_trama_sin_mascara_cierra_1002():
    async def go():
        async with running_server() as app:
            c = await WsClient.connect(app.listener.port)
            c.send_frame(0x1, b'{"t":"hello","name":"x"}', masked=False)
            await expect_close(c, 1002)
    asyncio.run(go())


def test_mensaje_antes_de_hello_error_y_1008():
    async def go():
        async with running_server() as app:
            c = await WsClient.connect(app.listener.port)
            c.send_json({"t": "input", "dx": 1, "dy": 0})
            await expect_close(c, 1008, "not_hello")
    asyncio.run(go())


def test_json_malformado_error_y_1008():
    async def go():
        async with running_server() as app:
            c = await WsClient.connect(app.listener.port)
            c.send_json({"t": "hello", "name": "Ana"})
            assert (await c.recv_json())["t"] == "welcome"
            c.send_frame(0x1, b"{roto")
            # Pueden llegar estados antes del error: se saltan.
            while (msg := await c.recv_frame())[0] == 0x1 and json.loads(msg[1])["t"] in ("state", "roster"):
                pass
            assert json.loads(msg[1]) == {"t": "error", "code": "bad_message"}
            opcode, payload = await c.recv_frame()
            assert opcode == CLOSE and struct.unpack("!H", payload[:2])[0] == 1008
    asyncio.run(go())


def test_ping_recibe_pong_con_el_mismo_payload():
    async def go():
        async with running_server() as app:
            c = await WsClient.connect(app.listener.port)
            c.send_frame(0x9, b"hola")
            assert await c.recv_frame() == (0xA, b"hola")
            await c.close()
    asyncio.run(go())


def test_close_del_cliente_se_responde_y_libera_todo():
    async def go():
        async with running_server() as app:
            c = await WsClient.connect(app.listener.port)
            c.send_json({"t": "hello", "name": "Ana"})
            await c.recv_json()
            await wait_until(lambda: len(app.game.world.players) == 1)
            c.send_frame(CLOSE, struct.pack("!H", 1000))
            while (frame := await c.recv_frame())[0] != CLOSE:
                pass
            assert await c.reader.read() == b""
            # Disconnect ejecutado por el tick y cupo devuelto.
            await wait_until(lambda: not app.game.world.players)
            await wait_until(lambda: app.metrics.connections_active == 0)
    asyncio.run(go())


def test_desconexion_abrupta_tambien_libera():
    async def go():
        async with running_server() as app:
            c = await WsClient.connect(app.listener.port)
            c.send_json({"t": "hello", "name": "Ana"})
            await c.recv_json()
            c.writer.transport.abort()           # RST, sin close de WebSocket
            await wait_until(lambda: not app.game.world.players)
            await wait_until(lambda: app.metrics.connections_active == 0)
    asyncio.run(go())


def test_tope_de_conexiones_rechaza_sin_caerse_y_libera_cupo():
    async def go():
        async with running_server(Config(PORT=0, MAX_CONNECTIONS=2, MAX_ACTIVE_PLAYERS=2)) as app:
            port = app.listener.port
            a, b = await WsClient.connect(port), await WsClient.connect(port)
            r, w = await asyncio.open_connection("127.0.0.1", port)
            assert await r.read() == b""         # la tercera se cierra de inmediato
            w.close()
            assert app.metrics.connections_rejected == 1
            await a.close()
            await wait_until(lambda: app.metrics.connections_active == 1)
            c = await WsClient.connect(port)     # el cupo liberado se puede reusar
            await c.close()
            await b.close()
    asyncio.run(go())


def test_get_raiz_sirve_el_cliente():
    async def go():
        static = {"/": StaticFile(b"<h1>StreetPunch</h1>", "text/html; charset=utf-8")}
        async with running_server(static=static) as app:
            r, w = await asyncio.open_connection("127.0.0.1", app.listener.port)
            w.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            response = await r.read()
            assert response.startswith(b"HTTP/1.1 200 OK") and response.endswith(b"<h1>StreetPunch</h1>")
            w.close()
    asyncio.run(go())


def test_roster_por_el_cable_welcome_luego_roster_luego_state():
    async def go():
        async with running_server() as app:
            a = await WsClient.connect(app.listener.port)
            a.send_json({"t": "hello", "name": "Ana"})
            kinds = [(await a.recv_json())["t"] for _ in range(3)]
            assert kinds == ["welcome", "roster", "state"]

            b = await WsClient.connect(app.listener.port)
            b.send_json({"t": "hello", "name": "Beto"})
            # El nuevo recibe la lista completa; el que ya estaba, la lista ampliada.
            for client in (b, a):
                while (msg := await client.recv_json())["t"] != "roster" or len(msg["players"]) < 2:
                    pass
                assert sorted(p["name"] for p in msg["players"]) == ["Ana", "Beto"]
                assert all(p["color"].startswith("#") for p in msg["players"])
            await a.close()
            await b.close()
    asyncio.run(go())
