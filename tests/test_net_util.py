import asyncio

from server.adapters.net_util import abort_with_reset


def test_abort_with_reset_el_cliente_recibe_rst_y_no_un_cierre_limpio():
    async def go():
        served = asyncio.Event()
        box = {}

        async def handler(reader, writer):
            box["writer"] = writer
            served.set()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await served.wait()
        abort_with_reset(box["writer"])
        try:
            await asyncio.wait_for(reader.read(), 2)
            outcome = "cierre limpio"
        except ConnectionResetError:
            outcome = "RST"
        writer.close()
        server.close()
        assert outcome == "RST"        # con transport.abort() a secas llegaría un FIN: read() devolvería b""
    asyncio.run(go())
