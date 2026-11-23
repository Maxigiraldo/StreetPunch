"use strict";
/*
 * Cliente de StreetPunch (P5a dibujo, P5b teclado, P5c interpolación, P5d pantalla).
 *
 * - Abre el WebSocket, manda `hello` y dibuja los `state` interpolados (~100 ms
 *   en el pasado). El `roster` da nombre y color a cada id.
 * - Envía INTENCIONES (hacia dónde quiere moverse, si carga), nunca posiciones:
 *   el servidor es la autoridad (docs/protocolo.md).
 */

// ---- Constantes visuales (el juego en sí lo define el servidor, no el cliente) ----
const CANVAS_MARGIN_PX = 48;       // aire entre la arena y el borde del canvas: cabe un disco entero
                                   // fuera del borde, que es donde se ve caer (ELIMINATION_MARGIN_DISCS)
const CHARGE_RING_GAP_PX = 5;      // separación entre el disco y su anillo de carga
const CHARGE_RING_WIDTH_PX = 4;    // grosor del anillo de carga
const OWN_OUTLINE_PX = 3;          // borde blanco para reconocer el propio disco
const DEAD_ALPHA = 0.25;           // discos eliminados: se dibujan apagados hasta fin de ronda
const AIM_GAP_FACTOR = 1.15;       // la flecha de puntería nace un poco fuera del disco (en radios)
const AIM_LENGTH_FACTOR = 2.2;     // y llega hasta esta distancia del centro (en radios)
const AIM_WIDTH_PX = 3;            // grosor de la flecha
const AIM_HEAD_PX = 9;             // largo de la punta
const AIM_HEAD_HALF_WIDTH = 0.6;   // media base de la punta, como fracción de su largo
const AIM_IDLE_ALPHA = 0.45;       // tenue mientras no se carga; opaca al cargar
const MS_PER_S = 1000;             // `ms` del servidor -> segundos para mostrar
// Color de un disco cuyo id aún no está en el `roster` (el roster llega en el mismo
// tick en que entra el jugador, pero un `state` puede adelantarse): gris neutro.
const UNKNOWN_COLOR = "#8a8f98";

// ---- Texto sobre el canvas (P5d) ----
const NAME_FONT = "600 13px system-ui, sans-serif";
const NAME_GAP_PX = 8;             // separación entre el borde del disco y su nombre
const NAME_OUTLINE_PX = 3;         // contorno oscuro: el nombre se lee sobre cualquier color
const BIG_FONT = "700 72px system-ui, sans-serif";
const MID_FONT = "700 34px system-ui, sans-serif";
const SMALL_FONT = "500 18px system-ui, sans-serif";
const OVERLAY_LINE_GAP_PX = 44;    // separación entre el texto grande y el pequeño
const OVERLAY_COLOR = "#ffffff";
const OVERLAY_SHADOW = "#000000";
const COOLDOWN_RING_GAP_PX = 9;    // el anillo de enfriamiento va por fuera del de carga
const COOLDOWN_COLOR = "#8a8f98";
const MS_PER_S_FLOAT = 1000;

// ---- Interpolación (P5c) ----
const INTERP_DELAY_MS = 100;       // se dibuja ~100 ms en el pasado (docs/diseno.md, P5)
const INTERVAL_FACTOR = 1.5;       // el retraso nunca baja de 1,5 intervalos entre estados:
                                   // esperando/cuenta atrás llegan cada 100 ms y hacen falta 2 que mezclar
const INTERVAL_SMOOTHING = 0.1;    // peso de cada muestra al promediar el intervalo (media móvil)
const MAX_SNAPSHOTS = 32;          // tope de estados guardados: acota la memoria si el navegador se detiene

// Radios por defecto hasta que llegue el `welcome` con los reales.
const DEFAULT_ARENA = { radius: 300, disc_radius: 24 };

const PHASE_TEXT = {
  waiting: "Esperando jugadores…",
  countdown: "¡Prepárate!",
  playing: "¡A pelear!",
  round_over: "Fin de la ronda",
};
const STATUS_TEXT = { playing: "en juego", eliminated: "eliminado", host: "anfitrión", inroom: "en la sala" };

// Rechazos del servidor que NO cierran la conexión (el cliente puede intentar otra cosa).
const ERROR_TEXT = {
  room_full: "Esa sala está llena. Elige otra o crea una.",
  room_busy: "Esa sala está jugando. Espera a que termine o crea otra.",
  no_room: "Esa sala ya no existe.",
  too_many_rooms: "Hay demasiadas salas. Únete a una.",
  already_in_room: "Ya estás en una sala.",
  not_in_room: "No estás en una sala.",
};
const PHASE_LABEL = { waiting: "en espera", countdown: "empezando", playing: "jugando", round_over: "terminando" };

const els = {
  form: document.getElementById("join"),
  name: document.getElementById("name"),
  enter: document.getElementById("enter"),
  status: document.getElementById("status"),
  hud: document.getElementById("hud"),
  scores: document.getElementById("scores"),
  start: document.getElementById("start"),
  lobby: document.getElementById("lobby"),
  rooms: document.getElementById("rooms"),
  create: document.getElementById("create"),
  leave: document.getElementById("leave"),
  game: document.getElementById("game"),
  canvas: document.getElementById("arena"),
};
const ctx = els.canvas.getContext("2d");

// Todo el estado del cliente en un solo objeto, para no repartir variables sueltas.
const session = {
  socket: null, me: null, color: null, arena: DEFAULT_ARENA, latest: null, phase: null,
  maxPlayers: null,    // tope de jugadores por sala (del welcome)
  room: null,          // id de la sala en que estoy; null = en el vestíbulo
  rooms: [],           // lista de salas (solo llega mientras estoy en el vestíbulo)
  cooldownS: 0,        // enfriamiento de la embestida (del welcome)
  host: null,          // id del anfitrión de la sala (el primero en llegar)
  roster: new Map(),   // id -> { name, color }, tal como lo manda el servidor
};

// Estados recibidos con su hora de LLEGADA (performance.now), para dibujar en el pasado.
// Se usa la hora de llegada y no el `tick` porque el cliente no conoce TICK_HZ.
const timeline = { snapshots: [], interval: null };

function resetTimeline() {
  timeline.snapshots.length = 0;
  timeline.interval = null;
}

function pushSnapshot(state) {
  const now = performance.now();
  const last = timeline.snapshots[timeline.snapshots.length - 1];
  if (last) {
    // Intervalo medio entre estados (media móvil): 33 ms jugando, 100 ms en espera/cuenta atrás.
    const gap = now - last.t;
    timeline.interval = timeline.interval === null
      ? gap
      : timeline.interval + INTERVAL_SMOOTHING * (gap - timeline.interval);
  }
  timeline.snapshots.push({ t: now, state });
  if (timeline.snapshots.length > MAX_SNAPSHOTS) timeline.snapshots.shift();
}

// Al empezar una ronda (cuenta atrás) los discos aparecen en su punto de salida: no es un
// movimiento, es un salto. Mezclar posiciones ahí los haría deslizarse por la arena.
function startsNewRound(before, after) {
  return after.phase === "countdown" && before.phase !== "countdown";
}

function lerp(from, to, alpha) {
  return from + (to - from) * alpha;
}

function lerpDiscs(before, after, alpha) {
  const previous = new Map(before.map((disc) => [disc[0], disc]));
  return after.map((disc) => {
    const old = previous.get(disc[0]);
    if (!old) return disc; // disco que no estaba antes: se dibuja donde está
    const [id, x, y, charge] = disc;
    // `vivo` se toma del estado anterior: un disco sigue vivo en pantalla hasta el estado en que muere.
    return [id, lerp(old[1], x, alpha), lerp(old[2], y, alpha), lerp(old[3], charge, alpha), old[4]];
  });
}

// Discos tal como se ven en el instante `now`: ~100 ms antes de lo último recibido.
function interpolatedDiscs(now) {
  const snaps = timeline.snapshots;
  if (snaps.length === 0) return [];
  const delay = Math.max(INTERP_DELAY_MS, (timeline.interval ?? 0) * INTERVAL_FACTOR);
  const renderTime = now - delay;
  // Se descartan los estados ya superados, dejando el último anterior a renderTime.
  while (snaps.length > 2 && snaps[1].t <= renderTime) snaps.shift();

  const [before, after] = snaps;
  if (!after || renderTime <= before.t) return before.state.d; // aún no hay historia suficiente
  if (renderTime >= after.t) return after.state.d;             // no ha llegado nada más nuevo: se mantiene
  if (startsNewRound(before.state, after.state)) return before.state.d;
  return lerpDiscs(before.state.d, after.state.d, (renderTime - before.t) / (after.t - before.t));
}

// ---------------------------------------------------------------- mensajes

// Registro {tipo: manejador}, igual que el codec del servidor: agregar un
// mensaje nuevo es agregar una entrada, no otro `if`.
const handlers = {
  welcome(msg) {
    session.me = msg.id;
    session.color = msg.color;
    session.arena = msg.arena;
    session.maxPlayers = msg.max_players;
    session.cooldownS = msg.dash_cooldown_s;
    session.minCharge = msg.min_charge;     // fracción de carga bajo la cual soltar no embiste
    session.minPlayers = msg.min_players;
    setStatus(`Conectado como #${msg.id}`);
  },
  rooms(msg) {
    // Siempre llega la lista completa: se reemplaza entera.
    session.rooms = msg.rooms;
    renderRooms();
  },
  room(msg) {
    // Entré a una sala (id) o volví al vestíbulo (null): empieza una "pantalla" nueva y
    // lo de la anterior (estado, jugadores, controles, interpolación) no vale.
    session.room = msg.id;
    resetRoomState();
    showView();
    setStatus(msg.id === null ? "Elige una sala o crea una." : `Estás en la sala ${msg.id}.`);
  },
  roster(msg) {
    // Siempre llega la lista completa: se reemplaza entera, no se mezcla.
    session.host = msg.host;
    session.roster = new Map(msg.players.map((p) => [p.id, { name: p.name, color: p.color }]));
  },
  state(msg) {
    // Solo importa el más reciente: cada `state` se basta a sí mismo
    // (docs/protocolo.md §4), así que descartar los intermedios no pierde nada.
    session.latest = msg;
    pushSnapshot(msg);
    // El cambio de fase se detecta comparando con la anterior: no hay mensaje aparte.
    if (msg.phase !== session.phase) {
      session.phase = msg.phase;
      // La frecuencia de envío cambia con la fase (10 Hz en espera, 30 Hz jugando): se
      // olvida el intervalo medio para que el retraso de interpolación se recalcule.
      timeline.interval = null;
      if (Object.hasOwn(phaseEntered, msg.phase)) phaseEntered[msg.phase]();
    }
  },
  error(msg) {
    setStatus(ERROR_TEXT[msg.code] ?? `El servidor rechazó el mensaje: ${msg.code}`);
  },
};

function onMessage(event) {
  let msg;
  try {
    msg = JSON.parse(event.data);
  } catch {
    return; // el servidor siempre manda JSON; si no lo es, no hay nada útil que hacer
  }
  // hasOwn evita que un `t` como "constructor" encuentre métodos heredados del objeto.
  if (Object.hasOwn(handlers, msg.t)) handlers[msg.t](msg);
  // Un tipo desconocido se ignora: así un servidor más nuevo no rompe a un cliente viejo.
}

// ---------------------------------------------------------------- conexión

// Lo que pertenece a UNA sala: se borra al entrar a otra o al volver al vestíbulo.
function resetRoomState() {
  session.latest = null;
  session.phase = null;
  session.host = null;
  session.roster = new Map();
  lastScoreKey = "";
  lastHudTick = -1;
  resetTimeline();
  resetControls();
  els.start.hidden = true;
  els.scores.replaceChildren();
}

// Vestíbulo (lista de salas) o sala (partida), según dónde esté el jugador.
function showView() {
  const connected = session.socket !== null;
  els.lobby.hidden = !(connected && session.room === null);
  els.game.hidden = !(connected && session.room !== null);
}

// Las salas las crean otros jugadores y sus nombres también: solo textContent, nunca HTML.
function renderRooms() {
  if (session.rooms.length === 0) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "No hay salas todavía. Crea la primera.";
    els.rooms.replaceChildren(empty);
    return;
  }
  els.rooms.replaceChildren(...session.rooms.map((room) => {
    const item = document.createElement("li");
    item.className = "room";
    const title = document.createElement("span");
    title.textContent = `${room.name} · anfitrión ${room.host}`;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = `${room.players}/${room.max} · ${PHASE_LABEL[room.phase] ?? room.phase}`;
    const join = document.createElement("button");
    join.textContent = room.players >= room.max ? "Llena" : "Unirse";
    join.disabled = !room.open;
    join.addEventListener("click", () => send({ t: "join", room: room.id }));
    item.append(title, meta, join);
    return item;
  }));
}

function connect(name) {
  session.me = null;
  session.arena = DEFAULT_ARENA;
  session.maxPlayers = null;
  session.cooldownS = 0;
  session.room = null;
  session.rooms = [];
  resetRoomState();
  els.enter.disabled = true;
  // Sin esto el foco se queda en el campo de nombre y las teclas del juego se
  // escribirían ahí en vez de llegar al juego.
  els.name.blur();

  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/ws`);
  session.socket = socket;
  showView();          // el socket ya está asignado: muestra el vestíbulo en cuanto llegue la lista
  setStatus("Conectando…");

  // `hello` debe ser el primer mensaje (docs/protocolo.md §2).
  socket.addEventListener("open", () => socket.send(JSON.stringify({ t: "hello", name })));
  socket.addEventListener("message", onMessage);
  socket.addEventListener("close", (event) => {
    // 1008 = el servidor rechazó algo (el `error` previo dice qué); se conserva ese texto.
    if (event.code !== 1008) setStatus(`Desconectado (código ${event.code})`);
    session.socket = null;
    session.room = null;
    resetRoomState();
    showView();
    els.enter.disabled = false;
  });
}

// ---------------------------------------------------------------- controles

// `code` es la tecla FÍSICA (no la letra impresa): WASD funciona igual con un
// teclado español, inglés u otro. El canvas dibuja el mundo sin invertir el eje y
// (crece hacia abajo), así que "arriba" es dy = -1.
const MOVE_KEYS = {
  ArrowLeft: [-1, 0], KeyA: [-1, 0],
  ArrowRight: [1, 0], KeyD: [1, 0],
  ArrowUp: [0, -1], KeyW: [0, -1],
  ArrowDown: [0, 1], KeyS: [0, 1],
};
// Varias teclas de carga: muchos teclados no registran Espacio + dos flechas a la vez
// (límite de hardware, "ghosting"); con J o K en la otra mano no hay ese problema.
const CHARGE_CODES = new Set(["Space", "KeyJ", "KeyK"]);

const controls = {
  held: new Set(),                 // teclas de movimiento pulsadas ahora
  intent: { dx: 0, dy: 0 },        // última intención ENVIADA (para no repetirla)
  chargeHeld: new Set(),           // teclas de carga pulsadas ahora (cargas mientras haya alguna)
  charging: false,                 // si se está cargando (alguna tecla de carga pulsada)
  cooldownUntil: 0,                // instante (performance.now) en que vuelve a estar lista la embestida
  // Hacia dónde sale la embestida: la última dirección NO nula enviada, igual que
  // Disc.facing en el servidor. null = no se ha pulsado nada esta ronda, y entonces
  // el servidor apunta al centro de la arena. El servidor no manda su `facing`,
  // así que el cliente lo reproduce con lo que él mismo envió (solo del propio disco).
  aim: null,
};

function send(msg) {
  const socket = session.socket;
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(msg));
}

function currentIntent() {
  let dx = 0;
  let dy = 0;
  for (const code of controls.held) {
    dx += MOVE_KEYS[code][0];
    dy += MOVE_KEYS[code][1];
  }
  // Math.sign deja cada eje en -1, 0 o 1 (lo único que acepta el servidor) y
  // hace que dos teclas opuestas se cancelen o que "flecha + WASD" no sumen doble.
  return { dx: Math.sign(dx), dy: Math.sign(dy) };
}

// Solo se envía cuando la intención CAMBIA. El servidor mantiene la dirección
// hasta que llega otra, y limita a 4 entradas por tick: mandar una por cada
// evento del teclado lo desbordaría.
function syncIntent() {
  const next = currentIntent();
  if (next.dx === controls.intent.dx && next.dy === controls.intent.dy) return;
  controls.intent = next;
  if (next.dx !== 0 || next.dy !== 0) controls.aim = next; // soltar las teclas no cambia la puntería
  send({ t: "input", ...next });
}

function ownCharge() {
  const own = session.latest?.d.find((disc) => disc[0] === session.me);
  return own ? own[3] : 0;
}

function setCharging(on) {
  if (on === controls.charging) return;
  controls.charging = on;
  // Soltar con carga acumulada lanza la embestida y empieza el enfriamiento en el servidor:
  // el cliente lo refleja (aproximado) para que el jugador VEA por qué no puede cargar.
  if (!on && ownCharge() >= (session.minCharge ?? 0) && ownCharge() > 0) controls.cooldownUntil = performance.now() + session.cooldownS * MS_PER_S_FLOAT;
  send({ t: "charge", on });
}

function resetControls() {
  controls.held.clear();
  controls.intent = { dx: 0, dy: 0 };
  controls.chargeHeld.clear();
  controls.charging = false;
  controls.cooldownUntil = 0;
  controls.aim = null;
}

// El servidor crea discos NUEVOS en cada ronda (al empezar la cuenta atrás), con
// la dirección en cero y apuntando al centro. Por eso la puntería se olvida ahí.
function newRound() {
  controls.aim = null;
  controls.cooldownUntil = 0;   // los discos nuevos nacen sin enfriamiento
}

// Si el jugador sigue manteniendo una tecla de la ronda anterior, el cliente cree
// que ya la envió y no la repetiría: se reenvía al empezar la fase de juego.
function resyncControls() {
  if (controls.intent.dx !== 0 || controls.intent.dy !== 0) {
    send({ t: "input", ...controls.intent });
    controls.aim = controls.intent;
  }
  if (controls.charging) send({ t: "charge", on: true });
}
const phaseEntered = { countdown: newRound, playing: resyncControls };

function isTyping(event) {
  return event.target instanceof HTMLInputElement;
}

window.addEventListener("keydown", (event) => {
  if (isTyping(event)) return; // escribiendo el nombre: no es una orden de juego
  const isMove = Object.hasOwn(MOVE_KEYS, event.code);
  if (!isMove && !CHARGE_CODES.has(event.code)) return;
  event.preventDefault();      // evita que flechas y espacio desplacen la página
  if (event.repeat) return;    // la repetición automática del teclado no es una pulsación nueva
  if (isMove) {
    controls.held.add(event.code);
    syncIntent();
  } else {
    controls.chargeHeld.add(event.code);
    setCharging(true);
  }
});

// keyup no mira isTyping a propósito: soltar una tecla siempre debe contar,
// o un disco podría quedarse corriendo si el foco cambió con la tecla pulsada.
window.addEventListener("keyup", (event) => {
  if (Object.hasOwn(MOVE_KEYS, event.code)) {
    controls.held.delete(event.code);
    syncIntent();
  } else if (CHARGE_CODES.has(event.code)) {
    controls.chargeHeld.delete(event.code);
    setCharging(controls.chargeHeld.size > 0); // con otra tecla de carga aún pulsada se sigue cargando
  }
});

// Si la ventana pierde el foco con una tecla pulsada, el navegador no manda su
// keyup: sin esto el disco seguiría corriendo solo. Soltar la carga aquí lanza
// la embestida; es preferible a dejar la carga "pegada" sin control del jugador.
window.addEventListener("blur", () => {
  controls.held.clear();
  controls.chargeHeld.clear();
  syncIntent();
  setCharging(false);
});

els.create.addEventListener("click", () => send({ t: "create" }));
els.leave.addEventListener("click", () => {
  send({ t: "leave" });
  els.leave.blur();   // que Espacio no vuelva a «pulsar» el botón
});

els.start.addEventListener("click", () => {
  send({ t: "start" });
  els.start.blur();   // que Espacio no vuelva a «pulsar» el botón en plena ronda
});

els.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const name = els.name.value.trim();
  if (name && !session.socket) connect(name);
});

// ---------------------------------------------------------------- pantalla

function setStatus(text) {
  els.status.textContent = text; // textContent, nunca innerHTML: no se interpreta nada como HTML
}

function discColor(id) {
  return session.roster.get(id)?.color ?? UNKNOWN_COLOR;
}

function nameOf(id) {
  return session.roster.get(id)?.name ?? `#${id}`;
}

// Se juega si el propio id aparece en `d` (fuera de ronda `d` está vacío: docs/protocolo.md §4).
function amPlaying(state) {
  return state.d.some((disc) => disc[0] === session.me);
}

function describe(state) {
  const parts = [PHASE_TEXT[state.phase] ?? state.phase];
  if (state.ms !== null) parts.push(String(Math.ceil(state.ms / MS_PER_S)));
  if (state.phase === "playing" && amPlaying(state)) {
    parts.push(performance.now() < controls.cooldownUntil ? "· embestida recargando…" : "· embestida lista");
  }
  return parts.join(" ");
}

// Qué decir en el centro de la arena según la fase: [texto grande, texto pequeño].
// Registro por fase (como `handlers`): una fase nueva es una entrada nueva.
const overlayFor = {
  // Sala de espera: la ronda no arranca sola, la inicia el anfitrión (el primero en llegar)
  // cuando ve en la lista que ya están todos.
  waiting: () => {
    const count = session.roster.size;
    const enough = count >= (session.minPlayers ?? 2);
    if (isHost()) {
      return enough
        ? ["Sala de espera", `${count}/${session.maxPlayers} en la sala: pulsa «Iniciar partida» cuando estén todos`]
        : ["Sala de espera", `Eres el anfitrión. Faltan jugadores (mínimo ${session.minPlayers})`];
    }
    return ["Sala de espera", `${nameOf(session.host)} iniciará la partida (${count}/${session.maxPlayers} en la sala)`];
  },
  countdown: (state) => [String(Math.ceil(state.ms / MS_PER_S)), "¡Prepárate!"],
  round_over: (state) => {
    if (state.winner === null) return ["Empate", "Nadie ganó la ronda"];
    return state.winner === session.me
      ? ["¡Ganaste!", "Siguiente ronda en breve"]
      : [`Ganó ${nameOf(state.winner)}`, "Siguiente ronda en breve"];
  },
};

// Filas del marcador: todos los del roster, con sus victorias y su estado en esta ronda.
// Más victorias primero; a igualdad, el orden del roster (el de llegada).
function isHost() {
  return session.me !== null && session.me === session.host;
}

// El botón solo existe para el anfitrión y solo mientras se espera: el servidor ignora
// la orden de cualquier otro, así que esto es comodidad, no seguridad.
function syncStartButton(state) {
  els.start.hidden = !(isHost() && state.phase === "waiting");
}

function scoreRows(state) {
  const inRound = new Map(state.d.map((disc) => [disc[0], disc[4]]));
  const rows = [...session.roster].map(([id, info]) => ({
    id,
    name: info.name,
    color: info.color,
    wins: state.scores[id] ?? 0,
    status: state.phase === "waiting" ? (id === session.host ? "host" : "inroom")
      : !inRound.has(id) ? "inroom" : inRound.get(id) ? "playing" : "eliminated",
    me: id === session.me,
  }));
  return rows.sort((a, b) => b.wins - a.wins);
}

// El marcador es DOM y solo se reconstruye si su contenido cambió: no a 30 Hz.
// Los nombres los eligen otros jugadores: se escriben con textContent, nunca como HTML.
let lastScoreKey = "";
function renderScores(state) {
  const rows = scoreRows(state);
  const key = JSON.stringify(rows);
  if (key === lastScoreKey) return;
  lastScoreKey = key;
  els.scores.replaceChildren(...rows.map((row) => {
    const item = document.createElement("li");
    item.className = `row ${row.status}${row.me ? " me" : ""}`;
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = row.color;
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = row.me ? `${row.name} (tú)` : row.name;
    const wins = document.createElement("span");
    wins.className = "wins";
    wins.textContent = String(row.wins);
    const status = document.createElement("span");
    status.className = "status";
    status.textContent = STATUS_TEXT[row.status];
    item.append(swatch, name, wins, status);
    return item;
  }));
}

// Texto con contorno oscuro: legible sobre la arena, los discos y el fondo.
function drawText(text, x, y, font, color = OVERLAY_COLOR) {
  ctx.font = font;
  ctx.lineJoin = "round";
  ctx.lineWidth = NAME_OUTLINE_PX;
  ctx.strokeStyle = OVERLAY_SHADOW;
  ctx.strokeText(text, x, y);
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
}

function drawOverlay(state, cx, cy) {
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  if (Object.hasOwn(overlayFor, state.phase)) {
    const [big, small] = overlayFor[state.phase](state);
    const isNumber = /^\d+$/.test(big); // las cuentas atrás van grandes; los textos, medianos
    drawText(big, cx, cy, isNumber ? BIG_FONT : MID_FONT);
    drawText(small, cx, cy + OVERLAY_LINE_GAP_PX, SMALL_FONT);
  }
}

// Flecha de puntería del PROPIO disco: hacia dónde saldría la embestida. Sin
// dirección pulsada apunta al centro de la arena, como el servidor al empezar.
function drawAim(px, py, worldX, worldY, r, charging) {
  const aim = controls.aim ?? { dx: -worldX, dy: -worldY };
  const length = Math.hypot(aim.dx, aim.dy);
  if (length === 0) return; // disco exactamente en el centro y sin dirección: nada que dibujar
  const ux = aim.dx / length;
  const uy = aim.dy / length;
  const fromX = px + ux * r * AIM_GAP_FACTOR;
  const fromY = py + uy * r * AIM_GAP_FACTOR;
  const toX = px + ux * r * AIM_LENGTH_FACTOR;
  const toY = py + uy * r * AIM_LENGTH_FACTOR;

  ctx.save();
  ctx.globalAlpha = charging ? 1 : AIM_IDLE_ALPHA;
  ctx.strokeStyle = "#fff";
  ctx.fillStyle = "#fff";
  ctx.lineWidth = AIM_WIDTH_PX;
  ctx.beginPath();
  ctx.moveTo(fromX, fromY);
  ctx.lineTo(toX, toY);
  ctx.stroke();
  // Punta: triángulo con la base perpendicular a la dirección (-uy, ux).
  ctx.beginPath();
  ctx.moveTo(toX + ux * AIM_HEAD_PX, toY + uy * AIM_HEAD_PX);
  ctx.lineTo(toX - uy * AIM_HEAD_PX * AIM_HEAD_HALF_WIDTH, toY + ux * AIM_HEAD_PX * AIM_HEAD_HALF_WIDTH);
  ctx.lineTo(toX + uy * AIM_HEAD_PX * AIM_HEAD_HALF_WIDTH, toY - ux * AIM_HEAD_PX * AIM_HEAD_HALF_WIDTH);
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}

// Anillo gris que se vacía mientras la embestida se recarga: sin él, soltar y volver a
// pulsar enseguida "no hace nada" y parece que los controles fallan.
function drawCooldown(px, py, r, now) {
  const total = session.cooldownS * MS_PER_S_FLOAT;
  const left = controls.cooldownUntil - now;
  if (total <= 0 || left <= 0) return;
  const start = -Math.PI / 2;
  ctx.beginPath();
  ctx.arc(px, py, r + COOLDOWN_RING_GAP_PX, start, start + (left / total) * 2 * Math.PI);
  ctx.strokeStyle = COOLDOWN_COLOR;
  ctx.lineWidth = CHARGE_RING_WIDTH_PX;
  ctx.stroke();
}

function draw(now) {
  const { width, height } = els.canvas;
  const cx = width / 2;
  const cy = height / 2;
  const { radius, disc_radius: discRadius } = session.arena;
  const scale = (width / 2 - CANVAS_MARGIN_PX) / radius; // px por unidad del mundo

  ctx.clearRect(0, 0, width, height);
  ctx.beginPath();
  ctx.arc(cx, cy, radius * scale, 0, 2 * Math.PI);
  ctx.fillStyle = "#1b1e24";
  ctx.fill();
  ctx.strokeStyle = "#555b66";
  ctx.lineWidth = 2;
  ctx.stroke();

  if (!session.latest) return;
  for (const [id, x, y, charge, alive] of interpolatedDiscs(now)) {
    const px = cx + x * scale;
    const py = cy + y * scale;
    const r = discRadius * scale;

    ctx.globalAlpha = alive ? 1 : DEAD_ALPHA;
    ctx.beginPath();
    ctx.arc(px, py, r, 0, 2 * Math.PI);
    ctx.fillStyle = discColor(id);
    ctx.fill();
    if (id === session.me) {
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = OWN_OUTLINE_PX;
      ctx.stroke();
    }
    if (id === session.me && alive) drawAim(px, py, x, y, r, charge > 0);
    if (id === session.me && alive) drawCooldown(px, py, r, now);
    if (charge > 0) { // anillo que se completa cuando la carga llega a 1.0
      const start = -Math.PI / 2;
      ctx.beginPath();
      ctx.arc(px, py, r + CHARGE_RING_GAP_PX, start, start + charge * 2 * Math.PI);
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = CHARGE_RING_WIDTH_PX;
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    drawText(nameOf(id), px, py - r - NAME_GAP_PX - (charge > 0 ? CHARGE_RING_GAP_PX + CHARGE_RING_WIDTH_PX : 0), NAME_FONT);
  }
  drawOverlay(session.latest, cx, cy);
}

// Se dibuja al ritmo de la pantalla, no al de los mensajes. El texto (DOM) solo
// se reescribe cuando llega un tick nuevo: tocar el DOM 60 veces por segundo sobra.
let lastHudTick = -1;
function frame(now) { // `now`: la hora del fotograma, en el mismo reloj que performance.now()
  draw(now);
  const state = session.latest;
  if (state && state.tick !== lastHudTick) {
    lastHudTick = state.tick;
    els.hud.textContent = describe(state);
    renderScores(state);
    syncStartButton(state);
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
