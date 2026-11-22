"""Saludo HTTP: sirve el cliente web y hace el upgrade a WebSocket (RFC 6455 §4).

Separado en dos partes para poder probarlo sin sockets:
- Funciones puras: parse_request() y HttpHandshake.respond() transforman bytes
  en bytes.
- read_head() y HttpHandshake.handle(): la parte asíncrona que lee y escribe
  en el stream.

Antes de este saludo todavía no existe la tarea writer del cliente, así que
handle() es el único que escribe en el socket en ese momento: la regla de un
solo escritor por socket se mantiene.
"""

import asyncio
import base64
import binascii
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus

# GUID fijo del RFC 6455 §1.3. El servidor lo concatena a la clave del cliente
# para demostrar que entiende WebSocket (un servidor HTTP cualquiera no
# sabría calcular la respuesta).
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WS_VERSION = "13"
_WS_KEY_BYTES = 16          # §4.1: la clave son 16 bytes aleatorios en base64
_HEAD_END = b"\r\n\r\n"     # fin de la cabecera HTTP


class HttpError(Exception):
    def __init__(self, status: HTTPStatus, extra_headers: dict[str, str] | None = None) -> None:
        super().__init__(status.phrase)
        self.status = status
        self.extra_headers = extra_headers or {}


@dataclass(frozen=True)
class HttpRequest:
    method: str
    path: str
    version: str
    headers: dict[str, str]     # nombres en minúsculas: HTTP no distingue mayúsculas


@dataclass(frozen=True)
class StaticFile:
    content: bytes
    content_type: str


@dataclass(frozen=True)
class HandshakeResult:
    upgraded: bool
    # Bytes que llegaron DESPUÉS de la cabecera en el mismo read(). Si hubo
    # upgrade, pueden ser el comienzo de la primera trama WebSocket: hay que
    # dárselos al FrameReader, o esa trama se pierde.
    leftover: bytes = b""


def accept_key(client_key: str) -> str:
    """Sec-WebSocket-Accept = base64(SHA-1(clave + GUID)) (§4.2.2)."""
    digest = hashlib.sha1((client_key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def parse_request(head: bytes) -> HttpRequest:
    """Interpreta la cabecera completa (sin el \\r\\n\\r\\n final)."""
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError:
        raise HttpError(HTTPStatus.BAD_REQUEST) from None
    request_line, *header_lines = text.split("\r\n")
    parts = request_line.split(" ")
    if len(parts) != 3:
        raise HttpError(HTTPStatus.BAD_REQUEST)
    method, target, version = parts
    if not version.startswith("HTTP/"):
        raise HttpError(HTTPStatus.BAD_REQUEST)
    headers: dict[str, str] = {}
    for line in header_lines:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            raise HttpError(HTTPStatus.BAD_REQUEST)
        headers[name.lower()] = value.strip()
    path = target.split("?", 1)[0]   # la query no se usa
    return HttpRequest(method, path, version, headers)


def _header_has_token(value: str, token: str) -> bool:
    """'Connection: keep-alive, Upgrade' contiene el token 'upgrade'."""
    return token in (t.strip().lower() for t in value.split(","))


def _response(status: HTTPStatus, headers: dict[str, str], body: bytes = b"") -> bytes:
    lines = [f"HTTP/1.1 {status.value} {status.phrase}"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def error_response(err: HttpError) -> bytes:
    body = f"{err.status.value} {err.status.phrase}\n".encode("ascii")
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Length": str(len(body)),
        "Connection": "close",
        **err.extra_headers,
    }
    return _response(err.status, headers, body)


async def read_head(reader: asyncio.StreamReader, max_bytes: int, chunk: int) -> tuple[bytes, bytes]:
    """Lee hasta el fin de la cabecera. Devuelve (cabecera, sobrante).

    No se usa readuntil(): su límite es el del StreamReader entero y no el de
    la cabecera. Aquí se acumula a mano, igual que en el FrameReader, porque
    un read() puede traer media cabecera o la cabecera y algo más.
    """
    buf = bytearray()
    while True:
        end = buf.find(_HEAD_END)
        # El tope se aplica a la cabecera, no al búfer: si un solo read()
        # trae 12 KB que incluyen el fin, la cabecera sigue siendo demasiado
        # grande aunque esté "completa". Como el búfer crece de a `chunk`, la
        # memoria queda acotada en max_bytes + chunk.
        if (end if end != -1 else len(buf)) > max_bytes:
            raise HttpError(HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE)
        if end != -1:
            return bytes(buf[:end]), bytes(buf[end + len(_HEAD_END):])
        data = await reader.read(chunk)
        if not data:
            raise ConnectionError("el cliente cerró durante el saludo")
        buf += data


class HttpHandshake:
    def __init__(self, static_files: dict[str, StaticFile], ws_path: str,
                 max_header_bytes: int, read_chunk: int,
                 dynamic: dict[str, Callable[[], StaticFile]] | None = None) -> None:
        # Solo se sirven las rutas de este diccionario, ya cargado en memoria
        # al arrancar: no hay E/S de disco en el event loop ni forma de pedir
        # "../../etc/passwd" (path traversal).
        self._static = static_files
        self._ws_path = ws_path
        # Rutas cuyo contenido se calcula en cada petición (p. ej. /metrics).
        # Son funciones sin await: no bloquean el event loop.
        self._dynamic = dynamic or {}
        self._max_header_bytes = max_header_bytes
        self._read_chunk = read_chunk

    def respond(self, req: HttpRequest) -> tuple[bytes, bool]:
        """Decide la respuesta. Devuelve (bytes a enviar, ¿hubo upgrade?).
        Lanza HttpError si la petición no es válida."""
        if req.method != "GET":
            raise HttpError(HTTPStatus.METHOD_NOT_ALLOWED, {"Allow": "GET"})
        if req.path == self._ws_path:
            return self._upgrade(req), True
        make = self._dynamic.get(req.path)
        static = make() if make is not None else self._static.get(req.path)
        if static is None:
            raise HttpError(HTTPStatus.NOT_FOUND)
        headers = {
            "Content-Type": static.content_type,
            "Content-Length": str(len(static.content)),
            "Cache-Control": "no-cache",
            "Connection": "close",
        }
        return _response(HTTPStatus.OK, headers, static.content), False

    def _upgrade(self, req: HttpRequest) -> bytes:
        h = req.headers
        # Requisitos de §4.2.1 para aceptar el upgrade.
        if req.version != "HTTP/1.1" or "host" not in h:
            raise HttpError(HTTPStatus.BAD_REQUEST)
        if h.get("upgrade", "").lower() != "websocket":
            raise HttpError(HTTPStatus.BAD_REQUEST)
        if not _header_has_token(h.get("connection", ""), "upgrade"):
            raise HttpError(HTTPStatus.BAD_REQUEST)
        if h.get("sec-websocket-version") != _WS_VERSION:
            # §4.4: se responde 426 indicando la versión que sí hablamos.
            raise HttpError(HTTPStatus.UPGRADE_REQUIRED, {"Sec-WebSocket-Version": _WS_VERSION})
        key = h.get("sec-websocket-key", "")
        try:
            if len(base64.b64decode(key, validate=True)) != _WS_KEY_BYTES:
                raise HttpError(HTTPStatus.BAD_REQUEST)
        except binascii.Error:
            raise HttpError(HTTPStatus.BAD_REQUEST) from None
        return _response(HTTPStatus.SWITCHING_PROTOCOLS, {
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Accept": accept_key(key),
        })

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> HandshakeResult:
        """Lee la petición, responde y dice si la conexión pasó a WebSocket.

        Si no hubo upgrade (página servida o error), quien llama debe cerrar
        la conexión. El timeout del saludo (HANDSHAKE_TIMEOUT_S) lo pone quien
        llama, envolviendo esta corrutina (paso P6).
        """
        try:
            head, leftover = await read_head(reader, self._max_header_bytes, self._read_chunk)
            response, upgraded = self.respond(parse_request(head))
        except HttpError as err:
            response, upgraded, leftover = error_response(err), False, b""
        writer.write(response)    # una sola llamada a write() con la respuesta completa
        await writer.drain()      # aquí sí se puede esperar: es la tarea de ESTE cliente, no el tick
        return HandshakeResult(upgraded, leftover if upgraded else b"")
