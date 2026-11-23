"""Comparación de modelos de concurrencia con la MISMA carga (para docs/modelo-concurrencia.md).

Uso:
    .venv/bin/python tools/model_bench.py [--clients 100,300] [--seconds 8] [--models asyncio,threads,pool,fork,threads-naive]

Qué hace: levanta, uno por uno, un servidor mínimo de cada modelo que difunde
un estado de 600 bytes a 30 Hz a todos sus clientes (algo MÁS que un estado real
de StreetPunch: 330 B con 8 discos y 456 B con 12; se usa 600 como cota prudente), conecta N clientes que solo leen, y mide desde /proc:
  * memoria real (PSS: reparte las páginas compartidas entre procesos; RSS las contaría N veces),
  * hilos/procesos, CPU, y cuántos de los clientes reciben ≥ 90 % de lo esperado.
Después repite con UN cliente lento que no lee (zombi) para ver si frena a los demás.

NO es el servidor del juego: son servidores mínimos con TCP crudo, sin WebSocket
ni física, para comparar solo el MODELO. Es una microprueba: sirve para el
orden de magnitud y para explicar el porqué, no como cifra absoluta.
"""

import argparse
import asyncio
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

HZ = 30
PAYLOAD = b"x" * 600           # cota prudente: un estado real mide 330 B (8 discos) a 456 B (12)
POOL_WORKERS = 16              # el ThreadingTCPServer "con tope": cabe por tamaño del pool
SNDBUF = 65536                 # el mismo límite de envío que usa el servidor real


def listener(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(1024)
    return sock


def tune(conn: socket.socket) -> None:
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF)
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


# ------------------------------------------------------------------ servidores

def serve_asyncio(port: int) -> None:
    """Un hilo, un event loop (epoll). Tick único y buzón por cliente, como el servidor real."""
    async def main() -> None:
        boxes: set[asyncio.Queue] = set()

        async def client(reader, writer):
            tune(writer.get_extra_info("socket"))
            box: asyncio.Queue = asyncio.Queue(1)
            boxes.add(box)
            try:
                while True:
                    data = await box.get()
                    writer.write(data)
                    await asyncio.wait_for(writer.drain(), 5)   # el zombi frena solo a su tarea
            except (ConnectionError, TimeoutError):
                pass
            finally:
                boxes.discard(box)
                writer.close()

        server = await asyncio.start_server(client, sock=listener(port), backlog=1024)
        async with server:
            while True:
                for box in boxes:
                    if box.full():
                        box.get_nowait()            # el estado viejo se pisa
                    box.put_nowait(PAYLOAD)
                await asyncio.sleep(1 / HZ)
    asyncio.run(main())


def serve_threads(port: int) -> None:
    """Un hilo por cliente; cada uno duerme y envía su último estado. Sin cola compartida."""
    sock = listener(port)
    tick = threading.Condition()
    counter = [0]

    def ticker() -> None:
        while True:
            time.sleep(1 / HZ)
            with tick:
                counter[0] += 1
                tick.notify_all()

    def client(conn: socket.socket) -> None:
        tune(conn)
        conn.settimeout(5)
        seen = 0
        try:
            while True:
                with tick:
                    while counter[0] == seen:
                        tick.wait()
                    seen = counter[0]
                conn.sendall(PAYLOAD)
        except OSError:
            pass
        finally:
            conn.close()

    threading.Thread(target=ticker, daemon=True).start()
    while True:
        conn, _ = sock.accept()
        threading.Thread(target=client, args=(conn,), daemon=True).start()


def serve_threads_naive(port: int) -> None:
    """El error clásico: UN hilo difunde con sendall bloqueante a cada cliente, en fila."""
    sock = listener(port)
    clients: list[socket.socket] = []
    lock = threading.Lock()

    def accept_loop() -> None:
        while True:
            conn, _ = sock.accept()
            tune(conn)
            conn.settimeout(5)
            with lock:
                clients.append(conn)

    threading.Thread(target=accept_loop, daemon=True).start()
    while True:
        time.sleep(1 / HZ)
        with lock:
            snapshot = list(clients)
        for conn in snapshot:
            try:
                conn.sendall(PAYLOAD)       # un zombi aquí frena a TODOS los que vienen detrás
            except OSError:
                with lock:
                    if conn in clients:
                        clients.remove(conn)


def serve_pool(port: int) -> None:
    """ThreadPool con tope (como ThreadingTCPServer acotado): cada conexión ocupa un worker entero."""
    sock = listener(port)
    pool = ThreadPoolExecutor(POOL_WORKERS)

    def client(conn: socket.socket) -> None:
        tune(conn)
        conn.settimeout(5)
        try:
            while True:
                time.sleep(1 / HZ)
                conn.sendall(PAYLOAD)
        except OSError:
            pass
        finally:
            conn.close()

    while True:
        conn, _ = sock.accept()
        pool.submit(client, conn)      # con el pool lleno, la conexión espera en la cola


def serve_fork(port: int) -> None:
    """Un proceso por cliente. Cota INFERIOR del costo: cada hijo genera su estado solo
    (sin memoria compartida ni IPC, que un juego real necesitaría y encarece más)."""
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    sock = listener(port)
    while True:
        conn, _ = sock.accept()
        if os.fork() == 0:
            sock.close()
            tune(conn)
            conn.settimeout(5)
            try:
                while True:
                    time.sleep(1 / HZ)
                    conn.sendall(PAYLOAD)
            except OSError:
                pass
            os._exit(0)
        conn.close()


SERVERS = {"asyncio": serve_asyncio, "threads": serve_threads, "threads-naive": serve_threads_naive,
           "pool": serve_pool, "fork": serve_fork}


# ------------------------------------------------------------------ medición (/proc)

def descendants(pid: int) -> list[int]:
    """El proceso y todos sus hijos (para el modelo fork)."""
    out, frontier = [pid], [pid]
    children: dict[int, list[int]] = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            try:
                with open(f"/proc/{name}/stat") as f:
                    ppid = int(f.read().rsplit(")", 1)[1].split()[1])
                children.setdefault(ppid, []).append(int(name))
            except (OSError, ValueError, IndexError):
                pass
    while frontier:
        nxt = [c for p in frontier for c in children.get(p, [])]
        out += nxt
        frontier = nxt
    return out


def sample(pid: int) -> dict:
    pss_kb = threads = 0
    cpu = 0.0
    procs = descendants(pid)
    for p in procs:
        try:
            with open(f"/proc/{p}/smaps_rollup") as f:
                pss_kb += next(int(line.split()[1]) for line in f if line.startswith("Pss:"))
            with open(f"/proc/{p}/status") as f:
                threads += next(int(line.split()[1]) for line in f if line.startswith("Threads:"))
            with open(f"/proc/{p}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
                cpu += (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
        except (OSError, StopIteration):
            pass
    return {"pss_mb": pss_kb / 1024, "threads": threads, "procs": len(procs), "cpu_s": cpu}


# ------------------------------------------------------------------ clientes

async def run_clients(port: int, n: int, seconds: float, zombie: bool) -> dict:
    received = [0] * n
    stop = asyncio.Event()

    async def reader(i: int) -> None:
        try:
            r, _ = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            return
        while not stop.is_set():
            try:
                data = await asyncio.wait_for(r.read(65536), 1.0)
            except asyncio.TimeoutError:
                continue
            if not data:
                return
            received[i] += len(data)

    tasks = [asyncio.create_task(reader(i)) for i in range(n)]
    held = []
    if zombie:      # conecta y NO lee: su búfer se llena y el servidor tiene que lidiar con él
        z = socket.socket(); z.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        z.connect(("127.0.0.1", port)); held.append(z)
    await asyncio.sleep(3.0)                       # calentamiento: que todos estén conectados
    base = list(received)
    t0 = time.monotonic()
    await asyncio.sleep(seconds)
    elapsed = time.monotonic() - t0
    got = [(received[i] - base[i]) / elapsed for i in range(n)]
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    for z in held:
        z.close()
    expected = HZ * len(PAYLOAD)
    healthy = sum(1 for g in got if g >= 0.9 * expected)
    return {"healthy": healthy, "min_hz": min(got) / len(PAYLOAD) if got else 0,
            "avg_hz": sum(got) / n / len(PAYLOAD)}


def run_clients_parallel(port: int, n: int, seconds: float, zombie: bool) -> dict:
    """Reparte los clientes en varios procesos: con uno solo, el GENERADOR de carga se
    satura (30 000 lecturas/s a 1000 clientes) y se culparía al servidor de un límite
    que es de la herramienta. Se usan como mucho la mitad de los núcleos para dejarle
    CPU al servidor que se mide."""
    workers = max(1, min(8, (os.cpu_count() or 2) // 2, n // 50))
    shares = [n // workers + (1 if i < n % workers else 0) for i in range(workers)]
    with ProcessPoolExecutor(workers) as pool:
        futures = [pool.submit(_client_proc, port, share, seconds, zombie and i == 0)
                   for i, share in enumerate(shares)]
        parts = [f.result() for f in futures]
    return {"healthy": sum(p["healthy"] for p in parts), "min_hz": min(p["min_hz"] for p in parts),
            "avg_hz": sum(p["avg_hz"] * sh for p, sh in zip(parts, shares)) / n}


def _client_proc(port: int, n: int, seconds: float, zombie: bool) -> dict:
    return asyncio.run(run_clients(port, n, seconds, zombie))


def bench(model: str, n: int, seconds: float, zombie: bool, port: int) -> dict:
    proc = subprocess.Popen([sys.executable, __file__, "--serve", model, "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):                         # esperar a que escuche
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        before = sample(proc.pid)
        t0 = time.monotonic()
        res = run_clients_parallel(port, n, seconds, zombie)
        after = sample(proc.pid)
        res.update(after)
        res["cpu_pct"] = (after["cpu_s"] - before["cpu_s"]) / (time.monotonic() - t0) * 100
        return res
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        for p in descendants(proc.pid)[1:]:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--serve", choices=SERVERS)
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--clients", default="100,300")
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--models", default="asyncio,threads,pool,fork,threads-naive")
    args = parser.parse_args()
    if args.serve:
        SERVERS[args.serve](args.port)
        return
    print(f"Carga: {len(PAYLOAD)} B a {HZ} Hz por cliente. Clientes sanos = reciben ≥ 90 % de lo esperado.\n")
    print(f"{'modelo':<14}{'N':>5}{'zombi':>6} | {'sanos':>9} {'min Hz':>7} {'prom Hz':>8} | "
          f"{'PSS MB':>7} {'hilos':>6} {'procs':>6} {'CPU %':>6}")
    port = args.port
    for n in (int(x) for x in args.clients.split(",")):
        for model in args.models.split(","):
            for zombie in (False, True):
                port += 1
                r = bench(model, n, args.seconds, zombie, port)
                print(f"{model:<14}{n:>5}{'sí' if zombie else 'no':>6} | {r['healthy']:>4}/{n:<4} {r['min_hz']:>7.1f} "
                      f"{r['avg_hz']:>8.1f} | {r['pss_mb']:>7.1f} {r['threads']:>6} {r['procs']:>6} {r['cpu_pct']:>6.1f}",
                      flush=True)


if __name__ == "__main__":
    main()
