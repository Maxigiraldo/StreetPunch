"""GameSession: el tick. ÚNICO escritor del estado del juego (AGENTS.md §4).

Cada tick:
  1. Ejecuta los comandos de membresía (Connect/Disconnect).
  2. Ejecuta las entradas, como máximo MAX_INPUTS_PER_CLIENT_PER_TICK por cliente.
  3. Avanza la fase de la ronda (patrón State).
  4. Codifica el estado UNA vez y deja ese mismo objeto bytes en el buzón de
     cada observador que toque según su política (Strategy).

tick() es síncrono: no tiene ningún await. Por eso nada puede intercalarse
mientras modifica el mundo o recorre la lista de observadores, y por eso se
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
from server.domain.commands import Command
from server.domain.round_states import RoundState
from server.domain.world import World
from server.metrics import Metrics

log = logging.getLogger(__name__)


class GameSession:
    def __init__(self, config: Config, world: World, initial_state: RoundState,
                 encoder: SnapshotEncoder, policies: dict[str, SendPolicy],
                 metrics: Metrics) -> None:
        self._cfg = config
        self.world = world
        self._state = initial_state
        self._encoder = encoder
        self._policies = policies
        self._metrics = metrics
        self._tick = 0
        self._observers: dict[int, SnapshotObserver] = {}
        # Verdadero si entró o salió alguien desde el último roster enviado.
        self._roster_dirty = False

        # Cola productor-consumidor: N readers producen y el tick consume.
        # Acotada: si un cliente inunda, la memoria no crece; se descarta.
        self._inputs: asyncio.Queue[tuple[int, Command]] = asyncio.Queue(config.INPUT_QUEUE_SIZE)
        # Membresía: nunca descarta (ver session_commands.py). deque.append
        # sin await en medio es atómico en un solo hilo.
        self._membership: deque[MembershipCommand] = deque()

    # ------------- lo que llaman las ClientSession (otras tareas) -------------

    def submit_input(self, player_id: int, command: Command) -> bool:
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

    def subscribe(self, player_id: int, observer: SnapshotObserver) -> None:
        self._observers[player_id] = observer
        self._roster_dirty = True
        player = self.world.players[player_id]
        welcome = WelcomeInfo(player.id, player.color, self.world.role_of(player_id),
                              self._cfg.ARENA_RADIUS, self._cfg.DISC_RADIUS)
        # Se publica en el MISMO tick en que se suscribe, antes del estado de
        # este tick: el writer del cliente siempre ve el welcome primero.
        self._notify(observer, observer.on_welcome, self._encoder.encode_welcome(welcome))

    def unsubscribe(self, player_id: int) -> None:
        self._observers.pop(player_id, None)
        self._roster_dirty = True

    def stats(self) -> dict:
        """Lectura para /metrics. Solo lee y no hay await: ve un estado
        coherente (el tick no puede correr a la mitad)."""
        playing = sum(1 for pid in self._observers if self.world.is_playing(pid))
        return {
            "tick": self._tick,
            "phase": self._state.PHASE,
            "subscribed": len(self._observers),
            "playing": playing,
            "spectators": len(self._observers) - playing,
        }

    # ------------- el tick -------------

    def tick(self) -> None:
        self._tick += 1
        self._apply_membership()
        self._apply_inputs()
        self._state = self._state.on_tick(self.world)
        self._publish_roster_if_changed()
        self._publish()

    def _publish_roster_if_changed(self) -> None:
        """El roster (id, nombre, color de TODOS) solo se envía cuando alguien
        entra o sale, no cada tick: nombres y colores no cambian durante la
        sesión. Va a todos los observadores sin importar su política de
        envío, y completo cada vez (un roster nuevo reemplaza al anterior, así
        que si el buzón de un cliente lento pisa uno, no se pierde nada)."""
        if not self._roster_dirty:
            return
        self._roster_dirty = False
        entries = tuple(RosterEntry(p.id, p.name, p.color) for p in self.world.players.values())
        data = self._encoder.encode_roster(entries)   # UNA vez, como el estado
        for observer in self._observers.values():
            self._notify(observer, observer.on_roster, data)

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
            command.execute(self.world)
            self._metrics.inputs_applied += 1

    def _snapshot(self) -> Snapshot:
        cfg = self._cfg
        discs = tuple(
            DiscView(d.id, d.pos.x, d.pos.y, d.charge_time / cfg.CHARGE_MAX_S, d.alive)
            for d in self.world.discs.values()
        )
        return Snapshot(self._tick, self._state.PHASE, self._state.remaining_s,
                        self._state.winner, dict(self.world.scores), discs)

    def _publish(self) -> None:
        data = self._encoder.encode_state(self._snapshot())   # UNA vez por tick
        # Se recorre el dict sin riesgo de "dictionary changed size during
        # iteration": solo el tick lo modifica, y on_snapshot no hace await.
        for player_id, observer in self._observers.items():
            policy = self._policies[self.world.role_of(player_id)]
            if policy.should_send(self._tick):
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
