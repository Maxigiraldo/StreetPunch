"""GameSession: el tick. ÚNICO escritor del estado del juego (AGENTS.md §4).

Cada tick:
  1. Ejecuta los comandos de membresía (Connect/Disconnect).
  2. Ejecuta las entradas (moverse, cargar, iniciar, crear/unirse/salir de sala),
     como máximo MAX_INPUTS_PER_CLIENT_PER_TICK por cliente.
  3. Avanza la fase de CADA sala (patrón State).
  4. Avisa a cada cliente de lo suyo: rechazos, cambio de sala, lista de salas
     (vestíbulo), lista de jugadores (sala) y estado (sala). Cada mensaje se
     codifica UNA vez y ese mismo objeto bytes se entrega a todos sus
     destinatarios.

tick() es síncrono: no tiene ningún await. Por eso nada puede intercalarse
mientras modifica el vestíbulo o recorre la lista de observadores, y por eso se
puede probar con pytest llamándolo directamente, sin event loop ni sockets.
El único await del bucle es el asyncio.sleep() ENTRE ticks, nunca sobre un socket.
"""

import asyncio
import logging
from collections import deque

from server.application.ports import (
    DiscView, RosterEntry, Snapshot, SnapshotEncoder, SnapshotObserver, WelcomeInfo,
)
from server.application.send_policies import SendPolicy
from server.application.session_commands import MembershipCommand
from server.config import Config
from server.domain.commands import Command, HallCommand
from server.domain.hall import Hall
from server.domain.room import Room
from server.metrics import Metrics

log = logging.getLogger(__name__)


class GameSession:
    def __init__(self, config: Config, hall: Hall, encoder: SnapshotEncoder,
                 policies: dict[str, SendPolicy], metrics: Metrics) -> None:
        self._cfg = config
        self.hall = hall
        self._encoder = encoder
        self._policies = policies          # {fase: política de envío}
        self._metrics = metrics
        self._tick = 0
        self._observers: dict[int, SnapshotObserver] = {}
        # Última sala de la que se AVISÓ a cada cliente (None = vestíbulo). El
        # tick compara con la real y manda `room` solo si cambió: no hace falta
        # que cada comando se acuerde de avisar.
        self._announced_room: dict[int, int | None] = {}
        # Lista de salas ya enviada a cada cliente del vestíbulo, por IDENTIDAD
        # del objeto bytes: se codifica una vez por cambio y se compara con `is`.
        self._rooms_sent: dict[int, bytes | None] = {}
        self._rooms_data: bytes = b""
        # None (no `()`): con cero salas el resumen vacío coincidiría con el inicial y
        # el primer cliente recibiría b"" en vez de la lista.
        self._rooms_summary: tuple | None = None
        # Último roster enviado por sala: (ids en orden de llegada, anfitrión).
        self._last_roster: dict[int, tuple[tuple[int, ...], int | None]] = {}

        # Cola productor-consumidor: N readers producen y el tick consume.
        # Acotada: si un cliente inunda, la memoria no crece; se descarta.
        self._inputs: asyncio.Queue[tuple[int, Command | HallCommand]] = asyncio.Queue(config.INPUT_QUEUE_SIZE)
        # Membresía: nunca descarta (ver session_commands.py). deque.append
        # sin await en medio es atómico en un solo hilo.
        self._membership: deque[MembershipCommand] = deque()

    # ------------- lo que llaman las ClientSession (otras tareas) -------------

    def submit_input(self, player_id: int, command: Command | HallCommand) -> bool:
        """Nunca bloquea: put_nowait. Devuelve False si se descartó."""
        try:
            self._inputs.put_nowait((player_id, command))
            return True
        except asyncio.QueueFull:
            self._metrics.inputs_dropped_queue += 1
            return False

    def submit_membership(self, command: MembershipCommand) -> None:
        self._membership.append(command)

    # ------------- lo que llaman los comandos (dentro del tick) -------------

    def subscribe(self, player_id: int, name: str, observer: SnapshotObserver) -> None:
        self.hall.connect(player_id, name)
        self._observers[player_id] = observer
        self._announced_room[player_id] = None     # empieza en el vestíbulo
        self._rooms_sent[player_id] = None
        welcome = WelcomeInfo(player_id, self.hall.color_of(player_id),
                              self._cfg.ARENA_RADIUS, self._cfg.DISC_RADIUS,
                              self._cfg.MAX_ROOM_PLAYERS, self._cfg.DASH_COOLDOWN_S,
                              self._cfg.MIN_CHARGE_S / self._cfg.CHARGE_MAX_S,
                              self._cfg.MIN_PLAYERS_TO_START)
        # Se publica en el MISMO tick en que se suscribe, antes de cualquier
        # otro mensaje: el writer del cliente siempre ve el welcome primero.
        self._notify(observer, observer.on_welcome, self._encoder.encode_welcome(welcome))

    def unsubscribe(self, player_id: int) -> None:
        self.hall.disconnect(player_id)
        self._observers.pop(player_id, None)
        self._announced_room.pop(player_id, None)
        self._rooms_sent.pop(player_id, None)

    def stats(self) -> dict:
        """Lectura para /metrics. Solo lee y no hay await: ve un estado
        coherente (el tick no puede correr a la mitad)."""
        rooms = self.hall.rooms.values()
        playing = sum(r.world.player_count() for r in rooms if r.phase == "playing")
        in_rooms = sum(r.world.player_count() for r in rooms)
        return {
            "tick": self._tick,
            "rooms": len(self.hall.rooms),
            "subscribed": len(self._observers),
            "playing": playing,
            "in_rooms": in_rooms,
            "lobby": len(self._observers) - in_rooms,
        }

    # ------------- el tick -------------

    def tick(self) -> None:
        self._tick += 1
        self._apply_membership()
        self._apply_inputs()
        self.hall.step()                 # la fase de cada sala
        self._publish_notices()
        self._publish_rooms_if_changed()
        self._publish_rosters_if_changed()
        self._publish_states()

    def _apply_membership(self) -> None:
        while self._membership:
            self._membership.popleft().execute(self)

    def _apply_inputs(self) -> None:
        # Se vacía la cola completa. Mientras tanto no puede llegar nada
        # nuevo, porque aquí no hay ningún await.
        per_player: dict[int, int] = {}
        while True:
            try:
                player_id, command = self._inputs.get_nowait()
            except asyncio.QueueEmpty:
                return
            count = per_player.get(player_id, 0) + 1
            per_player[player_id] = count
            if count > self._cfg.MAX_INPUTS_PER_CLIENT_PER_TICK:
                self._metrics.inputs_dropped_rate += 1
                continue
            command.run(self.hall)       # doble despacho: sala o vestíbulo, el tick no distingue
            self._metrics.inputs_applied += 1

    def _publish_notices(self) -> None:
        """Rechazos (sala llena...) y cambios de sala. Van por la casilla de avisos
        del cliente: reemplaza al anterior, y el último aviso es el que importa."""
        for player_id, code in self.hall.drain_rejections():
            observer = self._observers.get(player_id)
            if observer is not None:
                self._notify(observer, observer.on_notice, self._encoder.encode_error(code))
        for player_id, observer in self._observers.items():
            room = self.hall.room_of(player_id)
            room_id = room.id if room else None
            if self._announced_room.get(player_id) != room_id:
                self._announced_room[player_id] = room_id
                self._rooms_sent[player_id] = None          # al volver al vestíbulo necesita la lista
                self._notify(observer, observer.on_notice, self._encoder.encode_room(room_id))

    def _publish_rooms_if_changed(self) -> None:
        """Lista de salas para quienes están en el vestíbulo. Se codifica solo
        cuando cambia, y cada cliente la recibe una vez por cambio (o al llegar)."""
        summary = self.hall.summary()
        if summary != self._rooms_summary:
            self._rooms_summary = summary
            self._rooms_data = self._encoder.encode_rooms(summary)
        for player_id in self.hall.lobby_members():
            if self._rooms_sent.get(player_id) is not self._rooms_data:
                observer = self._observers.get(player_id)
                if observer is not None:
                    self._rooms_sent[player_id] = self._rooms_data
                    self._notify(observer, observer.on_rooms, self._rooms_data)

    def _publish_rosters_if_changed(self) -> None:
        """El roster de cada sala (id, nombre, color de sus jugadores y su
        anfitrión) solo se envía cuando cambia la lista o el anfitrión. Nombres
        y colores no cambian durante la sesión. Va completo cada vez (uno
        nuevo reemplaza al anterior: si el buzón de un cliente lento pisa uno,
        no se pierde nada)."""
        for room in self.hall.rooms.values():
            players = room.world.players
            key = (tuple(players), room.world.host())
            if self._last_roster.get(room.id) == key:
                continue
            self._last_roster[room.id] = key
            entries = tuple(RosterEntry(p.id, p.name, p.color) for p in players.values())
            data = self._encoder.encode_roster(entries, key[1])   # UNA vez, como el estado
            for player_id in players:
                if (observer := self._observers.get(player_id)) is not None:
                    self._notify(observer, observer.on_roster, data)
        for room_id in set(self._last_roster) - set(self.hall.rooms):
            del self._last_roster[room_id]                        # la sala ya no existe

    def _snapshot(self, room: Room) -> Snapshot:
        cfg = self._cfg
        world = room.world
        discs = tuple(
            DiscView(d.id, d.pos.x, d.pos.y, d.charge_time / cfg.CHARGE_MAX_S, d.alive)
            for d in world.discs.values()
        )
        return Snapshot(self._tick, room.phase, room.state.remaining_s,
                        room.state.winner, dict(world.scores), discs)

    def _publish_states(self) -> None:
        # Se recorre sin riesgo de "dictionary changed size during iteration":
        # solo el tick modifica estas estructuras, y on_snapshot no hace await.
        for room in self.hall.rooms.values():
            if not self._policies[room.phase].should_send(self._tick):
                continue
            data = self._encoder.encode_state(self._snapshot(room))   # UNA vez por sala y tick
            for player_id in room.world.players:
                if (observer := self._observers.get(player_id)) is not None:
                    self._notify(observer, observer.on_snapshot, data)

    @staticmethod
    def _notify(observer: SnapshotObserver, method, data: bytes) -> None:
        # Un observador defectuoso no puede detener la difusión a los demás
        # ni el tick (AGENTS.md §4.9).
        try:
            method(data)
        except Exception:
            log.exception("observador %r falló; se ignora en este tick", observer)

    # ------------- bucle de paso fijo -------------

    async def run_tick_loop(self) -> None:
        """Ejecuta tick() cada TICK_DT segundos, con un reloj monotónico.

        Paso fijo: el próximo tick se programa respecto del anterior
        (next_at += dt) y no respecto de "ahora", para que los pequeños
        retrasos no se acumulen. Si un tick se pasa de su presupuesto, no se
        intenta "recuperar" los ticks perdidos (eso causaría una espiral de
        atraso): se cuenta y se reprograma desde ahora.
        """
        loop = asyncio.get_running_loop()
        dt = self._cfg.TICK_DT
        next_at = loop.time()
        while True:
            started = loop.time()
            try:
                self.tick()
            except Exception:
                # Un bug en la lógica no debe congelar el juego para todos.
                log.exception("fallo dentro del tick %d", self._tick)
            self._metrics.record_tick(loop.time() - started)
            next_at += dt
            delay = next_at - loop.time()
            if delay < 0:
                self._metrics.tick_overruns += 1
                next_at = loop.time()
                delay = 0
            await asyncio.sleep(delay)   # el ÚNICO await: cede el loop a readers y writers
