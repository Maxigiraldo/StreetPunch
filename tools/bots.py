"""Bots: N jugadores simulados para cargar el servidor.

Uso (con el servidor corriendo):
    .venv/bin/python tools/bots.py [--count 50] [--duration 30] [--port 8080] [--ramp 0.02] [--host]

Cada bot hace lo que haría el navegador: saluda con `hello`, CONTESTA los ping
del servidor (si no, el servidor lo echa por inactividad a los 30 s), lee los
estados y cada tanto cambia de dirección o carga una embestida. Hace falta un anfitrión que inicie la ronda: tú desde el
navegador (entra PRIMERO) o `--host`, que hace al bot 0 anfitrión. Con más
bots que MAX_ACTIVE_PLAYERS (12) el resto juega de espectador; con más que
MAX_CONNECTIONS (100) el servidor responde `full` y cierra con 1013.

Al terminar imprime cuántos estados llegaron, cómo se cerró cada conexión y
cuántas fueron rechazadas. Mientras tanto, `tools/monitor.py` en otra terminal
muestra cómo lo lleva el servidor.
"""

import argparse
import asyncio
import random
import time
from collections import Counter

from wslite import OP_CLOSE, OP_PING, OP_PONG, OP_TEXT, HandshakeFailed, WsConn, connect

DIRECTIONS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
THINK_MIN_S, THINK_MAX_S = 0.3, 1.0     # cada cuánto cambia de idea (un humano pulsa teclas ~así)
CHARGE_CHANCE = 0.15                    # probabilidad de intentar una embestida en cada decisión
CHARGE_MIN_S, CHARGE_MAX_S = 0.3, 1.0   # cuánto mantiene la carga
PROGRESS_EVERY_S = 5.0
HOST_START_EVERY_S = 1.0               # cada cuánto el bot anfitrión pide iniciar
STATE_PREFIX = b'{"t":"state"'
ERROR_PREFIX = b'{"t":"error","code":"'


class Stats:
    def __init__(self) -> None:
        self.connected = 0
        self.connect_failed = 0
        self.states = 0
        self.errors: Counter[str] = Counter()     # códigos de error del servidor (full, timeout...)
        self.closes: Counter[str] = Counter()     # cómo terminó cada conexión

    def total_closed(self) -> int:
        return sum(self.closes.values())


async def behave(conn: WsConn, rng: random.Random, stop: asyncio.Event) -> None:
    """Decide intenciones al azar, igual que un jugador pulsando teclas."""
    while not stop.is_set():
        dx, dy = rng.choice(DIRECTIONS)
        await conn.send_json({"t": "input", "dx": dx, "dy": dy})
        if rng.random() < CHARGE_CHANCE:
            await conn.send_json({"t": "charge", "on": True})
            await asyncio.sleep(rng.uniform(CHARGE_MIN_S, CHARGE_MAX_S))
            await conn.send_json({"t": "charge", "on": False})
        await asyncio.sleep(rng.uniform(THINK_MIN_S, THINK_MAX_S))


async def host_loop(conn: WsConn) -> None:
    """Hace de anfitrión: pide iniciar cada segundo. Es inofensivo fuera de la
    sala (el servidor descarta la orden al terminar cada ronda) y así las rondas
    se encadenan solas durante una prueba de carga. Sin esto, con solo bots, la
    sala de espera esperaría para siempre."""
    while True:
        await conn.send_json({"t": "start"})
        await asyncio.sleep(HOST_START_EVERY_S)


async def listen(conn: WsConn, stats: Stats) -> str:
    """Lee hasta que la conexión termine. Devuelve cómo terminó."""
    while True:
        opcode, payload = await conn.recv()
        if opcode == OP_PING:
            await conn.send(OP_PONG, payload)          # sin esto el servidor nos echa a los 30 s
        elif opcode == OP_TEXT:
            if payload.startswith(STATE_PREFIX):
                stats.states += 1
            elif payload.startswith(ERROR_PREFIX):
                stats.errors[payload[len(ERROR_PREFIX):].split(b'"')[0].decode()] += 1
        elif opcode == OP_CLOSE:
            return f"close {WsConn.close_code(payload)}"


async def bot(index: int, port: int, stats: Stats, deadline: float, rng: random.Random,
              is_host: bool = False) -> None:
    try:
        conn = await connect(port)
    except (OSError, HandshakeFailed, asyncio.TimeoutError):
        stats.connect_failed += 1
        return
    stats.connected += 1
    stop = asyncio.Event()
    actor = None
    try:
        await conn.send_json({"t": "hello", "name": f"bot{index}"})
        actor = asyncio.create_task(host_loop(conn) if is_host else behave(conn, rng, stop))
        try:
            ended = await asyncio.wait_for(listen(conn, stats), max(0.1, deadline - time.monotonic()))
            stats.closes[ended] += 1                    # lo cerró el servidor
        except asyncio.TimeoutError:
            await conn.send(OP_CLOSE, b"\x03\xe8")      # se acabó el tiempo: cierre normal (1000)
            stats.closes["terminó la prueba"] += 1
    except (OSError, asyncio.IncompleteReadError):
        stats.closes["TCP cortado"] += 1
    finally:
        stop.set()
        if actor is not None:
            actor.cancel()
        await conn.close()


async def progress(stats: Stats, started: float) -> None:
    last_states, last_t = 0, started
    while True:
        await asyncio.sleep(PROGRESS_EVERY_S)
        now = time.monotonic()
        rate = (stats.states - last_states) / (now - last_t)
        last_states, last_t = stats.states, now
        print(f"  t={now - started:5.1f}s  conectados={stats.connected - stats.total_closed():>3}  "
              f"estados/s (todos los bots)={rate:7.0f}  rechazados={stats.errors['full']}  "
              f"cerrados={stats.total_closed()}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Bots de carga para StreetPunch")
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--duration", type=float, default=30.0, help="segundos")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--ramp", type=float, default=0.02,
                        help="segundos entre una conexión y la siguiente (0 = todas de golpe)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--host", action="store_true",
                        help="el bot 0 hace de anfitrión (pide iniciar cada segundo, no juega). "
                             "No lo uses si entras tú desde el navegador: el anfitrión eres tú.")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    stats = Stats()
    started = time.monotonic()
    deadline = started + args.duration
    print(f"{args.count} bots contra el puerto {args.port} durante {args.duration:.0f} s")
    reporter = asyncio.create_task(progress(stats, started))
    tasks = []
    for i in range(args.count):
        tasks.append(asyncio.create_task(bot(i, args.port, stats, deadline, random.Random(rng.random()), args.host and i == 0)))
        if args.ramp:
            await asyncio.sleep(args.ramp)
    await asyncio.gather(*tasks)
    reporter.cancel()

    elapsed = time.monotonic() - started
    print(f"\nResumen tras {elapsed:.1f} s")
    print(f"  conexiones logradas: {stats.connected}   fallidas al conectar: {stats.connect_failed}")
    print(f"  estados recibidos: {stats.states}  ({stats.states / elapsed:.0f}/s en total, "
          f"{stats.states / elapsed / max(1, stats.connected):.1f}/s por bot)")
    print(f"  errores del servidor: {dict(stats.errors) or 'ninguno'}")
    print(f"  cierres: {dict(stats.closes)}")


if __name__ == "__main__":
    asyncio.run(main())
