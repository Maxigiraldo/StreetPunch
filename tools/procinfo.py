"""Datos del servidor que se leen desde fuera: /proc (Linux) y GET /metrics.

Lo comparten monitor.py, churn.py y slow_client.py. No importa nada de `server/`.
"""

import json
import os
import socket
import time

HOST = "127.0.0.1"
SOCKET_TIMEOUT_S = 2.0
READ_BYTES = 4096
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
PAGE_BYTES = os.sysconf("SC_PAGE_SIZE")
BYTES_PER_MB = 1_048_576
TCP_CLOSE_WAIT, TCP_LISTEN, TCP_ESTABLISHED = "08", "0A", "01"


def find_server_pid(port: int) -> int | None:
    """Proceso que ESCUCHA en `port`. Se busca por el puerto y no por el nombre
    del comando: así, con dos servidores corriendo (p. ej. 8080 y 8081), cada
    herramienta mide el suyo. Camino: /proc/net/tcp da el inode del socket en
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
    rss_mb = int(fields[21]) * PAGE_BYTES / BYTES_PER_MB
    return {"fds": fds, "cpu_s": cpu_s, "rss_mb": rss_mb}


def count_tcp(port: int, state: str) -> int:
    """Conexiones en `state` cuyo extremo local es el servidor."""
    total = 0
    with open("/proc/net/tcp") as f:
        next(f)
        for line in f:
            cols = line.split()
            if int(cols[1].split(":")[1], 16) == port and cols[3] == state:
                total += 1
    return total


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
