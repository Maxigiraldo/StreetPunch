"""Verifica escenarios de juego contra un servidor YA corriendo y resume el resultado.

Uso:
    .venv/bin/python tools/scenario_check.py [--port 8080]

Conecta dos clientes (Ana y Luis), juega tres rondas y calcula sobre los mensajes
`state` lo que a ojo es imposible de comprobar:

  Ronda 1: punto de salida, choque frontal (¿se atraviesan?) y fin de ronda.
  Ronda 2: caminar hacia el borde (velocidad máxima y caída fuera de la arena).
  Ronda 3: carga, embestida y enfriamiento.

IMPORTANTE: no debe haber otros clientes con `hello` hecho (por ejemplo wscat
abiertos), porque entrarían en la ronda y los números no serían los esperados.

Los valores esperados salen de server/config.py, no se repiten aquí. Todos los
tiempos se miden en TICKS del servidor (campo `tick` de cada estado), no con el
reloj de esta máquina, para que el resultado no dependa de la carga del equipo.
"""

import argparse
import base64
import json
import math
import os
import pathlib
import queue
import socket
import struct
import sys
import threading
import time

# Permite importar `server.config` aunque el script se ejecute desde tools/.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from server.config import Config  # noqa: E402

HOST = "127.0.0.1"
WAIT_TIMEOUT_S = 20.0        # una fase completa dura ~3 s + 3 s; 20 s da margen sin colgarse
COLLISION_TICKS = 90         # 3 s de empuje frontal: de sobra para que se encuentren y reboten
CHARGE_WINDOW_TICKS = 36     # 1,2 s: un poco más que CHARGE_MAX_S para ver que se llena
DASH_WINDOW_TICKS = 40       # 1,33 s tras soltar: menos que el enfriamiento (1,5 s), para medirlo
FLOAT_TOLERANCE = 0.5        # px; los discos se redondean a 1 decimal en el estado
CARGA_TOLERANCE = 0.011      # la carga viaja con 2 decimales (CHARGE_DECIMALS)

OP_TEXT, OP_CLOSE = 0x1, 0x8


# ---------------------------------------------------------------- cliente mínimo

def parse_frame(buf: bytes):
    """Una trama del SERVIDOR (sin máscara). None si todavía está incompleta."""
    if len(buf) < 2:
        return None
    opcode, length, pos = buf[0] & 0x0F, buf[1] & 0x7F, 2
    if length == 126:
        if len(buf) < 4:
            return None
        length, pos = struct.unpack("!H", buf[2:4])[0], 4
    elif length == 127:
        if len(buf) < 10:
            return None
        length, pos = struct.unpack("!Q", buf[2:10])[0], 10
    if len(buf) < pos + length:
        return None
    return opcode, buf[pos:pos + length], buf[pos + length:]


class Client:
    def __init__(self, name: str, port: int) -> None:
        self.name = name
        self.id = -1
        self.inbox: queue.Queue = queue.Queue()
        self.sock = socket.create_connection((HOST, port), timeout=WAIT_TIMEOUT_S)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (f"GET /ws HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
             f"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
             f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(4096)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("el servidor no respondió 101")
        leftover = head.split(b"\r\n\r\n", 1)[1]
        # Hilo lector: así el servidor nunca ve un cliente lento ni se llena el buffer de B,
        # aunque el script solo consuma los estados de A.
        threading.Thread(target=self._read_loop, args=(leftover,), daemon=True).start()

    def send(self, obj: dict) -> None:
        payload = json.dumps(obj, separators=(",", ":")).encode()
        mask = os.urandom(4)  # el cliente SIEMPRE enmascara (RFC 6455 §5.3)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes([0x80 | OP_TEXT, 0x80 | len(payload)]) + mask + masked)

    def _read_loop(self, buf: bytes) -> None:
        try:
            while True:
                frame = parse_frame(buf)
                while frame is None:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        self.inbox.put({"t": "closed", "code": None})
                        return
                    buf += chunk
                    frame = parse_frame(buf)
                opcode, payload, buf = frame
                if opcode == OP_TEXT:
                    self.inbox.put(json.loads(payload))
                elif opcode == OP_CLOSE:
                    code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                    self.inbox.put({"t": "closed", "code": code})
                    return
        except OSError:
            self.inbox.put({"t": "closed", "code": None})

    def join(self) -> None:
        self.send({"t": "hello", "name": self.name})
        msg = self.inbox.get(timeout=WAIT_TIMEOUT_S)
        if msg["t"] != "welcome":
            raise RuntimeError(f"se esperaba welcome y llegó {msg}")
        self.id = msg["id"]

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- utilidades

def collect(client: Client, until) -> list[dict]:
    """Lee estados hasta que `until(estado)` sea cierto; devuelve todos (el último cumple)."""
    seen: list[dict] = []
    deadline = time.monotonic() + WAIT_TIMEOUT_S
    while True:
        try:
            msg = client.inbox.get(timeout=max(0.01, deadline - time.monotonic()))
        except queue.Empty:
            raise TimeoutError("el servidor no llegó al estado esperado a tiempo") from None
        if msg["t"] in ("closed", "error"):
            raise ConnectionError(f"el servidor cortó la conexión: {msg}")
        if msg["t"] == "state":
            seen.append(msg)
            if until(msg):
                return seen
        if time.monotonic() > deadline:
            raise TimeoutError("el servidor no llegó al estado esperado a tiempo")


def row(state: dict, player_id: int):
    for r in state["d"]:
        if r[0] == player_id:
            return r  # [id, x, y, carga, vivo]
    return None


def dist(a, b) -> float:
    return math.hypot(a[1] - b[1], a[2] - b[2])


def rounding_tolerance(cfg: Config) -> float:
    """Error máximo (px/s) al calcular velocidades con posiciones REDONDEADAS.

    El servidor redondea las posiciones a POSITION_DECIMALS decimales en el
    mensaje; dos muestras seguidas pueden diferir en un paso de redondeo de más
    o de menos. A 170 px/s cada tick avanza 5,667 px y se ven pasos de 5,6 o 5,7
    px: 168 o 171 px/s aunque la velocidad real sea exactamente 170."""
    return 10 ** -cfg.POSITION_DECIMALS / cfg.TICK_DT


def peak_speed(samples: list[tuple[int, float, float]], dt: float) -> float:
    """Velocidad máxima entre muestras consecutivas (tick, x, y).

    Se divide por los ticks REALES que separan cada par: si el script se saltó
    estados, dividir siempre por un tick inflaría la velocidad."""
    return max(math.hypot(x1 - x0, y1 - y0) / ((t1 - t0) * dt)
               for (t0, x0, y0), (t1, x1, y1) in zip(samples, samples[1:]))


class Report:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, str]] = []

    def add(self, ok: bool | None, what: str, detail: str) -> None:
        status = "INFO" if ok is None else ("PASS" if ok else "FAIL")
        self.lines.append((status, what, detail))
        print(f"  [{status}] {what}: {detail}", flush=True)

    @property
    def failed(self) -> bool:
        return any(s == "FAIL" for s, _, _ in self.lines)


def next_round_playing(a: Client) -> dict:
    """Espera a la siguiente ronda (fin de la anterior + cuenta atrás) y devuelve su primer `playing`."""
    return collect(a, lambda s: s["phase"] == "playing" and s["tick"] > 0)[-1]


def sides(state: dict, a: Client, b: Client) -> tuple[Client, Client]:
    """(el que nació en x>0, el que nació en x<0). Depende del orden de los `hello`, no del id."""
    xa, xb = row(state, a.id)[1], row(state, b.id)[1]
    return (a, b) if xa > xb else (b, a)


# ---------------------------------------------------------------- escenarios

def round1(a: Client, b: Client, cfg: Config, rep: Report) -> None:
    print("\nRonda 1: salida, choque frontal y fin de ronda")
    countdown = collect(a, lambda s: s["phase"] == "countdown")[-1]
    ids = {r[0] for r in countdown["d"]}
    if ids != {a.id, b.id}:
        raise RuntimeError(f"hay otros jugadores en la ronda (ids {sorted(ids)}); "
                           "cierra los wscat y vuelve a probar")
    ra, rb = row(countdown, a.id), row(countdown, b.id)
    ring = cfg.ARENA_RADIUS * cfg.SPAWN_RING_FRACTION
    opposite = abs(ra[1] + rb[1]) < FLOAT_TOLERANCE and abs(ra[2] + rb[2]) < FLOAT_TOLERANCE
    on_ring = abs(math.hypot(ra[1], ra[2]) - ring) < FLOAT_TOLERANCE
    rep.add(opposite and on_ring and dist(ra, rb) > 0,
            "salida en lados opuestos",
            f"Ana(id {a.id})=({ra[1]},{ra[2]})  Luis(id {b.id})=({rb[1]},{rb[2]})  "
            f"distancia={dist(ra, rb):.1f}  (esperado {2 * ring:.1f})")

    playing = collect(a, lambda s: s["phase"] == "playing")[-1]
    plus, minus = sides(playing, a, b)
    plus.send({"t": "input", "dx": -1, "dy": 0})   # cada uno va hacia el otro
    minus.send({"t": "input", "dx": 1, "dy": 0})
    window = collect(a, lambda s: s["tick"] >= playing["tick"] + COLLISION_TICKS)
    dists = [dist(row(s, plus.id), row(s, minus.id))
             for s in window if row(s, plus.id) and row(s, minus.id)
             and row(s, plus.id)[4] and row(s, minus.id)[4]]
    min_dist = min(dists)
    contact = 2 * cfg.DISC_RADIUS
    rep.add(min_dist <= contact + 20, "los discos llegan a chocar",
            f"distancia mínima={min_dist:.1f}  (contacto={contact:.1f})")
    rep.add(min_dist >= contact - FLOAT_TOLERANCE, "no se atraviesan (sin tunneling)",
            f"distancia mínima={min_dist:.1f}  >= {contact - FLOAT_TOLERANCE:.1f}")

    # Terminar la ronda: uno se queda quieto y el otro camina hacia arriba hasta caer.
    start = window[-1]["tick"]
    plus.send({"t": "input", "dx": 0, "dy": 1})
    minus.send({"t": "input", "dx": 0, "dy": 0})
    over = collect(a, lambda s: s["phase"] == "round_over")[-1]
    alive = [r[0] for r in over["d"] if r[4]]
    consistent = len(alive) == 1 and over["winner"] == alive[0] \
        and over["scores"].get(str(alive[0])) == 1
    rep.add(consistent, "fin de ronda coherente",
            f"ganador={over['winner']}  vivos={alive}  marcador={over['scores']}  "
            f"({over['tick'] - start} ticks desde que uno caminó hacia el borde)")


def round2(a: Client, cfg: Config, rep: Report, b: Client) -> None:
    print("\nRonda 2: caminar hacia el borde")
    playing = next_round_playing(a)
    plus, _ = sides(playing, a, b)
    plus.send({"t": "input", "dx": 1, "dy": 0})
    states = collect(a, lambda s: (row(s, plus.id) or [0, 0, 0, 0, 0])[4] == 0)
    track = [row(s, plus.id) for s in states if row(s, plus.id)]
    xs = [r[1] for r in track]
    dt = 1 / cfg.TICK_HZ
    walk_peak = peak_speed([(s["tick"], row(s, plus.id)[1], row(s, plus.id)[2])
                            for s in states if row(s, plus.id)], dt)
    last_dead = track[-1]
    tol = rounding_tolerance(cfg)
    rep.add(walk_peak <= cfg.MAX_SPEED + tol, "velocidad caminando <= MAX_SPEED",
            f"pico={walk_peak:.1f} px/s  (MAX_SPEED={cfg.MAX_SPEED}, tolerancia de redondeo ±{tol:.0f})")
    rep.add(math.hypot(last_dead[1], last_dead[2]) > cfg.ARENA_RADIUS
            and math.hypot(track[-2][1], track[-2][2]) <= cfg.ARENA_RADIUS,
            "cae justo al cruzar el borde",
            f"último estado vivo r={math.hypot(track[-2][1], track[-2][2]):.1f}, "
            f"primero muerto r={math.hypot(last_dead[1], last_dead[2]):.1f}  (borde={cfg.ARENA_RADIUS})")
    # Estimación con la física de config: acelerar hasta MAX_SPEED y seguir a esa velocidad.
    edge = cfg.ARENA_RADIUS - cfg.ARENA_RADIUS * cfg.SPAWN_RING_FRACTION
    estimate = edge / cfg.MAX_SPEED + cfg.MAX_SPEED / (2 * cfg.ACCELERATION)
    rep.add(None, "tiempo hasta caer",
            f"{len(xs) - 1} ticks = {(len(xs) - 1) * dt:.2f} s  (estimado con config: {estimate:.2f} s)")
    collect(a, lambda s: s["phase"] == "round_over")


def round3(a: Client, cfg: Config, rep: Report, b: Client) -> None:
    print("\nRonda 3: carga inmóvil, puntería, embestida y enfriamiento")
    playing = next_round_playing(a)
    plus, _ = sides(playing, a, b)
    spawn = row(playing, plus.id)
    plus.send({"t": "charge", "on": True})
    # Con la carga activa se pulsa una diagonal: no debe moverlo, solo apuntar
    # (hacia el centro y hacia +y; el centro está a la izquierda del que nace en x>0).
    plus.send({"t": "input", "dx": -1, "dy": 1})
    states = collect(a, lambda s: s["tick"] >= playing["tick"] + CHARGE_WINDOW_TICKS)
    held = max(math.hypot(row(s, plus.id)[1] - spawn[1], row(s, plus.id)[2] - spawn[2]) for s in states)
    rep.add(held <= FLOAT_TOLERANCE, "inmóvil mientras carga, aunque pulse una dirección",
            f"desplazamiento máximo={held:.2f} px en {CHARGE_WINDOW_TICKS} ticks")
    cargas = [row(s, plus.id)[3] for s in states]
    rising = all(cargas[i] <= cargas[i + 1] + CARGA_TOLERANCE for i in range(len(cargas) - 1))
    full_at = next((i for i, c in enumerate(cargas) if c >= 1.0 - CARGA_TOLERANCE), None)
    expected = round(cfg.CHARGE_MAX_S * cfg.TICK_HZ)
    rep.add(rising and full_at is not None and abs(full_at - expected) <= 3,
            "la carga sube hasta 1.0 en CHARGE_MAX_S",
            f"llega a 1.0 en el tick {full_at}  (esperado ~{expected})")

    # Se sueltan las flechas (la puntería queda guardada en el servidor) y se lanza.
    plus.send({"t": "input", "dx": 0, "dy": 0})
    x0, y0 = row(states[-1], plus.id)[1], row(states[-1], plus.id)[2]
    release_tick = states[-1]["tick"]
    plus.send({"t": "charge", "on": False})
    pre = collect(a, lambda s: s["tick"] >= release_tick + 2)   # deja que el tick procese la suelta
    plus.send({"t": "charge", "on": True})                       # volver a cargar DENTRO del enfriamiento
    window = collect(a, lambda s: s["tick"] >= release_tick + DASH_WINDOW_TICKS)
    track = [row(s, plus.id) for s in window]
    dt = 1 / cfg.TICK_HZ
    samples = [(states[-1]["tick"], x0, y0)] + [(s["tick"], row(s, plus.id)[1], row(s, plus.id)[2])
                                               for s in pre + window]
    peak = peak_speed(samples, dt)
    dx, dy = samples[-1][1] - x0, samples[-1][2] - y0
    travelled = math.hypot(dx, dy)
    rep.add(cfg.MAX_SPEED < peak <= cfg.DASH_MAX_SPEED + rounding_tolerance(cfg),
            "la embestida supera la velocidad de caminar",
            f"pico={peak:.0f} px/s  (MAX_SPEED={cfg.MAX_SPEED}, DASH_MAX_SPEED={cfg.DASH_MAX_SPEED})")
    # La diagonal apuntada (-1,+1) debe dar un desplazamiento de ~45°: |dx| ≈ |dy|, dx<0, dy>0.
    aimed = dx < 0 < dy and 0.8 <= abs(dy) / abs(dx) <= 1.25
    rep.add(aimed, "la embestida sale hacia donde se apuntó",
            f"desplazamiento=({dx:.0f}, {dy:.0f}) px  (esperado: dx<0, dy>0 y |dx|≈|dy|)")
    rep.add(max(r[3] for r in track) <= CARGA_TOLERANCE, "enfriamiento: no recarga",
            f"carga máxima en la ventana={max(r[3] for r in track)}  "
            f"(DASH_COOLDOWN_S={cfg.DASH_COOLDOWN_S} s)")
    gap = 2 * cfg.ARENA_RADIUS * cfg.SPAWN_RING_FRACTION - 2 * cfg.DISC_RADIUS
    rep.add(None, "alcance de una embestida desde la salida",
            f"recorre ~{travelled:.0f} px en {DASH_WINDOW_TICKS} ticks y la separación a cubrir es "
            f"{gap:.0f} px -> {'alcanza' if travelled >= gap else 'NO alcanza al rival sin acercarse antes'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=Config().PORT)
    args = parser.parse_args()
    cfg, rep = Config(), Report()
    a = b = None
    try:
        a, b = Client("Ana", args.port), Client("Luis", args.port)
        a.join()
        b.join()
        round1(a, b, cfg, rep)
        round2(a, cfg, rep, b)
        round3(a, cfg, rep, b)
    except (RuntimeError, TimeoutError, ConnectionError, OSError) as exc:
        print(f"\nABORTADO: {exc}")
        return 2
    finally:
        for c in (a, b):
            if c:
                c.close()
    print("\nResumen:", "HAY FALLOS" if rep.failed else "todo PASS",
          f"({sum(s == 'PASS' for s, _, _ in rep.lines)} PASS, "
          f"{sum(s == 'FAIL' for s, _, _ in rep.lines)} FAIL, "
          f"{sum(s == 'INFO' for s, _, _ in rep.lines)} INFO)")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
