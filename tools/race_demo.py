"""Demo de P8 (rama demo/race-bug): congelación y diccionario modificado en el tick.

Uso, con el servidor MALO corriendo en el puerto dado:
    .venv/bin/python tools/race_demo.py [puerto]

Pone dos jugadores (A, B), un zombi que sigue vivo sin leer (slow_client
--keepalive, a cuentagotas) y 20 trabajadores de churn. A mide cuántos
estados por segundo recibe: en el servidor bueno son ~30 todo el rato; en el
malo caen a 0 durante ~10 s. Luego cuenta en el log del servidor los
"dictionary changed size during iteration" y los "fallo dentro del tick".
"""
import asyncio, json, subprocess, sys, time
sys.path.insert(0, "tools")
PORT = sys.argv[1] if len(sys.argv) > 1 else PORT
from wslite import connect, OP_TEXT, OP_PING, OP_PONG
async def main():
    a = await connect(int(PORT)); await a.send_json({"t": "hello", "name": "A"})
    b = await connect(int(PORT)); await b.send_json({"t": "hello", "name": "B"})
    async def drain_b():
        while True:
            op, pl = await b.recv()
            if op == OP_PING: await b.send(OP_PONG, pl)
    asyncio.create_task(drain_b())
    slow = subprocess.Popen([".venv/bin/python", "tools/slow_client.py", "--seconds", "60", "--port", PORT, "--keepalive", "--mode", "trickle", "--rcvbuf", "1024"], stdout=subprocess.DEVNULL)
    await asyncio.sleep(2)
    await a.send_json({"t": "start"})
    churn = subprocess.Popen([".venv/bin/python", "tools/churn.py", "--workers", "20", "--seconds", "45", "--port", PORT], stdout=subprocess.DEVNULL)
    t0 = time.monotonic(); n = 0; last = t0; out = []; slow_in = False
    while time.monotonic() - t0 < 60:
        try: op, pl = await asyncio.wait_for(a.recv(), 1)
        except asyncio.TimeoutError: op = None
        now = time.monotonic()
        if op == OP_PING: await a.send(OP_PONG, pl)
        elif op == OP_TEXT and pl.startswith(b'{"t":"state"'): n += 1
        if now - last >= 3:
            out.append(f"t={now-t0:4.0f}s  estados/s recibidos por A: {n/(now-last):5.1f}"); n = 0; last = now
    print("\n".join(out)); slow.terminate()
asyncio.run(main())
