"""Utilidades de socket compartidas por los adaptadores."""

import asyncio
import socket
import struct

# struct linger { int l_onoff; int l_linger; }: activado con 0 s de espera.
_LINGER_RST = struct.pack("ii", 1, 0)


def abort_with_reset(writer: asyncio.StreamWriter) -> None:
    """Corta la conexión YA y avisa al otro extremo con un RST.

    `transport.abort()` por sí solo solo hace close() del socket, y close()
    envía un FIN, que TCP entrega DESPUÉS de los datos que aún no salieron. Con
    un cliente que no lee (ventana cero) ese FIN nunca sale: el descriptor se
    libera, pero el socket queda en FIN-WAIT-1 reteniendo su búfer en el kernel
    hasta que venzan los reintentos de TCP (minutos). Verificado con `ss -tan`.
    SO_LINGER con 0 s hace que close() descarte lo pendiente y mande RST.
    """
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _LINGER_RST)
        except OSError:
            pass   # el socket ya estaba cerrado: abort() de abajo es inofensivo
    writer.transport.abort()
