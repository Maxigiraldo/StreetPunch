"""Prueba de fugas: abre y cierra muchos clientes y compara el antes y el después.

Uso (con el servidor ya corriendo en otra terminal):
    .venv/bin/python tools/leakcheck.py [--clients 50] [--port 8080]

Qué mide (solo Linux, lee /proc):
  1. Descriptores de archivo abiertos por el proceso del servidor
     (/proc/<pid>/fd). Cada conexión usa uno; si no se cierra, el número sube.
  2. Conexiones del lado del servidor en ESTABLISHED o CLOSE_WAIT
     (/proc/net/tcp). CLOSE_WAIT = el cliente cerró y el servidor NO cerró el suyo.

Este script es una herramienta de verificación, no parte del servidor: no importa
nada de `server/` y se conecta como lo haría cualquier cliente externo.
"""

import argparse
import base64
import os
import socket
import struct
import sys
import threading
import time

import procinfo

# Estados TCP en /proc/net/tcp (columna "st", en hexadecimal).
TCP_ESTABLISHED = "01"
TCP_CLOSE_WAIT = "08"

# Opcodes WebSocket que usamos (RFC 6455 §5.2).
OP_TEXT = 0x1
OP_CLOSE = 0x8

SETTLE_SECONDS = 2.0   # margen para que el servidor procese los cierres antes de medir
SOCKET_TIMEOUT = 3.0   # si el servidor no contesta en 3 s, el cliente de prueba se rinde
READ_BYTES = 4096


def find_server_pid(port: int) -> int:
    """El proceso que ESCUCHA en ese puerto (por el inodo del socket, no por el nombre: un
    nombre puede coincidir con otro proceso, o no coincidir con el servidor; bitácora, entrada 6)."""
    pid = procinfo.find_server_pid(port)
    if pid is None:
        sys.exit(f"Nadie escucha en el puerto {port}: ¿está corriendo el servidor?")
    return pid


def count_fds(pid: int) -> int:
    return len(os.listdir(f"/proc/{pid}/fd"))


def count_server_side(port: int) -> dict[str, int]:
    """Cuenta conexiones cuyo extremo LOCAL es el puerto del servidor."""
    counts = {"ESTABLISHED": 0, "CLOSE_WAIT": 0}
    with open("/proc/net/tcp") as f:
        next(f)  # cabecera
        for line in f:
            cols = line.split()
            local_port = int(cols[1].split(":")[1], 16)
            if local_port != port:
                continue
            if cols[3] == TCP_ESTABLISHED:
                counts["ESTABLISHED"] += 1
            elif cols[3] == TCP_CLOSE_WAIT:
                counts["CLOSE_WAIT"] += 1
    return counts


def ws_frame(opcode: int, payload: bytes) -> bytes:
    """Trama del cliente: la máscara es OBLIGATORIA (RFC 6455 §5.3)."""
    if len(payload) >= 126:
        raise ValueError("este script solo envía tramas cortas")
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + masked


def connect(port: int) -> socket.socket:
    """Conecta y hace el saludo HTTP de WebSocket."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=SOCKET_TIMEOUT)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (f"GET /ws HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
         f"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
         f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
    if b" 101 " not in sock.recv(READ_BYTES).split(b"\r\n")[0]:
        raise RuntimeError("el servidor no respondió 101")
    return sock


# Cada escenario es una función; el registro evita un if/elif por tipo.
def clean_close(sock: socket.socket) -> None:
    sock.sendall(ws_frame(OP_TEXT, b'{"t":"hello","name":"Ana"}'))
    sock.recv(READ_BYTES)
    sock.sendall(ws_frame(OP_CLOSE, struct.pack("!H", 1000)))


def abrupt_close(sock: socket.socket) -> None:
    sock.sendall(ws_frame(OP_TEXT, b'{"t":"hello","name":"Bea"}'))
    sock.recv(READ_BYTES)
    # RST en vez de FIN: SO_LINGER con 0 s hace que close() aborte la conexión.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))


def bad_message(sock: socket.socket) -> None:
    sock.sendall(ws_frame(OP_TEXT, b"esto no es json"))
    sock.recv(READ_BYTES)


def no_hello(sock: socket.socket) -> None:
    sock.sendall(ws_frame(OP_TEXT, b'{"t":"input","dx":1,"dy":0}'))
    sock.recv(READ_BYTES)


SCENARIOS = [clean_close, abrupt_close, bad_message, no_hello]


def run_client(index: int, port: int, errors: list[str]) -> None:
    scenario = SCENARIOS[index % len(SCENARIOS)]
    try:
        sock = connect(port)
    except OSError as exc:
        errors.append(f"cliente {index}: no conectó ({exc})")
        return
    try:
        scenario(sock)
    except OSError as exc:
        errors.append(f"cliente {index} ({scenario.__name__}): {exc}")
    finally:
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--clients", type=int, default=50)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    pid = find_server_pid(args.port)
    fds_before = count_fds(pid)
    side_before = count_server_side(args.port)
    print(f"Servidor pid={pid}  fds antes={fds_before}  conexiones antes={side_before}")

    errors: list[str] = []
    threads = [threading.Thread(target=run_client, args=(i, args.port, errors))
               for i in range(args.clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"{args.clients} clientes abiertos y cerrados "
          f"({len(SCENARIOS)} tipos: {', '.join(s.__name__ for s in SCENARIOS)})")

    time.sleep(SETTLE_SECONDS)
    fds_after = count_fds(pid)
    side_after = count_server_side(args.port)
    print(f"fds después={fds_after}  conexiones después={side_after}")

    problems = list(errors)
    if fds_after > fds_before:
        problems.append(f"descriptores: {fds_before} -> {fds_after} (posible fuga)")
    if side_after["CLOSE_WAIT"] > side_before["CLOSE_WAIT"]:
        problems.append("hay conexiones en CLOSE_WAIT: el servidor no cerró su lado")
    if side_after["ESTABLISHED"] > side_before["ESTABLISHED"]:
        problems.append("quedan conexiones ESTABLISHED tras cerrar todos los clientes")

    if problems:
        print("\nFALLO:")
        for p in problems:
            print("  -", p)
        return 1
    print("\nOK: sin fugas de descriptores ni conexiones colgadas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
