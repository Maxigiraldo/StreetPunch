"""Bots: N jugadores simulados para cargar el servidor.

Uso (con el servidor corriendo):
    .venv/bin/python tools/bots.py [--count 50] [--duration 30] [--port 8080] [--ramp 0.02]
                                   [--room ID] [--no-host]

Cada bot hace lo que haría el navegador: saluda con `hello`, CONTESTA los ping
del servidor (si no, el servidor lo echa por inactividad a los 30 s), y como
las partidas son por SALAS, decide qué hacer con la lista que el servidor le
manda en el vestíbulo:
  * si hay una sala abierta (con sitio y en espera), se une a la más llena;
  * si no, crea la suya (tras una pausa al azar, para que 50 bots no creen 50
    salas a la vez) y pasa a ser su anfitrión: pide iniciar la ronda cuando hay
    gente, y la repite al terminar cada una.
Dentro de una sala cambia de dirección o carga una embestida al azar.

Con más bots que plazas (MAX_ROOM_PLAYERS = 8) se forman varias salas; con más
conexiones que MAX_CONNECTIONS (100) el servidor responde `full` y cierra con 1013.

  --room ID   solo se une a esa sala (p. ej. la que creaste tú en el navegador:
              el anfitrión eres tú y no crea ninguna otra). Si se llena, esperan.
  --no-host   los bots nunca crean sala: solo se unen a las que existan.

Al terminar imprime cuántos estados llegaron, cómo se cerró cada conexión y
cuántas fueron rechazadas. Mientras tanto, `tools/monitor.py` en otra terminal
muestra cómo lo lleva el servidor.
"""

import argparse
import asyncio
import json
import random
import time
from collections import Counter

from wslite import OP_CLOSE, OP_PING, OP_PONG, OP_TEXT, HandshakeFailed, WsConn, connect

DIRECTIONS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
THINK_MIN_S, THINK_MAX_S = 0.3, 1.0     # cada cuánto cambia de idea (un humano pulsa teclas ~así)
CHARGE_CHANCE = 0.15                    # probabilidad de intentar una embestida en cada decisión
CHARGE_MIN_S, CHARGE_MAX_S = 0.3, 1.0   # cuánto mantiene la carga
CREATE_DELAY_MIN_S, CREATE_DELAY_MAX_S = 0.2, 1.5   # pausa al azar antes de crear sala
RETRY_S = 1.0                           # tras un rechazo (sala llena o jugando), reevalúa
HOST_FIRST_START_S = 3.0                # el anfitrión da tiempo a que otros se unan
HOST_START_EVERY_S = 1.0                # y luego pide iniciar cada segundo (inofensivo si no procede)
PROGRESS_EVERY_S = 5.0
STATE_PREFIX = b'{"t":"state"'
ERROR_PREFIX = b'{"t":"error","code":"'
SOFT_ERRORS = {"room_full", "room_busy", "no_room", "too_many_rooms", "already_in_room", "not_in_room"}


class Stats:
    def __init__(self) -> None:
        self.connected = 0
        self.connect_failed = 0
        self.states = 0
        self.rooms_created = 0
        self.errors: Counter[str] = Counter()     # códigos de error del servidor (full, timeout...)
        self.closes: Counter[str] = Counter()     # cómo terminó cada conexión

    def total_closed(self) -> int:
        return sum(self.closes.values())


async def behave(conn: WsConn, rng: random.Random) -> None:
    """Decide intenciones al azar, igual que un jugador pulsando teclas."""
    while True:
        dx, dy = rng.choice(DIRECTIONS)
        await conn.send_json({"t": "input", "dx": dx, "dy": dy})
        if rng.random() < CHARGE_CHANCE:
            await conn.send_json({"t": "charge", "on": True})
            await asyncio.sleep(rng.uniform(CHARGE_MIN_S, CHARGE_MAX_S))
            await conn.send_json({"t": "charge", "on": False})
        await asyncio.sleep(rng.uniform(THINK_MIN_S, THINK_MAX_S))


async def host_loop(conn: WsConn) -> None:
    """Hace de anfitrión: pide iniciar cada segundo. Es inofensivo fuera de la
    sala de espera (el servidor ignora la orden) y así las rondas se encadenan
    solas durante una prueba de carga."""
    await asyncio.sleep(HOST_FIRST_START_S)
    while True:
        await conn.send_json({"t": "start"})
        await asyncio.sleep(HOST_START_EVERY_S)


class Bot:
    """La "cabeza" de un bot: qué hacer con cada mensaje del servidor."""

    def __init__(self, conn: WsConn, rng: random.Random, stats: Stats, target_room: int | None,
                 may_create: bool) -> None:
        self.conn, self.rng, self.stats = conn, rng, stats
        self.target_room = target_room
        self.may_create = may_create and target_room is None
        self.rooms: list[dict] = []
        self.in_room = False
        self.created_by_me = False
        self.tasks: list[asyncio.Task] = []
        self.pending: asyncio.Task | None = None    # decisión diferida (crear sala / reintentar)

    def schedule(self, delay: float) -> None:
        """Reevalúa la lista de salas más tarde. Una sola decisión pendiente a la vez."""
        if self.pending is None or self.pending.done():
            self.pending = asyncio.create_task(self._later(delay))

    async def _later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.decide(after_wait=True)

    async def decide(self, after_wait: bool = False) -> None:
        if self.in_room:
            return
        if self.target_room is not None:
            wanted = [r for r in self.rooms if r["id"] == self.target_room and r["open"]]
            if wanted:
                await self.conn.send_json({"t": "join", "room": self.target_room})
            return
        open_rooms = [r for r in self.rooms if r["open"]]
        if open_rooms:
            fullest = max(open_rooms, key=lambda r: r["players"])    # llena una antes de abrir otra
            await self.conn.send_json({"t": "join", "room": fullest["id"]})
        elif self.may_create:
            if after_wait:
                await self.conn.send_json({"t": "create"})
                self.created_by_me = True
                self.stats.rooms_created += 1
            else:
                self.schedule(self.rng.uniform(CREATE_DELAY_MIN_S, CREATE_DELAY_MAX_S))

    async def on_message(self, text: bytes) -> None:
        if text.startswith(STATE_PREFIX):
            self.stats.states += 1
            return
        msg = json.loads(text)
        kind = msg.get("t")
        if kind == "rooms":
            self.rooms = msg["rooms"]
            await self.decide()
        elif kind == "room":
            self.in_room = msg["id"] is not None
            self.stop_tasks()
            if self.in_room:
                self.tasks.append(asyncio.create_task(behave(self.conn, self.rng)))
                if self.created_by_me:
                    self.tasks.append(asyncio.create_task(host_loop(self.conn)))
        elif kind == "error":
            code = msg.get("code", "")
            if code in SOFT_ERRORS:
                self.schedule(RETRY_S)         # sala llena/jugando: otra vez con la lista que tengo
            else:
                self.stats.errors[code] += 1

    def stop_tasks(self) -> None:
        for task in self.tasks:
            task.cancel()
        self.tasks.clear()
        self.created_by_me = self.created_by_me and self.in_room

    def stop(self) -> None:
        self.stop_tasks()
        if self.pending is not None:
            self.pending.cancel()


async def listen(conn: WsConn, bot: Bot, stats: Stats) -> str:
    """Lee hasta que la conexión termine. Devuelve cómo terminó."""
    while True:
        opcode, payload = await conn.recv()
        if opcode == OP_PING:
            await conn.send(OP_PONG, payload)          # sin esto el servidor nos echa a los 30 s
        elif opcode == OP_TEXT:
            if payload.startswith(ERROR_PREFIX) and payload[len(ERROR_PREFIX):].split(b'"')[0].decode() == "full":
                stats.errors["full"] += 1              # tope de conexiones: viene con close 1013
            await bot.on_message(payload)
        elif opcode == OP_CLOSE:
            return f"close {WsConn.close_code(payload)}"


async def run_bot(index: int, port: int, stats: Stats, deadline: float, rng: random.Random,
                  target_room: int | None, may_create: bool) -> None:
    try:
        conn = await connect(port)
    except (OSError, HandshakeFailed, asyncio.TimeoutError):
        stats.connect_failed += 1
        return
    stats.connected += 1
    bot = Bot(conn, rng, stats, target_room, may_create)
    try:
        await conn.send_json({"t": "hello", "name": f"bot{index}"})
        try:
            ended = await asyncio.wait_for(listen(conn, bot, stats), max(0.1, deadline - time.monotonic()))
            stats.closes[ended] += 1                    # lo cerró el servidor
        except asyncio.TimeoutError:
            await conn.send(OP_CLOSE, b"\x03\xe8")      # se acabó el tiempo: cierre normal (1000)
            stats.closes["terminó la prueba"] += 1
    except (OSError, asyncio.IncompleteReadError):
        stats.closes["TCP cortado"] += 1
    finally:
        bot.stop()
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
    parser.add_argument("--room", type=int, default=None,
                        help="solo unirse a esta sala (p. ej. la que creaste tú); no crean otras")
    parser.add_argument("--no-host", action="store_true", help="los bots nunca crean sala")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    stats = Stats()
    started = time.monotonic()
    deadline = started + args.duration
    print(f"{args.count} bots contra el puerto {args.port} durante {args.duration:.0f} s")
    reporter = asyncio.create_task(progress(stats, started))
    tasks = []
    for i in range(args.count):
        tasks.append(asyncio.create_task(run_bot(i, args.port, stats, deadline, random.Random(rng.random()),
                                                 args.room, not args.no_host)))
        if args.ramp:
            await asyncio.sleep(args.ramp)
    await asyncio.gather(*tasks)
    reporter.cancel()

    elapsed = time.monotonic() - started
    print(f"\nResumen tras {elapsed:.1f} s")
    print(f"  conexiones logradas: {stats.connected}   fallidas al conectar: {stats.connect_failed}   "
          f"salas creadas por bots: {stats.rooms_created}")
    print(f"  estados recibidos: {stats.states}  ({stats.states / elapsed:.0f}/s en total, "
          f"{stats.states / elapsed / max(1, stats.connected):.1f}/s por bot)")
    print(f"  errores del servidor: {dict(stats.errors) or 'ninguno'}")
    print(f"  cierres: {dict(stats.closes)}")


if __name__ == "__main__":
    asyncio.run(main())
