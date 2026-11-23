"""Clientes lentos: conectan, saludan y dejan de leer (o leen a cuentagotas).

Uso (con el servidor corriendo; mira `tools/monitor.py` a la vez):
    .venv/bin/python tools/slow_client.py [--mode stall|trickle] [--count 1] [--seconds 60]
                                          [--rcvbuf 1024] [--port 8080] [--keepalive]

Qué demuestra (RNF-06, backpressure): un cliente que no lee NO debe frenar al
servidor ni a los demás.
  1. El socket del servidor hacia él se va llenando (el kernel guarda unos KB
     por conexión; `--rcvbuf` pequeño lo acelera).
  2. Cuando se llena, el `drain()` de SU writer se queda esperando. El tick
     sigue y reemplaza el estado en su buzón: sube `mailbox_overwrites`
     (columna `ovw/s` del monitor).
  3. Pasados WRITE_TIMEOUT_S sin avanzar, el servidor lo corta: sube
     `write_timeouts` (columna `to`) y se libera su descriptor y su cupo.
Mientras tanto, el tick p50/p99 y la latencia de los demás no deben moverse.

Al final compara /metrics antes y después.
"""

import argparse
import asyncio
import os
import socket
import time

from procinfo import fetch_metrics
from wslite import OP_PING, HandshakeFailed, WsConn, connect

TRICKLE_BYTES = 256          # cuánto lee por segundo en modo trickle
CHECK_EVERY_S = 1.0
KEEPALIVE_EVERY_S = 5.0      # con --keepalive: un ping vacío cada tanto (no lee nada)
WATCHED = ("mailbox_overwrites", "write_timeouts", "idle_timeouts", "bytes_sent")


async def lazy_client(index: int, args: argparse.Namespace, fates: list[str]) -> None:
    try:
        conn: WsConn = await connect(args.port, rcvbuf=args.rcvbuf)
    except (OSError, HandshakeFailed, asyncio.TimeoutError) as err:
        fates.append(f"#{index}: no pudo conectar ({err})")
        return
    transport = conn.writer.transport
    await conn.send_json({"t": "hello", "name": f"lento{index}"})
    started = time.monotonic()
    # Un socket propio (dup del descriptor) para mirar si el servidor nos cortó, SIN enviar
    # nada: cualquier byte que mandáramos renovaría el plazo de inactividad del servidor
    # y lo engañaría (el servidor cree "vivo" a quien le habla).
    probe = socket.socket(fileno=os.dup(transport.get_extra_info("socket").fileno()))
    transport.pause_reading()                       # a partir de aquí el kernel acumula sin que lo leamos
    cut_at = None
    next_keepalive = started
    while time.monotonic() - started < args.seconds:
        await asyncio.sleep(CHECK_EVERY_S)
        if args.mode == "trickle":
            transport.resume_reading()
            try:
                await asyncio.wait_for(conn.reader.read(TRICKLE_BYTES), CHECK_EVERY_S)
            except asyncio.TimeoutError:
                pass
            transport.pause_reading()
        if args.keepalive and time.monotonic() >= next_keepalive:
            # Enviar renueva el plazo de inactividad del servidor SIN leer nada: es
            # el zombi "vivo" que hace falta para probar la versión mala de P8
            # (sin esto el servidor bueno lo expulsa a los 30 s por inactividad).
            next_keepalive = time.monotonic() + KEEPALIVE_EVERY_S
            await conn.send(OP_PING, b"")
        if peer_cut(probe):
            cut_at = time.monotonic() - started
            break
    fates.append(f"#{index}: " + (f"el servidor lo cortó a los {cut_at:.0f} s" if cut_at is not None
                                  else f"seguía conectado a los {args.seconds:.0f} s"))
    probe.close()
    conn.abort()


TCP_ESTABLISHED = 1      # primer byte de TCP_INFO: estado de la conexión


def peer_cut(probe: socket.socket) -> bool:
    """True si la conexión ya no está ESTABLISHED (el servidor la cerró o la abortó).

    Se mira el estado TCP (TCP_INFO) y no se intenta leer: tras un RST, Linux
    conserva los datos que ya habían llegado y un recv(MSG_PEEK) los seguiría
    viendo, como si la conexión estuviera viva."""
    return probe.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 1)[0] != TCP_ESTABLISHED


async def main() -> None:
    parser = argparse.ArgumentParser(description="Clientes lentos para StreetPunch")
    parser.add_argument("--mode", choices=("stall", "trickle"), default="stall",
                        help="stall: no lee nada; trickle: lee 256 bytes por segundo")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--rcvbuf", type=int, default=1024, help="SO_RCVBUF en bytes (pequeño = se llena antes)")
    parser.add_argument("--keepalive", action="store_true",
                        help="manda un ping vacío cada 5 s para que el servidor no lo expulse por inactividad")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    before, _ = fetch_metrics(args.port)
    print(f"{args.count} cliente(s) {args.mode} durante {args.seconds:.0f} s (SO_RCVBUF={args.rcvbuf})")
    fates: list[str] = []
    await asyncio.gather(*(lazy_client(i, args, fates) for i in range(args.count)))
    await asyncio.sleep(1.0)          # margen para que el servidor termine de limpiar
    after, _ = fetch_metrics(args.port)

    for fate in sorted(fates):
        print(" ", fate)
    print("\nEl servidor, antes -> después:")
    for key in WATCHED:
        print(f"  {key:<20} {before[key]:>10} -> {after[key]:<10} (+{after[key] - before[key]})")
    print(f"  {'conexiones activas':<20} {before['connections_active']:>10} -> {after['connections_active']}")
    print(f"  {'tick p99 (ms)':<20} {before['tick_ms']['p99']:>10} -> {after['tick_ms']['p99']}")
    print(f"  {'tick overruns':<20} {before['tick_overruns']:>10} -> {after['tick_overruns']}")


if __name__ == "__main__":
    asyncio.run(main())
