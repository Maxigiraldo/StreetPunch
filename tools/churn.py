"""Churn: abrir y cerrar conexiones sin parar, de todas las formas posibles.

Uso (con el servidor corriendo):
    .venv/bin/python tools/churn.py [--workers 20] [--seconds 30] [--port 8080] [--mute]

Cada trabajador repite: elige al azar CÓMO conectarse y CÓMO irse. Es la prueba
de resistencia de recursos: al terminar (y tras esperar a que el servidor
limpie) los descriptores, las conexiones activas y los CLOSE_WAIT deben volver
al nivel de antes. Un `+N` en cualquiera de ellos es una fuga.

Formas de conexión (registro, no `if/elif`):
  clean      hello, lee un estado y cierra con close 1000.
  abrupt     hello y corte brusco del TCP (RST), como un cable desenchufado.
  no_hello   abre el WebSocket y se va sin saludar.
  bad_json   manda basura: espera error + close 1008.
  bad_frame  manda una trama sin máscara: espera close 1002.
  http       pide una página y cierra.
  mute (opcional con --mute)  abre el TCP y calla: el servidor debe cortarlo
             solo, a los HANDSHAKE_TIMEOUT_S; tarda 5 s por intento.
"""

import argparse
import asyncio
import random
import time
from collections import Counter

from procinfo import TCP_CLOSE_WAIT, count_tcp, fetch_metrics, find_server_pid, read_proc
from wslite import HOST, OP_CLOSE, OP_TEXT, HandshakeFailed, WsConn, connect

SETTLE_S = 2.5            # margen para que el servidor procese los cierres antes de medir
EXPECT_TIMEOUT_S = 3.0
MUTE_CUT_LIMIT_S = 8.0    # el servidor debe cortar a un mudo antes de esto (HANDSHAKE_TIMEOUT_S = 5)


async def wait_close_code(conn: WsConn) -> int | None:
    while True:
        opcode, payload = await asyncio.wait_for(conn.recv(), EXPECT_TIMEOUT_S)
        if opcode == OP_CLOSE:
            return WsConn.close_code(payload)


async def clean(port: int) -> str:
    conn = await connect(port)
    await conn.send_json({"t": "hello", "name": "churn"})
    while (await asyncio.wait_for(conn.recv(), EXPECT_TIMEOUT_S))[1][:12] != b'{"t":"state"':
        pass
    await conn.send(OP_CLOSE, b"\x03\xe8")
    await conn.close()
    return "ok"


async def abrupt(port: int) -> str:
    conn = await connect(port)
    await conn.send_json({"t": "hello", "name": "churn"})
    await asyncio.wait_for(conn.recv(), EXPECT_TIMEOUT_S)
    conn.abort()
    return "ok"


async def no_hello(port: int) -> str:
    conn = await connect(port)
    await conn.close()
    return "ok"


async def bad_json(port: int) -> str:
    conn = await connect(port)
    await conn.send(OP_TEXT, b"{roto")
    code = await wait_close_code(conn)
    await conn.close()
    return "ok" if code == 1008 else f"código inesperado {code}"


async def bad_frame(port: int) -> str:
    conn = await connect(port)
    conn.writer.write(bytes([0x81, 0x02]) + b"{}")      # sin bit de máscara: prohibido en el cliente
    await conn.writer.drain()
    code = await wait_close_code(conn)
    await conn.close()
    return "ok" if code == 1002 else f"código inesperado {code}"


async def http_page(port: int) -> str:
    reader, writer = await asyncio.open_connection(HOST, port)
    writer.write(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    await writer.drain()
    head = await asyncio.wait_for(reader.read(64), EXPECT_TIMEOUT_S)
    writer.close()
    return "ok" if head.startswith(b"HTTP/1.1 200") else f"respuesta inesperada {head[:20]!r}"


async def mute(port: int) -> str:
    reader, writer = await asyncio.open_connection(HOST, port)
    started = time.monotonic()
    data = await asyncio.wait_for(reader.read(), MUTE_CUT_LIMIT_S)      # EOF = el servidor lo cortó
    writer.close()
    return "ok" if not data and time.monotonic() - started < MUTE_CUT_LIMIT_S else "no lo cortó"


SCENARIOS = {"clean": clean, "abrupt": abrupt, "no_hello": no_hello, "bad_json": bad_json,
             "bad_frame": bad_frame, "http": http_page}


async def worker(port: int, deadline: float, names: list[str], rng: random.Random,
                 results: Counter, problems: Counter) -> None:
    while time.monotonic() < deadline:
        name = rng.choice(names)
        try:
            outcome = await SCENARIOS[name](port)
        except (OSError, HandshakeFailed, asyncio.TimeoutError, asyncio.IncompleteReadError) as err:
            outcome = f"{type(err).__name__}"
        results[name] += 1
        if outcome != "ok":
            problems[f"{name}: {outcome}"] += 1


def snapshot(port: int, pid: int | None) -> dict:
    snap = {"conexiones activas": fetch_metrics(port)[0]["connections_active"],
            "CLOSE_WAIT": count_tcp(port, TCP_CLOSE_WAIT)}
    if pid is not None:
        snap["descriptores"] = read_proc(pid)["fds"]
    return snap


async def main() -> None:
    parser = argparse.ArgumentParser(description="Churn de conexiones para StreetPunch")
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--mute", action="store_true", help="incluye clientes mudos (tardan 5 s cada uno)")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    if args.mute:
        SCENARIOS["mute"] = mute

    pid = find_server_pid(args.port)
    before = snapshot(args.port, pid)
    print(f"{args.workers} trabajadores, {args.seconds:.0f} s, escenarios: {', '.join(SCENARIOS)}")
    print("antes:  ", before)
    rng, results, problems = random.Random(args.seed), Counter(), Counter()
    deadline = time.monotonic() + args.seconds
    await asyncio.gather(*(worker(args.port, deadline, list(SCENARIOS), random.Random(rng.random()),
                                  results, problems) for _ in range(args.workers)))
    await asyncio.sleep(SETTLE_S)
    after = snapshot(args.port, pid)

    total = sum(results.values())
    print(f"\n{total} conexiones en {args.seconds:.0f} s ({total / args.seconds:.0f}/s): {dict(results)}")
    print("problemas de comportamiento:", dict(problems) or "ninguno")
    print("después:", after)
    leaks = {k: after[k] - before[k] for k in before if after[k] != before[k]}
    metrics = fetch_metrics(args.port)[0]
    print(f"  timeouts de saludo={metrics['handshake_timeouts']}  rechazadas={metrics['connections_rejected']}  "
          f"tick p99={metrics['tick_ms']['p99']} ms  overruns={metrics['tick_overruns']}")
    if leaks or problems:
        print(f"\nFALLO: diferencias {leaks or 'ninguna'}; problemas {dict(problems) or 'ninguno'}")
        raise SystemExit(1)
    print("\nOK: nada se filtró (descriptores, conexiones y CLOSE_WAIT como al inicio).")


if __name__ == "__main__":
    asyncio.run(main())
