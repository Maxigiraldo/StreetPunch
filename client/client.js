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
// El dibujo neón (paleta, robots, partículas, animaciones) vive en neon.js.
const CANVAS_MARGIN_PX = 48;       // aire entre la arena y el borde del canvas: cabe un disco entero
                                   // fuera del borde, que es donde se ve caer (ELIMINATION_MARGIN_DISCS)
const MS_PER_S = 1000;             // `ms` del servidor -> segundos para mostrar
// Color de un disco cuyo id aún no está en el `roster` (el roster llega en el mismo
// tick en que entra el jugador, pero un `state` puede adelantarse): gris neutro.
const UNKNOWN_COLOR = "#8a8f98";
const MAX_FRAME_DT_S = 0.1;        // un fotograma más largo que esto (pestaña dormida) cuenta como 0,1 s
const COUNT_POP_MS = 260;          // duración de la animación al cambiar el número/texto central
const WINNER_TIE_TONE = PALETTE.violet;
const HIT_OTHERS_VOLUME = 0.6;                 // los golpes ajenos suenan más bajo que los propios
const ELIM_OTHERS_VOLUME = 0.7;
const DASH_OTHERS_VOLUME = 0.5;                // el whoosh de una embestida ajena, más bajo que el tuyo
// Música por fase: la batalla solo mientras se juega; todo lo demás (vestíbulo, espera, cuenta
// regresiva, resultado) usa la del vestíbulo. Registro como `handlers`: otra fase es otra entrada.
const musicForPhase = { playing: "battle" };
const RESULT_ROWS = 5;                         // posiciones que muestra la pantalla de resultado
const RING_CIRCUMFERENCE = 2 * Math.PI * 46;   // r=46 del SVG del anillo de la cuenta regresiva
let ringAnimation = null;

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
  entry: document.getElementById("entry"),
  canvas: document.getElementById("arena"),
  overlay: document.getElementById("overlay"),
  overlayCore: document.getElementById("overlay-core"),
  overlayRing: document.getElementById("overlay-ring"),
  ringFill: document.getElementById("ring-fill"),
  result: document.getElementById("result"),
  resultTitle: document.getElementById("result-title"),
  resultRank: document.getElementById("result-rank"),
  resultLeave: document.getElementById("result-leave"),
  sound: document.getElementById("sound"),
  board: document.getElementById("board"),
  boardCount: document.getElementById("board-count"),
  overlayBig: document.getElementById("overlay-big"),
  overlaySmall: document.getElementById("overlay-small"),
  fps: document.getElementById("fps"),
};
// alpha:false -> el canvas es opaco y el navegador se ahorra mezclarlo con la página.
const ctx = els.canvas.getContext("2d", { alpha: false });

// Todo lo de neon.js que vive entre fotogramas: sprites horneados, partículas, animación por disco.
const sprites = new Sprites();
const particles = new ParticlePool(MAX_PARTICLES);
const beams = new EliminationBeams(BEAM_MAX);
const fxTracker = new FxTracker();
const perf = new URLSearchParams(location.search).has("fps") ? new PerfMeter(els.fps) : null;
if (perf) els.fps.hidden = false;
let arenaLayer = { key: "", canvas: null };   // capa estática pre-renderizada (se rehace solo si cambia la arena)
let lastFrameNow = 0;
// Si la fuente termina de cargar después de hornear los nombres, se hornean otra vez.
document.fonts?.load(`600 12px ${FONT_STACK}`).then(() => sprites.clearLabels());

// Todo el estado del cliente en un solo objeto, para no repartir variables sueltas.
const session = {
  socket: null, me: null, color: null, arena: DEFAULT_ARENA, latest: null, phase: null,
  maxPlayers: null,    // tope de jugadores por sala (del welcome)
  room: null,          // id de la sala en que estoy; null = en el vestíbulo
  rooms: [],           // lista de salas (solo llega mientras estoy en el vestíbulo)
  cooldownS: 0,        // enfriamiento de la embestida (del welcome)
  host: null,          // id del anfitrión de la sala (el más antiguo en ella)
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
    syncMusic();
    setStatus(msg.id === null ? "Elige una sala o crea una." : `Estás en la sala ${msg.id}.`);
  },
  roster(msg) {
    // Siempre llega la lista completa: se reemplaza entera, no se mezcla.
    session.host = msg.host;
    session.roster = new Map(msg.players.map((p) => [p.id, { name: p.name, color: p.color }]));
    warmSprites();
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
      syncMusic();
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
  fxTracker.clear();
  particles.clear();
  beams.clear();
  observedPair = "";
  hideOverlay();
  hideResult();
  els.start.hidden = true;
  els.scores.replaceChildren();
  els.board.hidden = true;
  els.boardCount.textContent = "";
}

// Vestíbulo (lista de salas) o sala (partida), según dónde esté el jugador.
function showView() {
  const connected = session.socket !== null;
  els.entry.hidden = connected;
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
    const dot = document.createElement("span");   // verde: abierta; amarillo: jugando; rojo: llena
    dot.className = `dot ${room.players >= room.max ? "full" : room.open ? "open" : "playing"}`;
    dot.title = room.players >= room.max ? "Llena" : room.open ? "Abierta" : "Jugando";
    const title = document.createElement("span");
    title.textContent = `${room.name} • anfitrión ${room.host}`;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = `${room.players}/${room.max} • ${PHASE_LABEL[room.phase] ?? room.phase}`;
    const join = document.createElement("button");
    join.textContent = room.players >= room.max ? "Llena" : "Unirse";
    join.disabled = !room.open;
    join.addEventListener("click", () => send({ t: "join", room: room.id }));
    item.append(dot, title, meta, join);
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
  syncMusic();
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
    syncMusic();
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
  if (!on && ownCharge() >= (session.minCharge ?? 0) && ownCharge() > 0) controls.cooldownUntil = performance.now() + session.cooldownS * MS_PER_S;
  // El sonido de carga solo suena si de verdad se puede cargar: jugando y con el propio robot vivo.
  if (!on) gameAudio.stopCharge();
  else if (ownAlive()) gameAudio.startCharge();
  send({ t: "charge", on });
}

function ownAlive() {
  return session.phase === "playing" && !!session.latest?.d.some((disc) => disc[0] === session.me && disc[4]);
}

function resetControls() {
  gameAudio.stopCharge();
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
  fxTracker.clear();
  particles.clear();
  beams.clear();
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
  if (controls.charging) {
    send({ t: "charge", on: true });
    gameAudio.startCharge();
  }
}

// Fin de ronda: se corta la carga (la tecla pudo seguir pulsada) y, si ganaste, suena el jingle.
function roundOver() {
  gameAudio.stopCharge();
  if (session.me !== null && session.latest?.winner === session.me) gameAudio.play("win");
}
const phaseEntered = { countdown: newRound, playing: resyncControls, round_over: roundOver };

// Qué música debe sonar ahora: ninguna sin conexión; si no, la de la fase (o la del vestíbulo).
function syncMusic() {
  gameAudio.setMusic(session.socket === null ? null : (musicForPhase[session.phase] ?? "lobby"));
}

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
  gameAudio.unlock();   // enviar el nombre es un gesto del usuario: el navegador ya deja sonar
  if (name && !session.socket) connect(name);
});

// Cualquier primer clic o tecla también desbloquea el audio (por si el gesto fue otro).
window.addEventListener("pointerdown", () => gameAudio.unlock(), { once: true });
window.addEventListener("keydown", () => gameAudio.unlock(), { once: true });

function renderSoundButton() {
  els.sound.textContent = gameAudio.muted ? "Sonido off" : "Sonido on";
  els.sound.setAttribute("aria-pressed", String(!gameAudio.muted));
}
els.sound.addEventListener("click", () => {
  gameAudio.unlock();
  gameAudio.setMuted(!gameAudio.muted);
  renderSoundButton();
  els.sound.blur();   // que Espacio no vuelva a «pulsar» el botón en plena ronda
});
renderSoundButton();

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
    parts.push(performance.now() < controls.cooldownUntil ? "• embestida recargando…" : "• embestida lista");
  }
  return parts.join(" ");
}

// Qué decir en el centro de la arena según la fase: [texto grande, texto pequeño, color del brillo].
// Es texto DOM (#overlay), no se dibuja en el canvas.
// Registro por fase (como `handlers`): una fase nueva es una entrada nueva.
const overlayFor = {
  // Sala de espera: la ronda no arranca sola, la inicia el anfitrión (el más antiguo de la sala)
  // cuando ve en la lista que ya están todos.
  waiting: () => {
    const count = session.roster.size;
    const enough = count >= (session.minPlayers ?? 2);
    if (isHost()) {
      return enough
        ? ["Sala de espera", `${count}/${session.maxPlayers} en la sala. Pulsa “Iniciar partida” cuando estén todos`, PALETTE.violet]
        : ["Sala de espera", `Eres el anfitrión. Faltan jugadores (mínimo ${session.minPlayers})`, PALETTE.violet];
    }
    return ["Sala de espera", `${nameOf(session.host)} iniciará la partida (${count}/${session.maxPlayers} en la sala)`, PALETTE.violet];
  },
  countdown: (state) => [String(Math.ceil(state.ms / MS_PER_S)), "¡Prepárate!", PALETTE.cyan],
  round_over: (state) => {
    if (state.winner === null) return ["Empate", "Nadie ganó la ronda", WINNER_TIE_TONE];
    const tone = discColor(state.winner);   // el brillo toma el color del ganador
    return state.winner === session.me
      ? ["¡Ganaste!", "Siguiente ronda en breve", tone]
      : [`Ganó ${nameOf(state.winner)}`, "Siguiente ronda en breve", tone];
  },
};

function isHost() {
  return session.me !== null && session.me === session.host;
}

// El botón solo existe para el anfitrión y solo mientras se espera: el servidor ignora
// la orden de cualquier otro, así que esto es comodidad, no seguridad.
function syncStartButton(state) {
  els.start.hidden = !(isHost() && state.phase === "waiting");
}

// Filas del marcador: todos los del roster, con sus victorias y su estado en esta ronda.
// Más victorias primero; a igualdad, el orden del roster (el de llegada).
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
  const top = rows.length > 0 ? rows[0].wins : 0;
  for (const row of rows) row.leader = top > 0 && row.wins === top; // corona al que va ganando
  const key = JSON.stringify(rows);
  if (key === lastScoreKey) return;
  lastScoreKey = key;
  els.board.hidden = rows.length === 0;
  els.boardCount.textContent = `${rows.length}/${session.maxPlayers}`;
  els.scores.replaceChildren(...rows.map((row) => {
    const item = document.createElement("li");
    item.className = `row ${row.status}${row.me ? " me" : ""}`;
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = row.color;
    swatch.style.color = row.color;   // el CSS saca el brillo de currentColor
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = row.me ? `${row.name} (tú)` : row.name;
    if (row.leader) {
      const crown = document.createElement("span");
      crown.className = "crown";
      name.append(crown);
    }
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

// ---- Capa central (DOM): espera, cuenta regresiva y resultado ----

let lastOverlayKey = "";

function hideOverlay() {
  lastOverlayKey = "";
  ringAnimation?.cancel();
  ringAnimation = null;
  els.overlay.hidden = true;
}

// ---- Pantalla de resultado (DOM) ----

let lastResultKey = "";

function hideResult() {
  lastResultKey = "";
  els.result.hidden = true;
}

// Título (el mismo texto que ya calcula overlayFor.round_over) + ranking por victorias + salida.
// Los nombres los eligen otros jugadores: textContent, nunca HTML.
function renderResult(state) {
  const [title, , tone] = overlayFor.round_over(state);
  const rows = scoreRows(state).slice(0, RESULT_ROWS);
  const key = JSON.stringify([title, tone, rows.map((row) => [row.id, row.wins])]);
  if (key === lastResultKey) return;   // llega a 10 Hz: solo se reconstruye si cambia algo
  lastResultKey = key;
  els.result.hidden = false;
  els.result.style.setProperty("--tone", tone);
  els.resultTitle.textContent = title;
  els.resultRank.replaceChildren(...rows.map((row) => {
    const item = document.createElement("li");
    if (row.id === state.winner) item.className = "winner";
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = row.color;
    swatch.style.color = row.color;
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = row.me ? `${row.name} (tú)` : row.name;
    const wins = document.createElement("span");
    wins.className = "wins";
    wins.textContent = String(row.wins);
    item.append(swatch, name, wins);
    return item;
  }));
  if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
    els.result.animate([{ opacity: 0 }, { opacity: 1 }], { duration: COUNT_POP_MS });
  }
}

els.resultLeave.addEventListener("click", () => {
  send({ t: "leave" });
  els.resultLeave.blur();   // que Espacio no vuelva a «pulsar» el botón
});

// El anillo se vacía durante el segundo que muestra el número actual: empieza con lo que
// queda de ese segundo (state.ms no es múltiplo exacto de 1000) y termina en cero.
function syncCountdownRing(state) {
  const counting = state.phase === "countdown";
  els.overlayCore.classList.toggle("countdown", counting);
  els.overlayRing.toggleAttribute("hidden", !counting);
  ringAnimation?.cancel();
  ringAnimation = null;
  if (!counting || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const left = state.ms - (Math.ceil(state.ms / MS_PER_S) - 1) * MS_PER_S; // ms que quedan de este segundo (0..1000]
  ringAnimation = els.ringFill.animate(
    [{ strokeDashoffset: RING_CIRCUMFERENCE * (1 - left / MS_PER_S) }, { strokeDashoffset: RING_CIRCUMFERENCE }],
    { duration: left, easing: "linear", fill: "forwards" },
  );
}

// Solo toca el DOM cuando el texto cambia (la cuenta atrás llega a 10 Hz, el texto cambia 1 vez por segundo).
// Los nombres los eligen otros jugadores: textContent, nunca HTML.
function renderOverlay(state) {
  if (state.phase === "round_over") { // el resultado tiene su propia pantalla
    hideOverlay();
    return renderResult(state);
  }
  hideResult();
  if (!Object.hasOwn(overlayFor, state.phase)) return hideOverlay();
  const [big, small, tone] = overlayFor[state.phase](state);
  const key = `${state.phase}|${big}|${small}|${tone}`;
  if (key === lastOverlayKey) return;
  lastOverlayKey = key;
  els.overlay.hidden = false;
  els.overlay.style.setProperty("--tone", tone);
  els.overlayBig.textContent = big;
  els.overlayBig.classList.toggle("number", /^\d+$/.test(big)); // las cuentas atrás van grandes
  syncCountdownRing(state);
  els.overlaySmall.textContent = small;
  // "Golpe" de entrada: transform y opacity, que no obligan al navegador a repintar el texto.
  if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
    els.overlayBig.animate(
      [{ transform: "scale(1.45)", opacity: 0 }, { transform: "scale(1)", opacity: 1 }],
      { duration: COUNT_POP_MS, easing: "cubic-bezier(.2,.9,.3,1.2)" },
    );
  }
}

// ---- Lo que el servidor no manda, deducido de los estados (golpes y embestidas) ----

// Se observa cada PAR de estados que se está mezclando: la velocidad de un disco es su
// desplazamiento entre los dos dividido por la diferencia de `tick`. Así no depende de
// cuándo llegaron los paquetes (la red tiene jitter; los ticks no).
let observedPair = "";
function observePair(now, view) {
  const snaps = timeline.snapshots;
  if (snaps.length < 2) return;
  const before = snaps[0].state;
  const after = snaps[1].state;
  const key = `${before.tick}-${after.tick}`;
  if (key === observedPair) return;   // el mismo par que el fotograma anterior
  observedPair = key;
  const ticks = after.tick - before.tick;
  // Solo se compara jugando: al empezar la ronda los discos "saltan" a su punto de salida.
  if (ticks <= 0 || before.phase !== "playing" || after.phase !== "playing") return;

  const previous = new Map(before.d.map((disc) => [disc[0], disc]));
  for (const [id, x, y, charge, alive] of after.d) {
    const old = previous.get(id);
    const fx = fxTracker.peek(id);
    if (!old || !fx) continue;
    if (!old[4] || !alive) { fx.dashing = false; fx.vTick = -1; continue; }

    const vx = (x - old[1]) / ticks;
    const vy = (y - old[2]) / ticks;
    fx.dashing = Math.hypot(vx, vy) > TRAIL_MIN_UNITS_PER_TICK;

    // Soltar la carga (>= min_charge -> 0) es una embestida: chispas hacia atrás.
    const released = old[3] > 0 && old[3] >= (session.minCharge ?? 0) && charge === 0;
    if (released) {
      fx.dashUntil = now + DASH_GRACE_MS;
      gameAudio.play("whoosh", { volume: id === session.me ? 1 : DASH_OTHERS_VOLUME });
      particles.burstDash(view.cx + old[1] * view.scale, view.cy + old[2] * view.scale, fx.heading, discColor(id), sprites);
    } else if (fx.vTick === before.tick && now >= fx.dashUntil
        && Math.hypot(vx - fx.vx, vy - fx.vy) > HIT_MIN_DV_UNITS_PER_TICK) {
      fx.flashAt = now;               // cambio brusco de velocidad sin que él haya embestido: lo golpearon
      gameAudio.play("hit", { volume: id === session.me ? 1 : HIT_OTHERS_VOLUME });
    }
    fx.vx = vx;
    fx.vy = vy;
    fx.vTick = after.tick;            // el par siguiente solo se compara con este si empieza donde éste acaba
  }
}

// ---- Dibujo ----

// Dirección de la flecha de puntería del PROPIO disco: la última pulsada o, sin ninguna,
// hacia el centro de la arena, como el servidor al empezar.
function aimOf(worldX, worldY) {
  const aim = controls.aim ?? { dx: -worldX, dy: -worldY };
  const length = Math.hypot(aim.dx, aim.dy);
  return length === 0 ? null : { ux: aim.dx / length, uy: aim.dy / length };
}

// Píxeles por unidad del mundo: la arena cabe en el canvas dejando CANVAS_MARGIN_PX de aire.
function pixelsPerUnit() {
  return (els.canvas.width / 2 - CANVAS_MARGIN_PX) / session.arena.radius;
}

// Hornear un sprite (con su brillo) cuesta varios milisegundos. Hacerlo la primera vez que
// aparece un color, ya con la ronda en marcha, provoca un tirón justo al empezar. El roster
// llega en la sala de espera, donde no hay animación que se pueda trabar: se hornea ahí.
function warmSprites() {
  sprites.setDiscRadius(session.arena.disc_radius * pixelsPerUnit());
  for (const { color } of session.roster.values()) {
    sprites.robot(color);
    sprites.halo(color);
    sprites.dot(color);
    sprites.white(color);
  }
}

function draw(now) {
  const { width, height } = els.canvas;
  const cx = width / 2;
  const cy = height / 2;
  const { radius, disc_radius: discRadius } = session.arena;
  const scale = pixelsPerUnit();
  const r = discRadius * scale;
  const dt = Math.min(MAX_FRAME_DT_S, lastFrameNow ? (now - lastFrameNow) / MS_PER_S : 0);
  lastFrameNow = now;

  // Capa estática: UNA vez (y otra si cambia el tamaño de la arena). Cada fotograma es un drawImage.
  const layerKey = `${width}x${height}:${radius * scale}`;
  if (arenaLayer.key !== layerKey) arenaLayer = { key: layerKey, canvas: buildArenaLayer(width, height, radius * scale) };
  ctx.globalAlpha = 1;
  ctx.globalCompositeOperation = "source-over";
  ctx.drawImage(arenaLayer.canvas, 0, 0);

  if (!session.latest) return;
  sprites.setDiscRadius(r);
  const discs = interpolatedDiscs(now);
  const view = { cx, cy, scale };
  observePair(now, view);

  fxTracker.nextFrame();
  // Los eliminados van debajo y el propio encima de todos.
  const ordered = [...discs].sort((a, b) => (a[4] - b[4]) || ((a[0] === session.me) - (b[0] === session.me)));
  for (const [id, x, y, charge, alive] of ordered) {
    const px = cx + x * scale;
    const py = cy + y * scale;
    const color = discColor(id);
    const own = id === session.me;
    const fx = fxTracker.get(id, now, !!alive, Math.atan2(-y, -x)); // al nacer mira al centro, como el servidor
    const aim = own && alive ? aimOf(x, y) : null;
    fx.track(px, py, dt, aim && charge > 0 ? Math.atan2(aim.uy, aim.ux) : null);

    // Eliminación: se detecta en el MOMENTO en que se ve (flanco vivo -> muerto en lo interpolado).
    if (fx.wasAlive && !alive) {
      fx.deadAt = now;
      fx.dashing = false;
      particles.burstElimination(px, py, color, sprites);
      beams.add(px, py);
      gameAudio.play("eliminated", { volume: own ? 1 : ELIM_OTHERS_VOLUME });
    }
    fx.wasAlive = !!alive;

    const cooldownLeft = own && session.cooldownS > 0 ? (controls.cooldownUntil - now) / (session.cooldownS * MS_PER_S) : 0;
    paintDisc(ctx, sprites, fx, {
      px, py, r, color, charge, alive: !!alive, own,
      aim, cooldown: Math.max(0, cooldownLeft), name: nameOf(id),
    }, now);
  }
  fxTracker.prune(discs.length);

  particles.update(dt);
  beams.update(dt);
  beams.draw(ctx, sprites);
  particles.draw(ctx);
}

// Se dibuja al ritmo de la pantalla, no al de los mensajes. El texto (DOM) solo
// se reescribe cuando llega un tick nuevo: tocar el DOM 60 veces por segundo sobra.
let lastHudTick = -1;
function frame(now) { // `now`: la hora del fotograma, en el mismo reloj que performance.now()
  const started = performance.now();
  draw(now);
  const state = session.latest;
  if (state && state.tick !== lastHudTick) {
    lastHudTick = state.tick;
    els.hud.textContent = describe(state);
    renderScores(state);
    renderOverlay(state);
    syncStartButton(state);
  }
  if (perf) perf.frame(now, performance.now() - started);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
