"""Demo de P8: congelación y diccionario modificado en el tick (rama demo/race-bug).

Uso, con el servidor (bueno o malo) corriendo en el puerto dado:
    .venv/bin/python tools/race_demo.py [puerto]

Arma UNA sala con dos jugadores (A y B) y un zombi que sigue vivo sin leer
(slow_client --keepalive, a cuentagotas, dentro de la MISMA sala y ronda),
más 20 trabajadores de churn que conectan y se van sin parar. A mide cuántos
estados por segundo recibe: en el servidor bueno son ~30 todo el rato; en el
malo caen a 0 durante ~10 s. Después se cuentan en el log del servidor los
"dictionary changed size during iteration" y los "fallo dentro del tick".
"""
import asyncio
import json
import subprocess
import sys
import time

sys.path.insert(0, "tools")
from wslite import OP_PING, OP_PONG, OP_TEXT, connect  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
PY = ".venv/bin/python"
SECONDS = 60


async def next_of(conn, kind: str) -> dict:
    """Siguiente mensaje JSON de ese tipo; contesta los ping mientras tanto."""
    while True:
        op, payload = await conn.recv()
        if op == OP_PING:
            await conn.send(OP_PONG, payload)
        elif op == OP_TEXT and payload.startswith(b'{"t":"%s"' % kind.encode()):
            return json.loads(payload)


async def main() -> None:
    a = await connect(PORT)
    await a.send_json({"t": "hello", "name": "A"})
    await a.send_json({"t": "create"})
    room = (await next_of(a, "room"))["id"]
    b = await connect(PORT)
    await b.send_json({"t": "hello", "name": "B"})
    await b.send_json({"t": "join", "room": room})

    async def drain_b() -> None:                  # B es un cliente sano: lee y contesta
        while True:
            op, payload = await b.recv()
            if op == OP_PING:
                await b.send(OP_PONG, payload)
    asyncio.create_task(drain_b())

    slow = subprocess.Popen([PY, "tools/slow_client.py", "--seconds", "70", "--port", str(PORT),
                             "--keepalive", "--mode", "trickle", "--rcvbuf", "1024", "--room", str(room)],
                            stdout=subprocess.DEVNULL)
    await asyncio.sleep(2)                        # que el zombi ya esté dentro de la sala
    await a.send_json({"t": "start"})
    churn = subprocess.Popen([PY, "tools/churn.py", "--workers", "20", "--seconds", "45", "--port", str(PORT)],
                             stdout=subprocess.DEVNULL)
    t0 = time.monotonic()
    last, n, report = t0, 0, []
    while time.monotonic() - t0 < SECONDS:
        try:
            op, payload = await asyncio.wait_for(a.recv(), 1)
        except asyncio.TimeoutError:
            op = None
        now = time.monotonic()
        if op == OP_PING:
            await a.send(OP_PONG, payload)
        elif op == OP_TEXT and payload.startswith(b'{"t":"state"'):
            n += 1
        if now - last >= 3:
            report.append(f"t={now - t0:4.0f}s  estados/s recibidos por A: {n / (now - last):5.1f}")
            n, last = 0, now
    print("\n".join(report))
    slow.terminate()
    churn.terminate()


asyncio.run(main())
