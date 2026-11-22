"use strict";
/*
 * Cliente de StreetPunch, paso P5a: SOLO MUESTRA lo que el servidor decide.
 *
 * - Abre el WebSocket, manda `hello` y dibuja cada `state` tal como llega
 *   (sin interpolar: eso es P5c) y sin enviar teclas todavía (P5b).
 * - El cliente nunca calcula posiciones: el servidor es la autoridad.
 *   (docs/protocolo.md). Aquí solo se leen y se pintan.
 */

// ---- Constantes visuales (el juego en sí lo define el servidor, no el cliente) ----
const CANVAS_MARGIN_PX = 24;       // aire entre la arena y el borde del canvas
const CHARGE_RING_GAP_PX = 5;      // separación entre el disco y su anillo de carga
const CHARGE_RING_WIDTH_PX = 4;    // grosor del anillo de carga
const OWN_OUTLINE_PX = 3;          // borde blanco para reconocer el propio disco
const DEAD_ALPHA = 0.25;           // discos eliminados: se dibujan apagados hasta fin de ronda
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
const session = { socket: null, me: null, color: null, arena: DEFAULT_ARENA, latest: null };

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
  session.arena = DEFAULT_ARENA;
  els.enter.disabled = true;

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
    els.enter.disabled = false;
  });
}

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
