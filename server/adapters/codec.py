"""Traducción entre JSON y comandos/estados (Factory Method con registro).

Decodificar: el campo `t` elige una fábrica en un diccionario {tipo: fábrica},
sin if/elif por tipo. Un mensaje nuevo es una fábrica más y una línea en el
registro (OCP).

Hay dos registros, según la etapa de la conexión:
- antes de `hello`: solo se acepta `hello`;
- después: `input` y `charge` (y un segundo `hello` es un error).
La ClientSession cambia de codec al entrar; no necesita un `if ya_entré`.

Toda la validación de lo que manda el cliente vive aquí (no confiar en el
cliente, RNF-11). El dominio recibe comandos ya válidos.
"""

import json
from collections.abc import Callable

from server.adapters.ws_frames import CloseCode, FrameWriter
from server.application.ports import RosterEntry, Snapshot, SnapshotEncoder, WelcomeInfo
from server.config import Config
from server.domain.commands import ChargeCommand, Command, JoinCommand, MoveCommand

_AXIS_VALUES = (-1, 0, 1)
_COMPACT = (",", ":")   # JSON sin espacios: menos bytes por tick


class MessageError(Exception):
    """Mensaje inválido: se responde {"t":"error","code":...} y se cierra."""

    def __init__(self, code: str, close_code: CloseCode = CloseCode.POLICY_VIOLATION) -> None:
        super().__init__(code)
        self.code = code
        self.close_code = close_code


Factory = Callable[[dict, int], Command]


class MessageCodec:
    def __init__(self, registry: dict[str, Factory], unknown_code: str) -> None:
        self._registry = registry
        self._unknown_code = unknown_code

    def decode(self, payload: bytes, player_id: int) -> Command:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise MessageError("bad_message", CloseCode.INVALID_PAYLOAD) from None
        try:
            msg = json.loads(text)
        except (ValueError, RecursionError):
            # RecursionError: 4 KB de "[[[[..." superan el límite de recursión
            # del decodificador. No es ValueError; si no se captura, tumba la
            # sesión con una excepción inesperada.
            raise MessageError("bad_message") from None
        if not isinstance(msg, dict):
            raise MessageError("bad_message")
        msg_type = msg.get("t")
        # Sin este chequeo, {"t": [1]} haría que registry.get([1]) lanzara
        # TypeError (una lista no se puede usar como clave de un dict).
        factory = self._registry.get(msg_type) if isinstance(msg_type, str) else None
        if factory is None:
            raise MessageError(self._unknown_code)
        return factory(msg, player_id)


# ---------------- fábricas ----------------

def _is_int(value: object) -> bool:
    # En Python, True es un int (isinstance(True, int) da True): hay que excluirlo.
    return isinstance(value, int) and not isinstance(value, bool)


def _make_hello(max_name_len: int) -> Factory:
    def hello(msg: dict, player_id: int) -> Command:
        name = msg.get("name")
        if not isinstance(name, str):
            raise MessageError("bad_name")
        name = name.strip()
        # isprintable() rechaza saltos de línea y caracteres de control que
        # romperían el dibujo o los logs.
        if not 1 <= len(name) <= max_name_len or not name.isprintable():
            raise MessageError("bad_name")
        return JoinCommand(player_id, name)
    return hello


def _input(msg: dict, player_id: int) -> Command:
    dx, dy = msg.get("dx"), msg.get("dy")
    if not (_is_int(dx) and _is_int(dy) and dx in _AXIS_VALUES and dy in _AXIS_VALUES):
        raise MessageError("bad_message")
    return MoveCommand(player_id, dx, dy)


def _charge(msg: dict, player_id: int) -> Command:
    on = msg.get("on")
    if not isinstance(on, bool):
        raise MessageError("bad_message")
    return ChargeCommand(player_id, on)


def _second_hello(msg: dict, player_id: int) -> Command:
    raise MessageError("not_hello")


def hello_codec(config: Config) -> MessageCodec:
    return MessageCodec({"hello": _make_hello(config.MAX_NAME_LEN)}, unknown_code="not_hello")


def game_codec() -> MessageCodec:
    return MessageCodec({
        "input": _input,
        "charge": _charge,
        "hello": _second_hello,
    }, unknown_code="bad_message")


# ---------------- codificación ----------------

class JsonFrameEncoder(SnapshotEncoder):
    """JSON compacto ya envuelto en trama WebSocket de texto: el tick obtiene
    bytes listos para write() y los comparte entre todos los clientes."""

    def __init__(self, config: Config) -> None:
        self._pos = config.POSITION_DECIMALS
        self._charge = config.CHARGE_DECIMALS

    @staticmethod
    def _frame(obj: dict) -> bytes:
        return FrameWriter.text(json.dumps(obj, separators=_COMPACT).encode("utf-8"))

    def encode_state(self, s: Snapshot) -> bytes:
        remaining_ms = None if s.remaining_s is None else round(s.remaining_s * 1000)
        return self._frame({
            "t": "state",
            "tick": s.tick,
            "phase": s.phase,
            "ms": remaining_ms,
            "winner": s.winner,
            "scores": {str(k): v for k, v in s.scores.items()},
            "d": [[d.id, round(d.x, self._pos), round(d.y, self._pos),
                   round(d.charge, self._charge), int(d.alive)] for d in s.discs],
        })

    def encode_welcome(self, w: WelcomeInfo) -> bytes:
        return self._frame({
            "t": "welcome", "id": w.id, "color": w.color, "role": w.role,
            "arena": {"radius": w.arena_radius, "disc_radius": w.disc_radius},
            "max_players": w.max_players, "dash_cooldown_s": w.dash_cooldown_s,
        })

    def encode_roster(self, entries: tuple[RosterEntry, ...]) -> bytes:
        return self._frame({"t": "roster", "players": [
            {"id": e.id, "name": e.name, "color": e.color} for e in entries]})

    def encode_error(self, code: str) -> bytes:
        return self._frame({"t": "error", "code": code})
