"use strict";
/*
 * Cliente de StreetPunch (P5a dibujo + P5b teclado).
 *
 * - Abre el WebSocket, manda `hello` y dibuja cada `state` tal como llega
 *   (sin interpolar: eso es P5c).
 * - Envía INTENCIONES (hacia dónde quiere moverse, si carga), nunca posiciones:
 *   el servidor es la autoridad (docs/protocolo.md).
 */

// ---- Constantes visuales (el juego en sí lo define el servidor, no el cliente) ----
const CANVAS_MARGIN_PX = 24;       // aire entre la arena y el borde del canvas
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
// El estado no trae el color de los demás (solo el `welcome` trae el propio),
// así que se deriva un tono estable del id. 47 es primo con 360: ids seguidos
// quedan bien separados en la rueda de color.
const HUE_STEP_DEG = 47;
const FULL_TURN_DEG = 360;
const OTHER_DISC_SATURATION = "70%";
const OTHER_DISC_LIGHTNESS = "55%";

// Radios por defecto hasta que llegue el `welcome` con los reales.
const DEFAULT_ARENA = { radius: 300, disc_radius: 24 };

const PHASE_TEXT = {
  waiting: "Esperando jugadores…",
  countdown: "¡Prepárate!",
  playing: "¡A pelear!",
  round_over: "Fin de la ronda",
};

const els = {
  form: document.getElementById("join"),
  name: document.getElementById("name"),
  enter: document.getElementById("enter"),
  status: document.getElementById("status"),
  hud: document.getElementById("hud"),
  scores: document.getElementById("scores"),
  canvas: document.getElementById("arena"),
};
const ctx = els.canvas.getContext("2d");

// Todo el estado del cliente en un solo objeto, para no repartir variables sueltas.
const session = { socket: null, me: null, color: null, arena: DEFAULT_ARENA, latest: null, phase: null };

// ---------------------------------------------------------------- mensajes

// Registro {tipo: manejador}, igual que el codec del servidor: agregar un
// mensaje nuevo es agregar una entrada, no otro `if`.
const handlers = {
  welcome(msg) {
    session.me = msg.id;
    session.color = msg.color;
    session.arena = msg.arena;
    setStatus(`Conectado como #${msg.id}`);
  },
  state(msg) {
    // Solo importa el más reciente: cada `state` se basta a sí mismo
    // (docs/protocolo.md §4), así que descartar los intermedios no pierde nada.
    session.latest = msg;
    // El cambio de fase se detecta comparando con la anterior: no hay mensaje aparte.
    if (msg.phase !== session.phase) {
      session.phase = msg.phase;
      if (Object.hasOwn(phaseEntered, msg.phase)) phaseEntered[msg.phase]();
    }
  },
  error(msg) {
    setStatus(`El servidor rechazó el mensaje: ${msg.code}`);
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

function connect(name) {
  session.me = null;
  session.latest = null;
  session.phase = null;
  session.arena = DEFAULT_ARENA;
  resetControls();
  els.enter.disabled = true;
  // Sin esto el foco se queda en el campo de nombre y las teclas del juego se
  // escribirían ahí en vez de llegar al juego.
  els.name.blur();

  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/ws`);
  session.socket = socket;
  setStatus("Conectando…");

  // `hello` debe ser el primer mensaje (docs/protocolo.md §2).
  socket.addEventListener("open", () => socket.send(JSON.stringify({ t: "hello", name })));
  socket.addEventListener("message", onMessage);
  socket.addEventListener("close", (event) => {
    // 1008 = el servidor rechazó algo (el `error` previo dice qué); se conserva ese texto.
    if (event.code !== 1008) setStatus(`Desconectado (código ${event.code})`);
    session.socket = null;
    resetControls();
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
const CHARGE_CODE = "Space";

const controls = {
  held: new Set(),                 // teclas de movimiento pulsadas ahora
  intent: { dx: 0, dy: 0 },        // última intención ENVIADA (para no repetirla)
  charging: false,                 // si la tecla de carga está pulsada
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

function setCharging(on) {
  if (on === controls.charging) return;
  controls.charging = on;
  send({ t: "charge", on });
}

function resetControls() {
  controls.held.clear();
  controls.intent = { dx: 0, dy: 0 };
  controls.charging = false;
  controls.aim = null;
}

// El servidor crea discos NUEVOS en cada ronda (al empezar la cuenta atrás), con
// la dirección en cero y apuntando al centro. Por eso la puntería se olvida ahí.
function newRound() {
  controls.aim = null;
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
  if (!isMove && event.code !== CHARGE_CODE) return;
  event.preventDefault();      // evita que flechas y espacio desplacen la página
  if (event.repeat) return;    // la repetición automática del teclado no es una pulsación nueva
  if (isMove) {
    controls.held.add(event.code);
    syncIntent();
  } else {
    setCharging(true);
  }
});

// keyup no mira isTyping a propósito: soltar una tecla siempre debe contar,
// o un disco podría quedarse corriendo si el foco cambió con la tecla pulsada.
window.addEventListener("keyup", (event) => {
  if (Object.hasOwn(MOVE_KEYS, event.code)) {
    controls.held.delete(event.code);
    syncIntent();
  } else if (event.code === CHARGE_CODE) {
    setCharging(false);
  }
});

// Si la ventana pierde el foco con una tecla pulsada, el navegador no manda su
// keyup: sin esto el disco seguiría corriendo solo. Soltar la carga aquí lanza
// la embestida; es preferible a dejar la carga "pegada" sin control del jugador.
window.addEventListener("blur", () => {
  controls.held.clear();
  syncIntent();
  setCharging(false);
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
  if (id === session.me && session.color) return session.color;
  const hue = (id * HUE_STEP_DEG) % FULL_TURN_DEG;
  return `hsl(${hue} ${OTHER_DISC_SATURATION} ${OTHER_DISC_LIGHTNESS})`;
}

function describe(state) {
  const parts = [PHASE_TEXT[state.phase] ?? state.phase];
  if (state.ms !== null) parts.push(String(Math.ceil(state.ms / MS_PER_S)));
  if (state.winner !== null) {
    parts.push(state.winner === session.me ? "— ¡ganaste!" : `— ganó #${state.winner}`);
  }
  // Se juega si el propio id aparece en `d`; si no, se es espectador (docs/protocolo.md §4).
  const playing = state.d.some((disc) => disc[0] === session.me);
  parts.push(playing ? "(juegas)" : "(espectador)");
  return parts.join(" ");
}

function describeScores(state) {
  return Object.entries(state.scores)
    .map(([id, wins]) => `${Number(id) === session.me ? "tú" : "#" + id}: ${wins}`)
    .join("   ");
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

function draw() {
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
  for (const [id, x, y, charge, alive] of session.latest.d) {
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
    if (charge > 0) { // anillo que se completa cuando la carga llega a 1.0
      const start = -Math.PI / 2;
      ctx.beginPath();
      ctx.arc(px, py, r + CHARGE_RING_GAP_PX, start, start + charge * 2 * Math.PI);
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = CHARGE_RING_WIDTH_PX;
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }
}

// Se dibuja al ritmo de la pantalla, no al de los mensajes. El texto (DOM) solo
// se reescribe cuando llega un tick nuevo: tocar el DOM 60 veces por segundo sobra.
let lastHudTick = -1;
function frame() {
  draw();
  const state = session.latest;
  if (state && state.tick !== lastHudTick) {
    lastHudTick = state.tick;
    els.hud.textContent = describe(state);
    els.scores.textContent = describeScores(state);
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
