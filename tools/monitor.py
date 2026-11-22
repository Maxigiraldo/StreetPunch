"""Monitor en vivo del servidor: una línea por segundo en la terminal.

Uso (con el servidor corriendo en otra terminal):
    .venv/bin/python tools/monitor.py [--port 8080] [--interval 1] [--once] [--no-ping]

De dónde sale cada dato (por eso es fiable: no depende de logs):
  * El servidor: GET /metrics (contadores, percentiles del tick, fase, jugadores).
  * El sistema operativo: /proc/<pid> (descriptores, memoria, CPU) y
    /proc/net/tcp (conexiones ESTABLISHED / CLOSE_WAIT).
  * Este script, como cliente: latencia HTTP (conectar + pedir /metrics) y
    latencia WebSocket (ping -> pong, sin hacer hello, así no entra a la partida).

Qué significa cada columna:
  tick/s    ticks reales por segundo. Debe ser ~30; menos = el servidor no da abasto.
  p50/p99/max  duración del trabajo de un tick (ms, ventana de ~10 s). El presupuesto
            es 1000/30 = 33 ms; si p99 se acerca, el tick se queda sin margen.
  http/ws   latencia medida desde fuera (ms). Si sube sin que suba el tick, el event
            loop está ocupado en otra cosa. ws incluye pasar por la tarea writer.
  conn      conexiones activas (jugando + espectadores = suscritas).
  out/in    KB/s enviados por el servidor y entradas/s que el tick ejecutó.
  drop/s    entradas descartadas (cola llena o más de 4 por tick).
  ovw/s     estados reemplazados en el buzón sin enviarse = un cliente lento.
  ovr       ticks que se pasaron de su presupuesto (acumulado).
  fd        descriptores abiertos del proceso: debe volver a su nivel base.
  cw        sockets en CLOSE_WAIT del lado servidor: debe ser 0 (si no, fuga).

Es una herramienta de verificación: no importa nada de `server/`.
"""

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time

HOST = "127.0.0.1"
SOCKET_TIMEOUT_S = 2.0
READ_BYTES = 4096
HEADER_EVERY_ROWS = 20
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
PAGE_BYTES = os.sysconf("SC_PAGE_SIZE")
TCP_CLOSE_WAIT = "08"
OP_PING, OP_PONG = 0x9, 0xA

# Umbrales de alerta. Son criterio de este monitor, no del servidor.
WARN_TICK_P99_FRACTION = 0.5     # p99 del tick > 50 % del presupuesto
WARN_RATE_FRACTION = 0.9         # menos del 90 % de los ticks esperados por segundo
WARN_LATENCY_MS = 50.0

RED, YELLOW, RESET = "\033[31m", "\033[33m", "\033[0m"


# ---------------------------------------------------------------- proceso
def find_server_pid() -> int | None:
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        if b"server.main" in args and int(entry) != os.getpid():
            return int(entry)
    return None


def read_proc(pid: int) -> dict:
    """fds, memoria residente (MB) y tiempo de CPU acumulado (s)."""
    fds = len(os.listdir(f"/proc/{pid}/fd"))
    with open(f"/proc/{pid}/stat") as f:
        # El nombre del proceso va entre paréntesis y puede tener espacios:
        # se parte después del último ')'.
        fields = f.read().rsplit(")", 1)[1].split()
    cpu_s = (int(fields[11]) + int(fields[12])) / CLOCK_TICKS   # utime + stime
    rss_mb = int(fields[21]) * PAGE_BYTES / 1_048_576
    return {"fds": fds, "cpu_s": cpu_s, "rss_mb": rss_mb}


def count_close_wait(port: int) -> int:
    """Conexiones cuyo extremo local es el servidor y quedaron en CLOSE_WAIT."""
    total = 0
    with open("/proc/net/tcp") as f:
        next(f)
        for line in f:
            cols = line.split()
            if int(cols[1].split(":")[1], 16) == port and cols[3] == TCP_CLOSE_WAIT:
                total += 1
    return total


# ---------------------------------------------------------------- red
def fetch_metrics(port: int) -> tuple[dict, float]:
    """GET /metrics con una conexión nueva. Devuelve (datos, ms que tardó)."""
    started = time.perf_counter()
    with socket.create_connection((HOST, port), timeout=SOCKET_TIMEOUT_S) as sock:
        sock.sendall(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        raw = b""
        while chunk := sock.recv(READ_BYTES):
            raw += chunk
    elapsed_ms = (time.perf_counter() - started) * 1000
    head, _, body = raw.partition(b"\r\n\r\n")
    if b" 200 " not in head.split(b"\r\n")[0]:
        raise RuntimeError(head.split(b"\r\n")[0].decode(errors="replace"))
    return json.loads(body), elapsed_ms


class PingProbe:
    """Conexión WebSocket que NO hace hello: solo mide ping -> pong.

    Sin hello el servidor no la cuenta como jugador ni como espectador, así que
    no altera la partida. (Cuando P6 agregue el plazo de handshake de 5 s, el
    servidor la cerrará: esta sonda se reconecta sola en el siguiente ciclo.)
    """

    def __init__(self, port: int) -> None:
        self._port = port
        self._sock: socket.socket | None = None

    def _connect(self) -> socket.socket:
        sock = socket.create_connection((HOST, self._port), timeout=SOCKET_TIMEOUT_S)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET /ws HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                      f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(READ_BYTES)
            if not chunk:
                raise ConnectionError("cerró durante el saludo")
            head += chunk
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("sin 101")
        return sock

    def rtt_ms(self) -> float | None:
        """None si falló (la sonda se reabre en la próxima llamada)."""
        try:
            if self._sock is None:
                self._sock = self._connect()
            payload = struct.pack("!d", time.perf_counter())
            mask = os.urandom(4)   # el cliente SIEMPRE enmascara (RFC 6455 §5.3)
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            started = time.perf_counter()
            self._sock.sendall(bytes([0x80 | OP_PING, 0x80 | len(payload)]) + mask + masked)
            self._read_pong(len(payload))
            return (time.perf_counter() - started) * 1000
        except (OSError, RuntimeError):
            self.close()
            return None

    def _read_pong(self, payload_len: int) -> None:
        expected = 2 + payload_len           # trama del servidor: sin máscara, <126 bytes
        data = b""
        while len(data) < expected:
            chunk = self._sock.recv(expected - len(data))
            if not chunk:
                raise ConnectionError("cerrada")
            data += chunk
        if data[0] & 0x0F != OP_PONG:
            raise RuntimeError("no es un pong")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# ---------------------------------------------------------------- reporte
class Series:
    """Resumen final (mínimo, promedio, máximo) de una columna."""

    def __init__(self) -> None:
        self.values: list[float] = []

    def add(self, value: float | None) -> None:
        if value is not None:
            self.values.append(value)

    def summary(self) -> str:
        if not self.values:
            return "—"
        return f"{min(self.values):.1f} / {sum(self.values) / len(self.values):.1f} / {max(self.values):.1f}"


def fmt(value: float | None, width: int, decimals: int = 1) -> str:
    return f"{'—':>{width}}" if value is None else f"{value:>{width}.{decimals}f}"


HEADER = (f"{'hora':8} {'tick/s':>6} {'p50':>5} {'p99':>5} {'max':>5} {'http':>5} {'ws':>5} "
          f"{'conn':>4} {'juega':>5} {'fase':<9} {'out KB/s':>8} {'in/s':>5} {'drop/s':>6} "
          f"{'ovw/s':>5} {'ovr':>4} {'fd':>4} {'MB':>5} {'cpu%':>5} {'cw':>3}  alertas")


def build_row(now: dict, prev: dict | None, http_ms: float, ws_ms: float | None,
              proc: dict, prev_proc: dict | None, close_wait: int, elapsed: float) -> tuple[str, list[str]]:
    game, tick_ms = now["game"], now["tick_ms"]
    budget_ms = 1000 / now["config"]["tick_hz"]

    def rate(key: str) -> float:
        return (now[key] - prev[key]) / elapsed if prev else 0.0

    tick_rate = (game["tick"] - prev["game"]["tick"]) / elapsed if prev else None
    drops = rate("inputs_dropped_queue") + rate("inputs_dropped_rate")
    cpu = ((proc["cpu_s"] - prev_proc["cpu_s"]) / elapsed * 100) if prev_proc else None

    alerts: list[str] = []
    if tick_rate is not None and tick_rate < now["config"]["tick_hz"] * WARN_RATE_FRACTION:
        alerts.append("ticks lentos")
    if tick_ms["p99"] is not None and tick_ms["p99"] > budget_ms * WARN_TICK_P99_FRACTION:
        alerts.append("p99 alto")
    if prev and now["tick_overruns"] > prev["tick_overruns"]:
        alerts.append("tick pasado de presupuesto")
    if drops > 0:
        alerts.append("descartando entradas")
    if rate("mailbox_overwrites") > 0:
        alerts.append("cliente lento")
    if close_wait:
        alerts.append("CLOSE_WAIT (fuga)")
    if http_ms > WARN_LATENCY_MS or (ws_ms is not None and ws_ms > WARN_LATENCY_MS):
        alerts.append("latencia alta")

    kb_out = rate("bytes_sent") / 1024
    line = (f"{time.strftime('%H:%M:%S')} {fmt(tick_rate, 6)} {fmt(tick_ms['p50'], 5)} "
            f"{fmt(tick_ms['p99'], 5)} {fmt(tick_ms['max'], 5)} {fmt(http_ms, 5)} {fmt(ws_ms, 5)} "
            f"{now['connections_active']:>4} {game['playing']:>5} {game['phase']:<9} "
            f"{kb_out:>8.1f} {rate('inputs_applied'):>5.0f} {drops:>6.1f} "
            f"{rate('mailbox_overwrites'):>5.1f} {now['tick_overruns']:>4} {proc['fds']:>4} "
            f"{proc['rss_mb']:>5.1f} {fmt(cpu, 5)} {close_wait:>3}  ")
    text = ", ".join(alerts)
    if alerts:
        colour = RED if any(a.startswith(("CLOSE", "tick pasado")) for a in alerts) else YELLOW
        text = f"{colour}{text}{RESET}"
    return line + text, alerts


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor en vivo de StreetPunch")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--interval", type=float, default=1.0, help="segundos entre muestras")
    parser.add_argument("--once", action="store_true", help="una sola muestra (JSON) y salir")
    parser.add_argument("--no-ping", action="store_true", help="no medir latencia WebSocket")
    args = parser.parse_args()

    if args.once:
        data, ms = fetch_metrics(args.port)
        data["http_ms"] = round(ms, 2)
        print(json.dumps(data, indent=2))
        return

    pid = find_server_pid()
    if pid is None:
        sys.exit("No encuentro el servidor: ¿está corriendo `python -m server.main`?")
    probe = None if args.no_ping else PingProbe(args.port)
    series = {name: Series() for name in ("tick/s", "p99 tick ms", "http ms", "ws ms", "out KB/s", "cpu %")}
    prev = prev_proc = None
    prev_time = time.monotonic()
    rows = alert_rows = 0
    print(f"Monitor del servidor pid {pid}, puerto {args.port}. Ctrl+C para terminar.\n")
    try:
        while True:
            try:
                now, http_ms = fetch_metrics(args.port)
                proc = read_proc(pid)
            except (OSError, ValueError, RuntimeError) as err:
                print(f"{RED}sin respuesta del servidor: {err}{RESET}")
                prev = prev_proc = None
                time.sleep(args.interval)
                continue
            ws_ms = probe.rtt_ms() if probe else None
            clock = time.monotonic()
            elapsed, prev_time = clock - prev_time, clock
            if rows % HEADER_EVERY_ROWS == 0:
                print(HEADER)
            line, alerts = build_row(now, prev, http_ms, ws_ms, proc, prev_proc,
                                     count_close_wait(args.port), elapsed)
            print(line)
            rows += 1
            alert_rows += bool(alerts)
            if prev:
                series["tick/s"].add((now["game"]["tick"] - prev["game"]["tick"]) / elapsed)
                series["out KB/s"].add((now["bytes_sent"] - prev["bytes_sent"]) / elapsed / 1024)
            series["p99 tick ms"].add(now["tick_ms"]["p99"])
            series["http ms"].add(http_ms)
            series["ws ms"].add(ws_ms)
            if prev_proc:
                series["cpu %"].add((proc["cpu_s"] - prev_proc["cpu_s"]) / elapsed * 100)
            prev, prev_proc = now, proc
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nResumen ({rows} muestras, {alert_rows} con alertas)   mín / prom / máx")
        for name, serie in series.items():
            print(f"  {name:<12} {serie.summary()}")
    finally:
        if probe:
            probe.close()


if __name__ == "__main__":
    main()
