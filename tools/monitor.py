"""Monitor en vivo del servidor: una línea por segundo en la terminal.

Uso (con el servidor corriendo en otra terminal):
    .venv/bin/python tools/monitor.py [--port 8080] [--interval 1] [--once]

De dónde sale cada dato (por eso es fiable: no depende de logs):
  * El servidor: GET /metrics (contadores, percentiles del tick, fase, jugadores).
  * El sistema operativo: /proc/<pid> (descriptores, memoria, CPU) y
    /proc/net/tcp (conexiones ESTABLISHED / CLOSE_WAIT).
  * Este script, como cliente: latencia HTTP (conectar + pedir /metrics).
  * La latencia de los clientes REALES la mide el servidor: cada 10 s manda un
    ping a cada conexión y cronometra el pong (RTT). El monitor muestra p50 y max.

Qué significa cada columna:
  tick/s    ticks reales por segundo. Debe ser ~30; menos = el servidor no da abasto.
  p50/p99/max  duración del trabajo de un tick (ms, ventana de ~10 s). El presupuesto
            es 1000/30 = 33 ms; si p99 se acerca, el tick se queda sin margen.
  http      latencia de una petición nueva a /metrics (ms). Si sube sin que suba el
            tick, el event loop está ocupado en otra cosa.
  rtt p50/max  ping -> pong de los clientes conectados (ms, últimas muestras). Es la
            latencia de verdad de cada jugador: red + event loop + su navegador.
            "—" si aún no hubo ningún ping (el primero sale a los 10 s).
  conn      conexiones activas (jugando + espectadores = suscritas).
  out/in    KB/s enviados por el servidor y entradas/s que el tick ejecutó.
  drop/s    entradas descartadas (cola llena o más de 4 por tick).
  ovw/s     estados reemplazados en el buzón sin enviarse = un cliente lento.
  ovr       ticks que se pasaron de su presupuesto (acumulado).
  to        timeouts acumulados (saludo + inactividad + escritura): clientes que el
            servidor tuvo que echar por no hablar, no responder o no leer.
  fd        descriptores abiertos del proceso: debe volver a su nivel base.
  cw        sockets en CLOSE_WAIT del lado servidor: debe ser 0 (si no, fuga).

Es una herramienta de verificación: no importa nada de `server/`.
"""

import argparse
import json
import os
import socket
import sys
import time

HOST = "127.0.0.1"
SOCKET_TIMEOUT_S = 2.0
READ_BYTES = 4096
HEADER_EVERY_ROWS = 20
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
PAGE_BYTES = os.sysconf("SC_PAGE_SIZE")
TCP_CLOSE_WAIT, TCP_LISTEN = "08", "0A"

# Umbrales de alerta. Son criterio de este monitor, no del servidor.
WARN_TICK_P99_FRACTION = 0.5     # p99 del tick > 50 % del presupuesto
WARN_RATE_FRACTION = 0.9         # menos del 90 % de los ticks esperados por segundo
WARN_LATENCY_MS = 50.0

RED, YELLOW, RESET = "\033[31m", "\033[33m", "\033[0m"


# ---------------------------------------------------------------- proceso
def find_server_pid(port: int) -> int | None:
    """Proceso que ESCUCHA en `port`. Se busca por el puerto y no por el nombre
    del comando: así, con dos servidores corriendo (p. ej. 8080 y 8081), cada
    monitor mide el suyo. Camino: /proc/net/tcp da el inode del socket en
    LISTEN, y el proceso dueño es el que tiene ese inode entre sus descriptores."""
    inode = None
    with open("/proc/net/tcp") as f:
        next(f)
        for line in f:
            cols = line.split()
            if cols[3] == TCP_LISTEN and int(cols[1].split(":")[1], 16) == port:
                inode = cols[9]
                break
    if inode is None:
        return None
    target = f"socket:[{inode}]"
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            for fd in os.listdir(f"/proc/{entry}/fd"):
                if os.readlink(f"/proc/{entry}/fd/{fd}") == target:
                    return int(entry)
        except OSError:
            continue   # el proceso terminó, o no es nuestro y no se puede mirar
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


HEADER = (f"{'hora':8} {'tick/s':>6} {'p50':>5} {'p99':>5} {'max':>5} {'http':>5} {'rtt50':>5} {'rttmx':>5} "
          f"{'conn':>4} {'juega':>5} {'fase':<9} {'out KB/s':>8} {'in/s':>5} {'drop/s':>6} "
          f"{'ovw/s':>5} {'ovr':>4} {'to':>3} {'fd':>4} {'MB':>5} {'cpu%':>5} {'cw':>3}  alertas")


def build_row(now: dict, prev: dict | None, http_ms: float,
              proc: dict, prev_proc: dict | None, close_wait: int, elapsed: float) -> tuple[str, list[str]]:
    game, tick_ms, rtt = now["game"], now["tick_ms"], now["rtt_ms"]
    timeouts = now["handshake_timeouts"] + now["idle_timeouts"] + now["write_timeouts"]
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
    if prev and timeouts > prev["handshake_timeouts"] + prev["idle_timeouts"] + prev["write_timeouts"]:
        alerts.append("timeouts")
    if close_wait:
        alerts.append("CLOSE_WAIT (fuga)")
    if http_ms > WARN_LATENCY_MS or (rtt["p50"] is not None and rtt["p50"] > WARN_LATENCY_MS):
        alerts.append("latencia alta")

    kb_out = rate("bytes_sent") / 1024
    line = (f"{time.strftime('%H:%M:%S')} {fmt(tick_rate, 6)} {fmt(tick_ms['p50'], 5)} "
            f"{fmt(tick_ms['p99'], 5)} {fmt(tick_ms['max'], 5)} {fmt(http_ms, 5)} {fmt(rtt['p50'], 5)} {fmt(rtt['max'], 5)} "
            f"{now['connections_active']:>4} {game['playing']:>5} {game['phase']:<9} "
            f"{kb_out:>8.1f} {rate('inputs_applied'):>5.0f} {drops:>6.1f} "
            f"{rate('mailbox_overwrites'):>5.1f} {now['tick_overruns']:>4} {timeouts:>3} {proc['fds']:>4} "
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
    args = parser.parse_args()

    if args.once:
        data, ms = fetch_metrics(args.port)
        data["http_ms"] = round(ms, 2)
        print(json.dumps(data, indent=2))
        return

    pid = find_server_pid(args.port)
    if pid is None:
        sys.exit(f"Nadie escucha en el puerto {args.port}: ¿está corriendo el servidor?")
    series = {name: Series() for name in ("tick/s", "p99 tick ms", "http ms", "rtt p50 ms", "out KB/s", "cpu %")}
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
            clock = time.monotonic()
            elapsed, prev_time = clock - prev_time, clock
            if rows % HEADER_EVERY_ROWS == 0:
                print(HEADER)
            line, alerts = build_row(now, prev, http_ms, proc, prev_proc,
                                     count_close_wait(args.port), elapsed)
            print(line)
            rows += 1
            alert_rows += bool(alerts)
            if prev:
                series["tick/s"].add((now["game"]["tick"] - prev["game"]["tick"]) / elapsed)
                series["out KB/s"].add((now["bytes_sent"] - prev["bytes_sent"]) / elapsed / 1024)
            series["p99 tick ms"].add(now["tick_ms"]["p99"])
            series["http ms"].add(http_ms)
            series["rtt p50 ms"].add(now["rtt_ms"]["p50"])
            if prev_proc:
                series["cpu %"].add((proc["cpu_s"] - prev_proc["cpu_s"]) / elapsed * 100)
            prev, prev_proc = now, proc
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nResumen ({rows} muestras, {alert_rows} con alertas)   mín / prom / máx")
        for name, serie in series.items():
            print(f"  {name:<12} {serie.summary()}")


if __name__ == "__main__":
    main()
