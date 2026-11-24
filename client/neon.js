"use strict";
/*
 * Dibujo neón de StreetPunch. Todo sale de código: ni imágenes ni fuentes externas.
 *
 * REGLA DE RENDIMIENTO (la razón de ser de este archivo): `shadowBlur` hace un
 * desenfoque gaussiano POR CADA trazo. Con 8 robots, anillos y partículas serían
 * decenas por fotograma y los FPS se hunden. Por eso:
 *   - `shadowBlur` solo se usa al CONSTRUIR capas y sprites (una vez), nunca al dibujar
 *     un fotograma. Cada fotograma solo hace `drawImage`, arcos y líneas.
 *   - El brillo que cambia (cargar, golpe) usa un halo pre-renderizado con composición
 *     aditiva (`lighter`) y se anima con `globalAlpha` y escala, que son casi gratis.
 *   - Los anillos se hacen con DOS trazos (uno ancho y tenue, otro fino y brillante).
 *   - Las partículas son un arreglo de tamaño fijo (tope) y un sprite de punto por color.
 * Este archivo no conoce la sesión ni el WebSocket: solo dibuja lo que se le pasa.
 */

const PALETTE = {
  bg: "#090D1B",
  arena: "#151E38",
  cyan: "#00E5FF",
  pink: "#FF3CAC",
  violet: "#8B5CF6",
  yellow: "#F9F871",
  text: "#F4F7FF",
};
const FONT_STACK = "Orbitron, system-ui, sans-serif"; // respaldo si la fuente no carga
const TAU = Math.PI * 2;

// ---- Rendimiento ----
const MAX_PARTICLES = 200;         // tope duro: con 8 jugadores y varias eliminaciones seguidas no crece más
const PARTICLE_SPRITE_PX = 24;     // lado del sprite de punto luminoso (se escala al dibujar)
const PERF_REPORT_MS = 500;        // cada cuánto se calcula el FPS mostrado
const LONG_FRAME_MS = 25;          // un fotograma más largo que esto (< 40 FPS) se nota como tirón: se cuenta aparte
const PERF_WINDOW_FRAMES = 300;    // fotogramas recordados para el "1 % peor" (~5 s a 60 FPS)
const LABEL_CACHE_MAX = 64;        // los nombres los elige la gente: la caché no puede crecer sin límite

// ---- Arena (capa estática) ----
const SKYLINE_SEED = 20240611;     // semilla fija: la ciudad es siempre la misma, sin Math.random
const SKYLINE_BANDS = [            // de atrás hacia adelante; `flip` la dibuja colgando del borde superior
  { fill: "#0f1838", maxHeight: 0.46, minWidth: 28, maxWidth: 64, windowChance: 0.30, neonChance: 0.30, flip: false },
  { fill: "#0a1028", maxHeight: 0.32, minWidth: 22, maxWidth: 52, windowChance: 0.22, neonChance: 0.20, flip: false },
  { fill: "#0b1130", maxHeight: 0.16, minWidth: 24, maxWidth: 58, windowChance: 0.18, neonChance: 0.15, flip: true },
];
const WINDOW_COLORS = [PALETTE.cyan, PALETTE.pink, PALETTE.yellow, PALETTE.violet];
const WINDOW_STEP_X = 9;           // separación entre ventanas (px)
const WINDOW_STEP_Y = 11;
const WINDOW_W = 3;
const WINDOW_H = 4;
const WINDOW_ALPHA = 0.42;
const BORDER_GLOW_PX = 22;         // desenfoque de los tramos del borde (solo al construir la capa)
const RIM_WIDTH_PX = 12;           // reborde exterior: el "grosor" de la plataforma vista desde arriba
const RING_INSET_PX = 3;           // el anillo luminoso va un poco dentro del borde jugable
const RING_SEGMENTS = 28;          // tramos del anillo de neón
const RING_SEGMENT_FILL = 0.74;    // fracción de cada tramo que se pinta (el resto es hueco)
const RING_BRIGHT_EVERY = 4;       // uno de cada 4 tramos es un "bloque brillante"
const RING_WIDTH_PX_SEG = 3.5;
const RING_BRIGHT_WIDTH_PX = 6;
const GRID_RINGS = 4;              // anillos concéntricos del piso
const FLOOR_SECTORS = 8;           // placas del piso (y lados de la placa central)
const FLOOR_PLATE_FRACTION = 0.34; // radio de la placa central octogonal
const SECTOR_OFFSET = Math.PI / 8; // gira las placas para que ninguna costura caiga justo en un eje
const SEAM_ALPHA = 0.16;
const GRID_ALPHA = 0.09;
const CROWN_SIZE_FRACTION = 0.16;  // ancho de la corona respecto al radio de la arena
const CROWN_ALPHA = 0.55;          // tenue: es un adorno de piso, no debe tapar la acción

// ---- Props de azotea y luna (capa estática; vistos desde arriba) ----
const PROP_INSET_FRACTION = 0.09;  // los props viven en las 4 esquinas: es lo único fuera del círculo de la arena
const PROP_SIZE_FRACTION = 0.085;
const MOON_X_FRACTION = 0.27;
const MOON_Y_FRACTION = 0.07;
const MOON_RADIUS_PX = 22;
const BILLBOARD_W_PX = 86;
const BILLBOARD_H_PX = 48;
const BILLBOARD_ANGLE_RAD = -0.2;  // girada: parece apoyada en el borde de la azotea, no alineada con el marco

// ---- Robot (proporciones respecto al radio del disco; el robot mira hacia ARRIBA en el sprite) ----
const ROBOT = {
  halfWidth: 0.78, top: -0.8, bottom: 0.85, corner: 0.42,
  visorHalfWidth: 0.55, visorTop: -0.52, visorBottom: -0.06, visorCorner: 0.2,
  eyeX: 0.25, eyeY: -0.29, eyeRadius: 0.11,
  antennaBaseX: 0.45, antennaBaseY: -0.8, antennaTipX: 0.68, antennaTipY: -1.22, tipRadius: 0.11,
  thrusterY: 0.58, thrusterHalfWidth: 0.4,
  spriteSide: 5,                   // lado del sprite en radios: cabe antenas y el brillo
  glowBlur: 0.7,                   // desenfoque del brillo horneado, en radios
};
const HALO_RADIUS_FACTOR = 2.3;    // el halo aditivo llega a esta distancia (en radios)

// ---- Animaciones (tiempos en ms, ángulos en radianes) ----
const SWAY_HZ = 0.6;               // balanceo en reposo: ~1 oscilación cada 1,7 s, lento para que "respire"
const SWAY_RAD = 0.12;             // ±7°
const BREATHE_SCALE = 0.04;
const SWAY_FADE_PX_S = 70;         // por encima de esta velocidad (px/s) el balanceo se apaga: ya se mueve
const SPEED_SMOOTHING = 0.2;       // peso de cada muestra en la velocidad suavizada
const HEADING_RATE = 14;           // 1/s: qué tan rápido gira el robot hacia donde va
const HEADING_MIN_MOVE_PX = 0.4;   // por debajo de este desplazamiento por fotograma no se cambia el rumbo
const CHARGE_SCALE = 0.14;         // el robot se hincha un 14 % con la carga completa
const CHARGE_SHAKE_PX = 1.2;       // vibración al cargar
const HALO_BASE_ALPHA = 0.30;
const HALO_CHARGE_ALPHA = 0.55;
const HALO_CHARGE_GROW = 0.45;
const CHARGE_RING_GAP_PX = 5;      // separación entre el robot y su anillo de carga
const COOLDOWN_RING_GAP_PX = 10;   // el anillo de recarga va por fuera
const RING_WIDTH_PX = 3;
const RING_GLOW_FACTOR = 2.8;      // el trazo tenue es 2,8 veces más ancho que el fino
const FLASH_MS = 280;              // destello al recibir un golpe
const FLASH_SCALE = 0.2;
const SHOCK_GROW = 1.3;            // la onda del golpe crece hasta 2,3 radios
const ELIM_MS = 520;               // encoger y apagar al ser eliminado
const ELIM_END_SCALE = 0.55;
const ELIM_SPIN = 1.6;
const DEAD_ALPHA = 0.25;           // un eliminado queda apagado hasta fin de ronda
const TRAIL_LEN = 7;               // fantasmas de la estela
const TRAIL_ALPHA = 0.32;
const NAME_GAP_PX = 10;
const NAME_FONT = `600 12px ${FONT_STACK}`;
const NAME_STROKE_PX = 4;
const LABEL_PAD_PX = 4;
const MARKER_SIZE_PX = 8;          // triángulo sobre el propio robot
const MARKER_BOB_PX = 2.5;
const MARKER_BOB_HZ = 0.55;

// Umbrales sobre la velocidad que el servidor NO manda: se infiere del desplazamiento entre
// dos `state` dividido por la diferencia de `tick` (unidades del mundo por tick, así no
// depende de la irregularidad con que llegan los paquetes). Dependen de config.py:
// MAX_SPEED 170 u/s ≈ 5,7 u/tick a 30 Hz; DASH_MAX_SPEED 600 u/s = 20 u/tick.
const TRAIL_MIN_UNITS_PER_TICK = 9;    // claramente por encima de caminar: es una embestida
const HIT_MIN_DV_UNITS_PER_TICK = 4;   // cambio brusco de velocidad = golpe (la fricción no pasa de ~2,7)
const DASH_GRACE_MS = 350;             // tras soltar la carga el salto de velocidad es la embestida, no un golpe

// ---- Golpe: estrellitas y embestida: líneas de velocidad ----
const STAR_COUNT = 3;              // estrellitas que giran sobre la cabeza de quien recibe un golpe
const STAR_MS = 650;
const STAR_ORBIT_HZ = 1.7;
const STAR_SPRITE_PX = 16;
const STAR_SIZE_PX = 16;
const SPEED_LINES = 4;             // rayas detrás de quien embiste
const SPEED_LINE_CYCLE_MS = 110;   // cada raya nace, se alarga y se apaga en este tiempo
const SPEED_LINE_ALPHA = 0.95;

// ---- Eliminación: columna de luz ----
const BEAM_MAX = 8;                // una sala tiene a lo sumo 8 eliminaciones por ronda
const BEAM_LIFE_S = 0.9;
const BEAM_W_PX = 44;              // tamaño del sprite de la columna (se escala al dibujar)
const BEAM_H_PX = 190;
const BEAM_RING_START_PX = 10;     // la onda del suelo nace con este radio y crece
const BEAM_RING_GROW_PX = 46;
const SQUARE_SPRITE_PX = 22;

// ---- Partículas ----
const DASH_PARTICLES = 14;
const DASH_SPREAD_RAD = 0.9;
const DASH_SPEED_MIN = 90;             // px/s
const DASH_SPEED_MAX = 260;
const DASH_LIFE_MIN_S = 0.25;
const DASH_LIFE_MAX_S = 0.5;
const ELIM_PARTICLES = 30;
const ELIM_SPEED_MIN = 60;
const ELIM_SPEED_MAX = 320;
const ELIM_LIFE_MIN_S = 0.5;
const ELIM_LIFE_MAX_S = 0.95;
const PARTICLE_SIZE_MIN = 6;
const PARTICLE_SIZE_MAX = 15;
const PARTICLE_DRAG_PER_S = 2.2;       // las chispas frenan solas
const PARTICLE_DT_MAX_S = 0.05;        // un fotograma largo (pestaña dormida) no teletransporta chispas

// ------------------------------------------------------------------ utilidades

function makeLayer(width, height) {
  // OffscreenCanvas si existe; si no, un canvas normal que nunca se inserta en la página.
  if (typeof OffscreenCanvas === "function") return new OffscreenCanvas(width, height);
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  return canvas;
}

function mixNum(from, to, t) { return from + (to - from) * t; }
function clamp01(v) { return v < 0 ? 0 : v > 1 ? 1 : v; }

function angleDiff(target, from) {
  let d = (target - from) % TAU;
  if (d > Math.PI) d -= TAU;
  if (d < -Math.PI) d += TAU;
  return d;
}

const HEX_COLOR = /^#[0-9a-f]{6}$/i;
const tintCache = new Map();
// "#rrggbb" + alfa -> "rgba(...)", memorizado: se llama varias veces por fotograma con pocos valores distintos.
function tint(hex, alpha) {
  const key = `${hex}|${alpha}`;
  let value = tintCache.get(key);
  if (value === undefined) {
    const n = parseInt(HEX_COLOR.test(hex) ? hex.slice(1) : "8a8f98", 16);
    value = `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
    tintCache.set(key, value);
  }
  return value;
}

function roundedRect(g, x, y, w, h, radius) {
  g.beginPath();
  g.moveTo(x + radius, y);
  g.arcTo(x + w, y, x + w, y + h, radius);
  g.arcTo(x + w, y + h, x, y + h, radius);
  g.arcTo(x, y + h, x, y, radius);
  g.arcTo(x, y, x + w, y, radius);
  g.closePath();
}

function seededRandom(seed) { // mulberry32: pseudoaleatorio determinista, para que la ciudad no cambie entre cargas
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ------------------------------------------------------------------ capa estática de la arena

function paintSkylineBand(g, width, height, band, rand) {
  g.save();
  if (band.flip) { g.translate(0, height); g.scale(1, -1); } // cuelga del borde superior
  let x = -rand() * band.minWidth;
  while (x < width) {
    const w = mixNum(band.minWidth, band.maxWidth, rand());
    const h = height * band.maxHeight * mixNum(0.35, 1, rand());
    const top = height - h;
    g.fillStyle = band.fill;
    g.fillRect(x, top, w - 1, h);
    for (let wy = top + 8; wy < height - 6; wy += WINDOW_STEP_Y) {
      for (let wx = x + 5; wx < x + w - 7; wx += WINDOW_STEP_X) {
        if (rand() >= band.windowChance) continue;
        g.fillStyle = tint(WINDOW_COLORS[Math.floor(rand() * WINDOW_COLORS.length)], WINDOW_ALPHA);
        g.fillRect(wx, wy, WINDOW_W, WINDOW_H);
      }
    }
    if (rand() < band.neonChance) { // tira de neón en la azotea (el brillo es barato: esto se pinta una vez)
      g.save();
      const color = WINDOW_COLORS[Math.floor(rand() * 2)];
      g.shadowColor = color;
      g.shadowBlur = 10;
      g.fillStyle = color;
      g.fillRect(x, top, w - 1, 2);
      g.restore();
    }
    x += w;
  }
  g.restore();
}

function paintCrown(g, cx, cy, size, color = PALETTE.yellow, alpha = CROWN_ALPHA) {
  const w = size / 2;
  const h = size * 0.36;
  g.save();
  g.translate(cx, cy);
  g.lineJoin = "round";
  g.lineWidth = 2.5;
  g.strokeStyle = tint(color, alpha);
  g.fillStyle = tint(color, 0.10);
  g.shadowColor = color;
  g.shadowBlur = 16;
  g.beginPath();
  g.moveTo(-w, h);
  g.lineTo(-w, -h * 0.35);
  g.lineTo(-w * 0.5, h * 0.2);
  g.lineTo(0, -h);
  g.lineTo(w * 0.5, h * 0.2);
  g.lineTo(w, -h * 0.35);
  g.lineTo(w, h);
  g.closePath();
  g.fill();
  g.stroke();
  g.fillStyle = tint(color, alpha);
  for (const [px, py] of [[-w, -h * 0.35], [0, -h], [w, -h * 0.35]]) {
    g.beginPath();
    g.arc(px, py, 3.2, 0, TAU);
    g.fill();
  }
  g.restore();
}

// Ventilador de azotea: placa, rejilla circular y seis aspas (visto desde arriba).
function paintVent(g, x, y, size) {
  const half = size / 2;
  g.save();
  g.translate(x, y);
  roundedRect(g, -half, -half, size, size, size * 0.14);
  g.fillStyle = "#0c1330";
  g.fill();
  g.lineWidth = 2;
  g.strokeStyle = tint(PALETTE.cyan, 0.45);
  g.shadowColor = PALETTE.cyan;
  g.shadowBlur = 10;
  g.stroke();
  g.shadowBlur = 0;
  g.beginPath();
  g.arc(0, 0, half * 0.74, 0, TAU);
  g.fillStyle = "#060a1c";
  g.fill();
  g.strokeStyle = tint(PALETTE.cyan, 0.3);
  g.lineWidth = 1.5;
  g.stroke();
  g.fillStyle = tint(PALETTE.cyan, 0.5);
  for (let i = 0; i < 6; i++) {
    g.save();
    g.rotate((i * TAU) / 6 + 0.3);
    g.beginPath();
    g.ellipse(half * 0.38, 0, half * 0.36, half * 0.1, 0, 0, TAU);
    g.fill();
    g.restore();
  }
  g.beginPath();
  g.arc(0, 0, half * 0.12, 0, TAU);
  g.fillStyle = tint(PALETTE.cyan, 0.9);
  g.fill();
  g.restore();
}

// Pila de cajas con una tira de neón.
function paintCrates(g, x, y, size) {
  const boxes = [[-0.2, 0.15, 0.62, 0.15, PALETTE.pink], [0.28, -0.12, 0.5, -0.3, PALETTE.violet], [0.1, 0.38, 0.4, 0.5, PALETTE.cyan]];
  g.save();
  g.translate(x, y);
  for (const [dx, dy, scale, angle, color] of boxes) {
    const side = size * scale;
    g.save();
    g.translate(dx * size, dy * size);
    g.rotate(angle);
    g.fillStyle = "#10173a";
    g.fillRect(-side / 2, -side / 2, side, side);
    g.lineWidth = 1.8;
    g.strokeStyle = tint(color, 0.6);
    g.shadowColor = color;
    g.shadowBlur = 8;
    g.strokeRect(-side / 2, -side / 2, side, side);
    g.shadowBlur = 0;
    g.fillStyle = tint(color, 0.45);
    g.fillRect(-side / 2, -side * 0.08, side, side * 0.16); // tira de neón
    g.restore();
  }
  g.restore();
}

// Antena parabólica vista desde arriba: plato, brazo y receptor.
function paintDish(g, x, y, size) {
  const radius = size * 0.42;
  g.save();
  g.translate(x, y);
  const dish = g.createRadialGradient(-radius * 0.25, -radius * 0.25, radius * 0.1, 0, 0, radius);
  dish.addColorStop(0, "#3a2f78");
  dish.addColorStop(1, "#14183e");
  g.beginPath();
  g.arc(0, 0, radius, 0, TAU);
  g.fillStyle = dish;
  g.fill();
  g.lineWidth = 2;
  g.strokeStyle = tint(PALETTE.violet, 0.7);
  g.shadowColor = PALETTE.violet;
  g.shadowBlur = 10;
  g.stroke();
  g.shadowBlur = 0;
  g.beginPath();
  g.moveTo(0, 0);
  g.lineTo(radius * 0.9, -radius * 0.6);
  g.strokeStyle = tint(PALETTE.text, 0.5);
  g.lineWidth = 2;
  g.stroke();
  g.beginPath();
  g.arc(radius * 0.9, -radius * 0.6, radius * 0.13, 0, TAU);
  g.fillStyle = PALETTE.pink;
  g.shadowColor = PALETTE.pink;
  g.shadowBlur = 8;
  g.fill();
  g.restore();
}

// Pantalla con la corona (el emblema de la arena), apoyada en una esquina.
function paintBillboard(g, x, y, w, h, angle) {
  g.save();
  g.translate(x, y);
  g.rotate(angle);
  roundedRect(g, -w / 2, -h / 2, w, h, 6);
  const screen = g.createLinearGradient(0, -h / 2, 0, h / 2);
  screen.addColorStop(0, "#2a1156");
  screen.addColorStop(1, "#120a30");
  g.fillStyle = screen;
  g.fill();
  g.lineWidth = 2.5;
  g.strokeStyle = tint(PALETTE.pink, 0.85);
  g.shadowColor = PALETTE.pink;
  g.shadowBlur = 14;
  g.stroke();
  g.shadowBlur = 0;
  paintCrown(g, 0, 2, h * 0.62, PALETTE.yellow, 0.95);
  g.restore();
}

function paintMoon(g, x, y, radius) {
  const halo = g.createRadialGradient(x, y, radius * 0.4, x, y, radius * 3.4);
  halo.addColorStop(0, tint(PALETTE.pink, 0.30));
  halo.addColorStop(1, tint(PALETTE.pink, 0));
  g.fillStyle = halo;
  g.fillRect(x - radius * 4, y - radius * 4, radius * 8, radius * 8);
  const disc = g.createLinearGradient(x - radius, y - radius, x + radius, y + radius);
  disc.addColorStop(0, "#ffe3f7");
  disc.addColorStop(1, "#b073ff");
  g.beginPath();
  g.arc(x, y, radius, 0, TAU);
  g.fillStyle = disc;
  g.fill();
  g.fillStyle = "rgba(90,50,140,0.22)"; // cráteres
  for (const [cx, cy, cr] of [[-0.3, -0.2, 0.22], [0.35, 0.15, 0.16], [-0.05, 0.45, 0.12]]) {
    g.beginPath();
    g.arc(x + cx * radius, y + cy * radius, cr * radius, 0, TAU);
    g.fill();
  }
}

// Fondo + ciudad + piso + borde + corona: todo lo que NO cambia entre fotogramas, pintado UNA vez.
function buildArenaLayer(width, height, radiusPx) {
  const layer = makeLayer(width, height);
  const g = layer.getContext("2d");
  const cx = width / 2;
  const cy = height / 2;

  const sky = g.createLinearGradient(0, 0, 0, height);
  sky.addColorStop(0, "#0b1030");
  sky.addColorStop(0.6, PALETTE.bg);
  sky.addColorStop(1, "#150d34");
  g.fillStyle = sky;
  g.fillRect(0, 0, width, height);

  const rand = seededRandom(SKYLINE_SEED);
  for (const band of SKYLINE_BANDS) paintSkylineBand(g, width, height, band, rand);
  // La luna va DESPUÉS de los edificios: la banda de arriba la tapaba por completo.
  paintMoon(g, width * MOON_X_FRACTION, height * MOON_Y_FRACTION, MOON_RADIUS_PX);

  // Viñeta: oscurece las esquinas para que la mirada vaya a la arena.
  const vignette = g.createRadialGradient(cx, cy, radiusPx, cx, cy, Math.hypot(cx, cy));
  vignette.addColorStop(0, "rgba(9,13,27,0)");
  vignette.addColorStop(1, "rgba(9,13,27,0.7)");
  g.fillStyle = vignette;
  g.fillRect(0, 0, width, height);

  // Props de azotea en las cuatro esquinas (el único sitio libre fuera del círculo). El de arriba
  // a la derecha puede quedar bajo el marcador, que es DOM y flota encima.
  const inset = width * PROP_INSET_FRACTION;
  const propSize = width * PROP_SIZE_FRACTION;
  paintVent(g, inset, inset, propSize);
  paintCrates(g, width - inset, inset, propSize);
  paintDish(g, inset, height - inset, propSize);
  paintBillboard(g, width - inset, height - inset, BILLBOARD_W_PX, BILLBOARD_H_PX, BILLBOARD_ANGLE_RAD);

  // Resplandor del borde hacia afuera (un degradado, no un desenfoque).
  const spill = g.createRadialGradient(cx, cy, radiusPx * 0.98, cx, cy, radiusPx * 1.22);
  spill.addColorStop(0, tint(PALETTE.cyan, 0.22));
  spill.addColorStop(1, tint(PALETTE.cyan, 0));
  g.fillStyle = spill;
  g.fillRect(0, 0, width, height);

  // Piso.
  const floor = g.createRadialGradient(cx, cy, 0, cx, cy, radiusPx);
  floor.addColorStop(0, "#1c2850");
  floor.addColorStop(0.7, PALETTE.arena);
  floor.addColorStop(1, "#0f1730");
  g.beginPath();
  g.arc(cx, cy, radiusPx, 0, TAU);
  g.fillStyle = floor;
  g.fill();

  // Piso: anillos, placas alternas y placa central octogonal, recortados al círculo.
  g.save();
  g.clip();
  g.strokeStyle = tint(PALETTE.cyan, GRID_ALPHA);
  g.lineWidth = 1;
  for (let i = 1; i <= GRID_RINGS; i++) {
    g.beginPath();
    g.arc(cx, cy, (radiusPx * i) / GRID_RINGS, 0, TAU);
    g.stroke();
  }
  const sector = TAU / FLOOR_SECTORS;
  const plate = radiusPx * FLOOR_PLATE_FRACTION;
  g.fillStyle = "rgba(255,255,255,0.022)";
  for (let i = 0; i < FLOOR_SECTORS; i += 2) { // placas alternas un poco más claras
    g.beginPath();
    g.moveTo(cx, cy);
    g.arc(cx, cy, radiusPx, i * sector + SECTOR_OFFSET, (i + 1) * sector + SECTOR_OFFSET);
    g.closePath();
    g.fill();
  }
  g.strokeStyle = tint(PALETTE.cyan, SEAM_ALPHA);
  g.lineWidth = 1.5;
  for (let i = 0; i < FLOOR_SECTORS; i++) { // costuras entre placas
    const a = i * sector + SECTOR_OFFSET;
    g.beginPath();
    g.moveTo(cx + Math.cos(a) * plate, cy + Math.sin(a) * plate);
    g.lineTo(cx + Math.cos(a) * radiusPx, cy + Math.sin(a) * radiusPx);
    g.stroke();
  }
  g.beginPath(); // placa central
  for (let i = 0; i < FLOOR_SECTORS; i++) {
    const a = i * sector + SECTOR_OFFSET;
    if (i === 0) g.moveTo(cx + Math.cos(a) * plate, cy + Math.sin(a) * plate);
    else g.lineTo(cx + Math.cos(a) * plate, cy + Math.sin(a) * plate);
  }
  g.closePath();
  g.fillStyle = tint(PALETTE.cyan, 0.04);
  g.fill();
  g.strokeStyle = tint(PALETTE.cyan, 0.3);
  g.lineWidth = 2;
  g.stroke();
  const inner = g.createRadialGradient(cx, cy, radiusPx * 0.8, cx, cy, radiusPx);
  inner.addColorStop(0, tint(PALETTE.cyan, 0));
  inner.addColorStop(1, tint(PALETTE.cyan, 0.16));
  g.fillStyle = inner;
  g.fillRect(0, 0, width, height);
  g.restore();

  paintCrown(g, cx, cy, radiusPx * CROWN_SIZE_FRACTION * 2);

  // Reborde exterior (el grosor de la plataforma) con su filo.
  const rim = g.createRadialGradient(cx, cy, radiusPx, cx, cy, radiusPx + RIM_WIDTH_PX);
  rim.addColorStop(0, "#1b2650");
  rim.addColorStop(1, "#080c1e");
  g.beginPath();
  g.arc(cx, cy, radiusPx + RIM_WIDTH_PX, 0, TAU);
  g.arc(cx, cy, radiusPx, 0, TAU, true); // sentido contrario: deja el hueco del piso
  g.fillStyle = rim;
  g.fill();
  g.beginPath();
  g.arc(cx, cy, radiusPx + RIM_WIDTH_PX, 0, TAU);
  g.strokeStyle = tint(PALETTE.cyan, 0.35);
  g.lineWidth = 1.2;
  g.stroke();

  // Anillo de neón en tramos: una línea fina continua debajo y los tramos con brillo encima
  // (uno de cada RING_BRIGHT_EVERY, más gordo y vivo). Aquí sí hay shadowBlur: se pinta una vez.
  const ringRadius = radiusPx - RING_INSET_PX;
  g.beginPath();
  g.arc(cx, cy, ringRadius, 0, TAU);
  g.strokeStyle = tint(PALETTE.cyan, 0.28);
  g.lineWidth = 1.5;
  g.stroke();
  const step = TAU / RING_SEGMENTS;
  g.save();
  g.shadowColor = PALETTE.cyan;
  g.shadowBlur = BORDER_GLOW_PX;
  for (let i = 0; i < RING_SEGMENTS; i++) {
    const bright = i % RING_BRIGHT_EVERY === 0;
    const from = i * step + (step * (1 - RING_SEGMENT_FILL)) / 2;
    g.beginPath();
    g.arc(cx, cy, ringRadius, from, from + step * RING_SEGMENT_FILL);
    g.strokeStyle = bright ? PALETTE.cyan : tint(PALETTE.cyan, 0.62);
    g.lineWidth = bright ? RING_BRIGHT_WIDTH_PX : RING_WIDTH_PX_SEG;
    g.stroke();
  }
  g.restore();
  return layer;
}

// ------------------------------------------------------------------ sprites (pre-renderizados)

// Accesorios por color: cada jugador tiene su silueta, no solo su tono. Registro {nombre: accesorio}
// (como los handlers y el codec) y no una cadena de if/else: otro accesorio es otra entrada.
// `layer`: "back" va detrás del cuerpo, "front" encima. `paint(g, r, ink)` dibuja con el relleno ya fijado.
const ROBOT_ACCESSORIES = {
  headphones: { layer: "back", paint(g, r, ink) {   // auriculares: dos copas y la diadema por encima
    for (const s of [-1, 1]) { roundedRect(g, s * 0.82 * r - 0.14 * r, -0.45 * r, 0.28 * r, 0.62 * r, 0.12 * r); g.fill(); }
    g.beginPath(); g.arc(0, -0.15 * r, 0.82 * r, Math.PI * 1.06, Math.PI * 1.94);
    g.strokeStyle = ink; g.lineWidth = r * 0.1; g.stroke();
  } },
  shoulders: { layer: "back", paint(g, r) {          // hombreras
    for (const s of [-1, 1]) { g.beginPath(); g.arc(s * 0.9 * r, 0.15 * r, 0.25 * r, 0, TAU); g.fill(); }
  } },
  horns: { layer: "back", paint(g, r) {               // cuernos
    for (const s of [-1, 1]) {
      g.beginPath(); g.moveTo(s * 0.5 * r, -0.76 * r); g.lineTo(s * 0.74 * r, -0.66 * r); g.lineTo(s * 0.98 * r, -1.34 * r); g.closePath(); g.fill();
    }
  } },
  ears: { layer: "back", paint(g, r) {                // orejas de gato
    for (const s of [-1, 1]) {
      g.beginPath(); g.moveTo(s * 0.5 * r, -0.8 * r); g.lineTo(s * 0.8 * r, -0.5 * r); g.lineTo(s * 1.02 * r, -1.02 * r); g.closePath(); g.fill();
    }
  } },
  fin: { layer: "front", paint(g, r) {                // cresta central, partida por el visor
    roundedRect(g, -0.09 * r, -0.76 * r, 0.18 * r, 0.2 * r, 0.06 * r); g.fill();
    roundedRect(g, -0.09 * r, 0.0, 0.18 * r, 0.56 * r, 0.06 * r); g.fill();
  } },
  wings: { layer: "back", paint(g, r) {                // alerones traseros
    for (const s of [-1, 1]) {
      g.beginPath(); g.moveTo(s * 0.7 * r, 0); g.lineTo(s * 1.2 * r, 0.3 * r); g.lineTo(s * 1.04 * r, 0.72 * r); g.lineTo(s * 0.7 * r, 0.56 * r); g.closePath(); g.fill();
    }
  } },
  stripes: { layer: "front", paint(g, r) {              // franjas de carreras en la parte baja
    g.fillRect(-0.7 * r, 0.2 * r, 1.4 * r, 0.07 * r);
    g.fillRect(-0.7 * r, 0.36 * r, 1.4 * r, 0.07 * r);
  } },
  antenna: { layer: "back", paint(g, r, ink) {         // antena central con bola grande
    g.beginPath(); g.moveTo(0, -0.8 * r); g.lineTo(0, -1.38 * r); g.strokeStyle = ink; g.lineWidth = r * 0.1; g.stroke();
    g.beginPath(); g.arc(0, -1.42 * r, 0.17 * r, 0, TAU); g.fill();
  } },
};
// Qué accesorio lleva cada color: son los 8 de PLAYER_COLORS en config.py (en minúsculas, como llegan).
// Un color que no esté aquí (otra paleta) simplemente no lleva accesorio.
const ACCESSORY_BY_COLOR = {
  "#00e5ff": "headphones", "#4d7cff": "shoulders", "#a15cff": "horns", "#ff3cac": "ears",
  "#ff3b4e": "fin", "#ff9a1f": "wings", "#f9f871": "stripes", "#5cff4a": "antenna",
};

function accessoryFor(color) {
  return ROBOT_ACCESSORIES[ACCESSORY_BY_COLOR[color.toLowerCase()]] ?? null;
}

// Pinta un accesorio con el relleno de la silueta (blanco liso) o el del jugador (con brillo).
function paintAccessory(g, r, accessory, color, silhouette) {
  if (!accessory) return;
  const ink = silhouette ? "#fff" : tint(color, 0.92);
  g.fillStyle = ink;
  g.shadowColor = silhouette ? "transparent" : color;
  g.shadowBlur = silhouette ? 0 : r * 0.35;
  accessory.paint(g, r, ink);
  g.shadowBlur = 0;
}

// Robot visto desde arriba, mirando hacia -y. `silhouette` lo pinta todo en blanco liso (para el destello).
function paintRobot(g, r, color, silhouette, accessory) {
  const R = ROBOT;
  const left = -R.halfWidth * r;
  const top = R.top * r;
  const bodyW = R.halfWidth * 2 * r;
  const bodyH = (R.bottom - R.top) * r;

  g.lineCap = "round";
  g.lineJoin = "round";
  for (const side of [-1, 1]) { // antenas, detrás del cuerpo
    g.beginPath();
    g.moveTo(side * R.antennaBaseX * r, R.antennaBaseY * r);
    g.lineTo(side * R.antennaTipX * r, R.antennaTipY * r);
    g.lineWidth = Math.max(1.6, r * 0.09);
    g.strokeStyle = silhouette ? "#fff" : color;
    g.shadowColor = silhouette ? "transparent" : color;
    g.shadowBlur = silhouette ? 0 : r * R.glowBlur * 0.6;
    g.stroke();
    g.beginPath();
    g.arc(side * R.antennaTipX * r, R.antennaTipY * r, R.tipRadius * r, 0, TAU);
    g.fillStyle = silhouette ? "#fff" : PALETTE.text;
    g.fill();
  }

  if (accessory?.layer === "back") paintAccessory(g, r, accessory, color, silhouette);

  roundedRect(g, left, top, bodyW, bodyH, R.corner * r);
  if (silhouette) {
    g.shadowBlur = 0;
    g.fillStyle = "#fff";
    g.fill();
    if (accessory?.layer === "front") paintAccessory(g, r, accessory, color, true);
    return;
  }
  g.shadowColor = color;
  g.shadowBlur = r * R.glowBlur;
  g.fillStyle = "#0b1126";
  g.fill();                     // base oscura: el resplandor se proyecta desde aquí
  g.shadowBlur = 0;
  const tintFill = g.createLinearGradient(0, top, 0, top + bodyH);
  tintFill.addColorStop(0, tint(color, 0.55));
  tintFill.addColorStop(1, tint(color, 0.16));
  g.fillStyle = tintFill;
  g.fill();
  g.lineWidth = Math.max(2, r * 0.12);
  g.strokeStyle = color;
  g.shadowColor = color;
  g.shadowBlur = r * 0.45;
  g.stroke();                   // borde brillante
  g.shadowBlur = 0;

  g.fillStyle = tint(color, 0.85); // tobera trasera
  roundedRect(g, -R.thrusterHalfWidth * r, R.thrusterY * r, R.thrusterHalfWidth * 2 * r, r * 0.12, r * 0.06);
  g.fill();

  roundedRect(g, -R.visorHalfWidth * r, R.visorTop * r, R.visorHalfWidth * 2 * r, (R.visorBottom - R.visorTop) * r, R.visorCorner * r);
  g.fillStyle = "#060913";
  g.fill();
  g.lineWidth = 1.2;
  g.strokeStyle = tint(color, 0.9);
  g.stroke();

  g.fillStyle = color;           // dos ojos
  g.shadowColor = color;
  g.shadowBlur = r * 0.3;
  for (const side of [-1, 1]) {
    g.beginPath();
    g.arc(side * R.eyeX * r, R.eyeY * r, R.eyeRadius * r, 0, TAU);
    g.fill();
  }
  g.shadowBlur = 0;
  if (accessory?.layer === "front") paintAccessory(g, r, accessory, color, false);
}

class Sprites {
  constructor() {
    this.r = 0;
    this.robots = new Map();   // color -> sprite
    this.halos = new Map();
    this.dots = new Map();
    this.squares = new Map();
    this.beamSprite = null;
    this.starSprite = null;
    this.silhouettes = new Map();
    this.labels = new Map();   // nombre -> { canvas, w, h }
    this.measure = makeLayer(1, 1).getContext("2d");
  }

  get side() { return Math.ceil(this.r * ROBOT.spriteSide); }

  // Los sprites dependen del radio en píxeles: si cambia (otra arena) se reconstruyen.
  setDiscRadius(r) {
    const rounded = Math.round(r * 2) / 2;
    if (rounded === this.r) return;
    this.r = rounded;
    this.robots.clear();
    this.halos.clear();
    this.silhouettes.clear();
  }

  robot(color) {
    let sprite = this.robots.get(color);
    if (!sprite) {
      sprite = makeLayer(this.side, this.side);
      const g = sprite.getContext("2d");
      g.translate(this.side / 2, this.side / 2);
      paintRobot(g, this.r, color, false, accessoryFor(color));
      this.robots.set(color, sprite);
    }
    return sprite;
  }

  // Silueta blanca del robot (para el destello del golpe): una por color, porque el accesorio cambia la forma.
  white(color) {
    let sprite = this.silhouettes.get(color);
    if (!sprite) {
      sprite = makeLayer(this.side, this.side);
      const g = sprite.getContext("2d");
      g.translate(this.side / 2, this.side / 2);
      paintRobot(g, this.r, color, true, accessoryFor(color));
      this.silhouettes.set(color, sprite);
    }
    return sprite;
  }

  halo(color) {
    let sprite = this.halos.get(color);
    if (!sprite) {
      const radius = this.r * HALO_RADIUS_FACTOR;
      const side = Math.ceil(radius * 2);
      sprite = makeLayer(side, side);
      const g = sprite.getContext("2d");
      const gradient = g.createRadialGradient(side / 2, side / 2, 0, side / 2, side / 2, radius);
      gradient.addColorStop(0, tint(color, 0.65));
      gradient.addColorStop(0.45, tint(color, 0.22));
      gradient.addColorStop(1, tint(color, 0));
      g.fillStyle = gradient;
      g.fillRect(0, 0, side, side);
      this.halos.set(color, sprite);
    }
    return sprite;
  }

  dot(color) {
    let sprite = this.dots.get(color);
    if (!sprite) {
      const side = PARTICLE_SPRITE_PX;
      sprite = makeLayer(side, side);
      const g = sprite.getContext("2d");
      const gradient = g.createRadialGradient(side / 2, side / 2, 0, side / 2, side / 2, side / 2);
      gradient.addColorStop(0, "rgba(255,255,255,1)");
      gradient.addColorStop(0.25, tint(color, 0.95));
      gradient.addColorStop(1, tint(color, 0));
      g.fillStyle = gradient;
      g.fillRect(0, 0, side, side);
      this.dots.set(color, sprite);
    }
    return sprite;
  }

  // Estrella de cuatro puntas amarilla (brillo horneado).
  star() {
    if (!this.starSprite) {
      const side = STAR_SPRITE_PX;
      this.starSprite = makeLayer(side, side);
      const g = this.starSprite.getContext("2d");
      const c = side / 2;
      g.translate(c, c);
      g.beginPath();
      for (let i = 0; i < 8; i++) {
        const radius = i % 2 === 0 ? c - 3 : (c - 3) * 0.32;
        const a = (i / 8) * TAU - Math.PI / 2;
        if (i === 0) g.moveTo(Math.cos(a) * radius, Math.sin(a) * radius);
        else g.lineTo(Math.cos(a) * radius, Math.sin(a) * radius);
      }
      g.closePath();
      g.shadowColor = PALETTE.yellow;
      g.shadowBlur = 4;
      g.fillStyle = PALETTE.yellow;
      g.fill();
    }
    return this.starSprite;
  }

  // Chispa cuadrada, estilo píxel: núcleo blanco, cuerpo de color y un poco de brillo (horneado).
  square(color) {
    let sprite = this.squares.get(color);
    if (!sprite) {
      const side = SQUARE_SPRITE_PX;
      sprite = makeLayer(side, side);
      const g = sprite.getContext("2d");
      g.shadowColor = color;
      g.shadowBlur = 5;
      g.fillStyle = color;
      g.fillRect(5, 5, side - 10, side - 10);
      g.shadowBlur = 0;
      g.fillStyle = "#fff";
      g.fillRect(8, 8, side - 16, side - 16);
      this.squares.set(color, sprite);
    }
    return sprite;
  }

  // Columna de luz violeta (la misma para todos): campana horizontal x degradado vertical.
  beam() {
    if (!this.beamSprite) {
      const w = BEAM_W_PX;
      const h = BEAM_H_PX;
      this.beamSprite = makeLayer(w, h);
      const g = this.beamSprite.getContext("2d");
      const across = g.createLinearGradient(0, 0, w, 0);
      across.addColorStop(0, tint(PALETTE.violet, 0));
      across.addColorStop(0.3, tint(PALETTE.violet, 0.75));
      across.addColorStop(0.5, "rgba(236,226,255,1)");
      across.addColorStop(0.7, tint(PALETTE.violet, 0.75));
      across.addColorStop(1, tint(PALETTE.violet, 0));
      g.fillStyle = across;
      g.fillRect(0, 0, w, h);
      g.globalCompositeOperation = "destination-in"; // el alfa se desvanece hacia arriba
      const up = g.createLinearGradient(0, 0, 0, h);
      up.addColorStop(0, "rgba(0,0,0,0)");
      up.addColorStop(1, "rgba(0,0,0,1)");
      g.fillStyle = up;
      g.fillRect(0, 0, w, h);
    }
    return this.beamSprite;
  }

  // Nombre con contorno oscuro, dibujado una vez y reutilizado: fillText + strokeText cada
  // fotograma para 8 nombres es trabajo que se repite sin cambiar nada.
  label(text) {
    let label = this.labels.get(text);
    if (!label) {
      if (this.labels.size >= LABEL_CACHE_MAX) this.labels.clear();
      this.measure.font = NAME_FONT;
      const w = Math.ceil(this.measure.measureText(text).width) + LABEL_PAD_PX * 2 + NAME_STROKE_PX;
      const h = 12 + LABEL_PAD_PX * 2 + NAME_STROKE_PX;
      const canvas = makeLayer(w, h);
      const g = canvas.getContext("2d");
      g.font = NAME_FONT;
      g.textAlign = "center";
      g.textBaseline = "middle";
      g.lineJoin = "round";
      g.lineWidth = NAME_STROKE_PX;
      g.strokeStyle = tint(PALETTE.bg, 0.95);
      g.strokeText(text, w / 2, h / 2);
      g.fillStyle = PALETTE.text;
      g.fillText(text, w / 2, h / 2);
      label = { canvas, w, h };
      this.labels.set(text, label);
    }
    return label;
  }

  // Cuando la fuente termina de cargar, los nombres horneados con la de respaldo ya no valen.
  clearLabels() { this.labels.clear(); }
}

// ------------------------------------------------------------------ partículas

class ParticlePool {
  constructor(max) {
    this.max = max;
    this.count = 0;
    this.cursor = 0;
    this.x = new Float32Array(max);
    this.y = new Float32Array(max);
    this.vx = new Float32Array(max);
    this.vy = new Float32Array(max);
    this.age = new Float32Array(max);
    this.life = new Float32Array(max);
    this.size = new Float32Array(max);
    this.sprite = new Array(max).fill(null);
  }

  emit(x, y, vx, vy, life, size, sprite) {
    let i;
    if (this.count < this.max) {
      i = this.count++;
    } else { // lleno: el tope es duro, se pisa una (en anillo) en vez de crecer
      i = this.cursor;
      this.cursor = (this.cursor + 1) % this.max;
    }
    this.x[i] = x; this.y[i] = y; this.vx[i] = vx; this.vy[i] = vy;
    this.age[i] = 0; this.life[i] = life; this.size[i] = size; this.sprite[i] = sprite;
  }

  // Chispas hacia atrás de una embestida (opuestas al rumbo).
  burstDash(x, y, heading, color, sprites) {
    const dot = sprites.dot(color);
    for (let n = 0; n < DASH_PARTICLES; n++) {
      const angle = heading + Math.PI + (Math.random() - 0.5) * DASH_SPREAD_RAD;
      const speed = mixNum(DASH_SPEED_MIN, DASH_SPEED_MAX, Math.random());
      this.emit(x, y, Math.cos(angle) * speed, Math.sin(angle) * speed,
        mixNum(DASH_LIFE_MIN_S, DASH_LIFE_MAX_S, Math.random()),
        mixNum(PARTICLE_SIZE_MIN, PARTICLE_SIZE_MAX, Math.random()), dot);
    }
  }

  // Explosión radial al eliminar a alguien.
  burstElimination(x, y, color, sprites) {
    const dot = sprites.dot(color);
    const square = sprites.square(color);
    for (let n = 0; n < ELIM_PARTICLES; n++) {
      const angle = Math.random() * TAU;
      const speed = mixNum(ELIM_SPEED_MIN, ELIM_SPEED_MAX, Math.random());
      this.emit(x, y, Math.cos(angle) * speed, Math.sin(angle) * speed,
        mixNum(ELIM_LIFE_MIN_S, ELIM_LIFE_MAX_S, Math.random()),
        mixNum(PARTICLE_SIZE_MIN, PARTICLE_SIZE_MAX, Math.random()), n % 2 ? dot : square); // mitad redondas, mitad "píxeles"
    }
  }

  update(dt) {
    const step = Math.min(dt, PARTICLE_DT_MAX_S);
    const drag = Math.exp(-PARTICLE_DRAG_PER_S * step);
    for (let i = this.count - 1; i >= 0; i--) {
      this.age[i] += step;
      if (this.age[i] >= this.life[i]) {
        const last = --this.count; // se sustituye por la última: sin huecos ni desplazar el arreglo
        if (i !== last) {
          this.x[i] = this.x[last]; this.y[i] = this.y[last];
          this.vx[i] = this.vx[last]; this.vy[i] = this.vy[last];
          this.age[i] = this.age[last]; this.life[i] = this.life[last];
          this.size[i] = this.size[last]; this.sprite[i] = this.sprite[last];
        }
        this.sprite[last] = null;
        continue;
      }
      this.vx[i] *= drag;
      this.vy[i] *= drag;
      this.x[i] += this.vx[i] * step;
      this.y[i] += this.vy[i] * step;
    }
    if (this.cursor >= this.count) this.cursor = 0;
  }

  draw(ctx) {
    if (this.count === 0) return;
    ctx.globalCompositeOperation = "lighter"; // se fija UNA vez para todo el lote
    for (let i = 0; i < this.count; i++) {
      const t = 1 - this.age[i] / this.life[i];
      const s = this.size[i] * (0.4 + 0.6 * t);
      ctx.globalAlpha = t;
      ctx.drawImage(this.sprite[i], this.x[i] - s / 2, this.y[i] - s / 2, s, s);
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
  }

  clear() { this.count = 0; this.cursor = 0; this.sprite.fill(null); }
}

// Columnas de luz donde cae alguien: pocas (BEAM_MAX), así que un arreglo basta.
class EliminationBeams {
  constructor(max) {
    this.max = max;
    this.count = 0;
    this.cursor = 0;
    this.x = new Float32Array(max);
    this.y = new Float32Array(max);
    this.age = new Float32Array(max);
  }

  add(x, y) {
    let i;
    if (this.count < this.max) i = this.count++;
    else { i = this.cursor; this.cursor = (this.cursor + 1) % this.max; }
    this.x[i] = x; this.y[i] = y; this.age[i] = 0;
  }

  update(dt) {
    const step = Math.min(dt, PARTICLE_DT_MAX_S);
    for (let i = this.count - 1; i >= 0; i--) {
      this.age[i] += step;
      if (this.age[i] < BEAM_LIFE_S) continue;
      const last = --this.count;
      if (i !== last) { this.x[i] = this.x[last]; this.y[i] = this.y[last]; this.age[i] = this.age[last]; }
    }
    if (this.cursor >= this.count) this.cursor = 0;
  }

  draw(ctx, sprites) {
    if (this.count === 0) return;
    const beam = sprites.beam();
    for (let i = 0; i < this.count; i++) {
      const t = this.age[i] / BEAM_LIFE_S;
      const rise = 1 - (1 - t) ** 3;           // sube rápido y se frena
      const w = BEAM_W_PX * (1 - 0.55 * t);    // se afina al apagarse
      const h = BEAM_H_PX * (0.45 + 0.55 * rise);
      ctx.globalCompositeOperation = "lighter";
      ctx.globalAlpha = 1 - t;
      ctx.drawImage(beam, this.x[i] - w / 2, this.y[i] - h, w, h);
      ctx.globalCompositeOperation = "source-over";
      glowArc(ctx, this.x[i], this.y[i], BEAM_RING_START_PX + BEAM_RING_GROW_PX * rise, 1, PALETTE.violet, 2.5);
    }
    ctx.globalAlpha = 1;
  }

  clear() { this.count = 0; this.cursor = 0; }
}

// ------------------------------------------------------------------ estado de animación por disco

class DiscFx {
  constructor(now, alive, heading) {
    this.phase = Math.random() * TAU;   // desfasa el balanceo: 8 robots no respiran al unísono
    this.heading = heading;             // rumbo en radianes (0 = derecha)
    this.lastPx = NaN;
    this.lastPy = NaN;
    this.speedPx = 0;                   // px/s suavizados, para apagar el balanceo al moverse
    this.wasAlive = alive;
    this.deadAt = alive ? -1 : now - ELIM_MS; // si lo vemos por primera vez ya muerto, sin animación
    this.flashAt = -Infinity;
    this.dashUntil = 0;
    this.vx = 0;                        // velocidad por tick del último par de estados observado
    this.vy = 0;
    this.vTick = -1;                    // tick donde terminó ese par (para comprobar que son contiguos)
    this.dashing = false;
    this.trail = [];                    // [{x, y, rot}], los últimos TRAIL_LEN fotogramas de embestida
    this.seen = 0;
  }

  // Actualiza rumbo y velocidad en pantalla. `aimAngle`: rumbo forzado (el propio al cargar) o null.
  track(px, py, dt, aimAngle) {
    if (!Number.isNaN(this.lastPx)) {
      const dx = px - this.lastPx;
      const dy = py - this.lastPy;
      const dist = Math.hypot(dx, dy);
      if (dt > 0) this.speedPx += (dist / dt - this.speedPx) * SPEED_SMOOTHING;
      const target = aimAngle ?? (dist > HEADING_MIN_MOVE_PX ? Math.atan2(dy, dx) : null);
      if (target !== null) this.heading += angleDiff(target, this.heading) * (1 - Math.exp(-dt * HEADING_RATE));
    }
    this.lastPx = px;
    this.lastPy = py;
  }

  updateTrail(px, py, rot) {
    if (this.dashing) {
      this.trail.push({ x: px, y: py, rot });
      if (this.trail.length > TRAIL_LEN) this.trail.shift();
    } else if (this.trail.length > 0) {
      this.trail.shift(); // la estela se desvanece sola al terminar la embestida
    }
  }
}

class FxTracker {
  constructor() { this.map = new Map(); this.frame = 0; }
  nextFrame() { this.frame++; }
  peek(id) { return this.map.get(id) ?? null; }
  get(id, now, alive, heading) {
    let fx = this.map.get(id);
    if (!fx) {
      fx = new DiscFx(now, alive, heading);
      this.map.set(id, fx);
    }
    fx.seen = this.frame;
    return fx;
  }
  // Olvida a quien ya no aparece en el estado (salió de la sala o empezó otra ronda).
  prune(visibleCount) {
    if (this.map.size === visibleCount) return;
    for (const [id, fx] of this.map) if (fx.seen !== this.frame) this.map.delete(id);
  }
  clear() { this.map.clear(); }
}

// ------------------------------------------------------------------ dibujo de un disco

// Anillo con "brillo": un trazo ancho y tenue debajo de uno fino y vivo. Sin shadowBlur.
function glowArc(ctx, x, y, radius, fraction, color, width) {
  if (fraction <= 0) return;
  const start = -Math.PI / 2;
  const end = start + Math.min(fraction, 1) * TAU;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.arc(x, y, radius, start, end);
  ctx.strokeStyle = tint(color, 0.28);
  ctx.lineWidth = width * RING_GLOW_FACTOR;
  ctx.stroke();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.stroke();
}

// Flecha de puntería del propio robot: hacia dónde saldría la embestida.
function drawAimArrow(ctx, px, py, r, ux, uy, strong) {
  const fromX = px + ux * r * 1.3;
  const fromY = py + uy * r * 1.3;
  const toX = px + ux * r * 2.3;
  const toY = py + uy * r * 2.3;
  const head = 9;
  const half = 0.6;
  ctx.globalAlpha = strong ? 1 : 0.45;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(fromX, fromY);
  ctx.lineTo(toX, toY);
  ctx.strokeStyle = tint(PALETTE.cyan, 0.3);
  ctx.lineWidth = 8;
  ctx.stroke();
  ctx.strokeStyle = PALETTE.text;
  ctx.lineWidth = 2.5;
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(toX + ux * head, toY + uy * head);
  ctx.lineTo(toX - uy * head * half, toY + ux * head * half);
  ctx.lineTo(toX + uy * head * half, toY - ux * head * half);
  ctx.closePath();
  ctx.fillStyle = PALETTE.text;
  ctx.fill();
  ctx.globalAlpha = 1;
}

// Triángulo amarillo sobre el propio robot: "tú estás aquí".
function drawOwnMarker(ctx, px, py, r, now) {
  const bob = Math.sin((now / 1000) * MARKER_BOB_HZ * TAU) * MARKER_BOB_PX;
  const y = py - r * Math.abs(ROBOT.antennaTipY) - 10 + bob; // justo encima de las antenas
  ctx.beginPath();
  ctx.moveTo(px - MARKER_SIZE_PX, y - MARKER_SIZE_PX);
  ctx.lineTo(px + MARKER_SIZE_PX, y - MARKER_SIZE_PX);
  ctx.lineTo(px, y + MARKER_SIZE_PX * 0.2);
  ctx.closePath();
  ctx.lineJoin = "round";
  ctx.strokeStyle = tint(PALETTE.yellow, 0.3);
  ctx.lineWidth = 7;
  ctx.stroke();
  ctx.fillStyle = PALETTE.yellow;
  ctx.fill();
}

/*
 * Dibuja un robot con todas sus animaciones. `d`:
 *   px, py, r      posición y radio en píxeles
 *   color          color del jugador
 *   charge, alive  del estado interpolado
 *   own            true si es el propio
 *   aim            {ux, uy} unitario o null (solo el propio)
 *   cooldown       0..1 fracción de recarga restante (solo el propio)
 *   name           texto de la etiqueta
 * No hay `await` ni estado compartido: es dibujo puro sobre `ctx`.
 */
function paintDisc(ctx, sprites, fx, d, now) {
  const { px, py, r, color, charge, alive, own } = d;
  const sprite = sprites.robot(color);
  const half = sprites.side / 2;

  // Eliminación: progreso 0..1 con salida suave (encoge, gira y se apaga).
  let eased = 0;
  if (!alive) {
    const p = clamp01((now - fx.deadAt) / ELIM_MS);
    eased = 1 - (1 - p) ** 3;
  }
  const elimScale = 1 - (1 - ELIM_END_SCALE) * eased;
  const bodyAlpha = 1 - (1 - DEAD_ALPHA) * eased;
  const live = 1 - eased; // 1 vivo, 0 apagado: gobierna halo, anillos y destellos

  // Destello por golpe: 1 -> 0 en FLASH_MS.
  const flash = clamp01(1 - (now - fx.flashAt) / FLASH_MS) * live;

  // Balanceo en reposo (se apaga al moverse o cargar) y "respiración".
  const t = now / 1000;
  const idle = clamp01(1 - fx.speedPx / SWAY_FADE_PX_S) * (charge > 0 ? 0 : 1) * live;
  const sway = Math.sin(t * SWAY_HZ * TAU + fx.phase) * SWAY_RAD * idle;
  const breathe = 1 + Math.sin(t * SWAY_HZ * 2 * TAU + fx.phase) * BREATHE_SCALE * idle;
  const spin = alive ? 0 : eased * ELIM_SPIN;
  const rot = fx.heading + Math.PI / 2 + sway + spin;

  const scale = elimScale * breathe * (1 + CHARGE_SCALE * charge) * (1 + FLASH_SCALE * flash);
  const shake = charge > 0 && alive ? CHARGE_SHAKE_PX * charge : 0;
  const x = px + (shake ? Math.sin(now * 0.09 + fx.phase) * shake : 0);
  const y = py + (shake ? Math.cos(now * 0.11 + fx.phase) * shake : 0);

  fx.updateTrail(x, y, rot);

  // --- capa aditiva: estela y halo ---
  ctx.globalCompositeOperation = "lighter";
  const n = fx.trail.length;
  for (let i = 0; i < n - 1; i++) { // el último fantasma es el propio robot
    const g = fx.trail[i];
    const k = (i + 1) / n;
    const s = mixNum(0.7, 1, k);
    const c = Math.cos(g.rot) * s;
    const sn = Math.sin(g.rot) * s;
    ctx.globalAlpha = TRAIL_ALPHA * k;
    ctx.setTransform(c, sn, -sn, c, g.x, g.y);
    ctx.drawImage(sprite, -half, -half);
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  if (fx.dashing && alive) { // líneas de velocidad: hacia atrás del rumbo, cada una con su propio ritmo
    const bx = -Math.cos(fx.heading);
    const by = -Math.sin(fx.heading);
    ctx.lineCap = "round";
    ctx.lineWidth = 2.5;
    for (let i = 0; i < SPEED_LINES; i++) {
      const phase = (now / SPEED_LINE_CYCLE_MS + i * 0.37) % 1;
      const lateral = (i - (SPEED_LINES - 1) / 2) * r * 0.38;
      const sx = x + bx * r * 0.7 - by * lateral;
      const sy = y + by * r * 0.7 + bx * lateral;
      const len = r * (1.2 + 1.8 * phase);
      ctx.globalAlpha = SPEED_LINE_ALPHA * (1 - phase);
      ctx.strokeStyle = i % 2 === 0 ? PALETTE.text : color;
      ctx.beginPath();
      ctx.moveTo(sx, sy);
      ctx.lineTo(sx + bx * len, sy + by * len);
      ctx.stroke();
    }
  }
  if (live > 0.02) {
    const halo = sprites.halo(color);
    const haloAlpha = (HALO_BASE_ALPHA + HALO_CHARGE_ALPHA * charge + 0.5 * flash) * live;
    const haloSize = halo.width * (1 + HALO_CHARGE_GROW * charge + 0.2 * flash);
    ctx.globalAlpha = Math.min(1, haloAlpha);
    ctx.drawImage(halo, x - haloSize / 2, y - haloSize / 2, haloSize, haloSize);
  }
  ctx.globalCompositeOperation = "source-over";

  // --- anillos (carga y recarga) ---
  if (alive) {
    ctx.globalAlpha = 1;
    if (charge > 0) {
      const full = charge >= 1;
      glowArc(ctx, x, y, r * scale + CHARGE_RING_GAP_PX, charge, full ? PALETTE.text : color,
        RING_WIDTH_PX + (full ? 1 + Math.sin(now * 0.025) : 0));
    }
    if (own && d.cooldown > 0) glowArc(ctx, px, py, r + COOLDOWN_RING_GAP_PX, d.cooldown, PALETTE.violet, RING_WIDTH_PX);
    if (own && d.aim) drawAimArrow(ctx, px, py, r, d.aim.ux, d.aim.uy, charge > 0);
  }

  // --- el robot ---
  const c = Math.cos(rot) * scale;
  const sn = Math.sin(rot) * scale;
  ctx.globalAlpha = bodyAlpha;
  ctx.setTransform(c, sn, -sn, c, x, y);
  ctx.drawImage(sprite, -half, -half);
  if (flash > 0) { // silueta blanca encima: el "golpe"
    ctx.globalAlpha = flash * 0.9;
    ctx.drawImage(sprites.white(color), -half, -half);
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);

  if (flash > 0) { // onda de choque
    ctx.globalAlpha = flash * 0.8;
    ctx.beginPath();
    ctx.arc(px, py, r * (1 + SHOCK_GROW * (1 - flash)), 0, TAU);
    ctx.strokeStyle = PALETTE.text;
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  // --- estrellitas del golpe: giran en una órbita aplastada sobre la cabeza ---
  const sinceHit = now - fx.flashAt;
  if (alive && sinceHit < STAR_MS) {
    const star = sprites.star();
    const fade = clamp01((STAR_MS - sinceHit) / (STAR_MS * 0.35)); // se apagan en el último tercio
    ctx.globalAlpha = fade;
    for (let i = 0; i < STAR_COUNT; i++) {
      const a = (now / 1000) * STAR_ORBIT_HZ * TAU + (i / STAR_COUNT) * TAU;
      const size = STAR_SIZE_PX * (0.85 + 0.15 * Math.sin(a * 2));
      ctx.drawImage(star, px + Math.cos(a) * r * 0.85 - size / 2, py - r * 1.5 + Math.sin(a) * r * 0.22 - size / 2, size, size);
    }
  }

  // --- marcador y nombre ---
  ctx.globalAlpha = 1;
  if (own && alive) drawOwnMarker(ctx, px, py, r, now);
  const label = sprites.label(d.name);
  ctx.globalAlpha = alive ? 1 : 0.4;
  ctx.drawImage(label.canvas, px - label.w / 2, py + r + NAME_GAP_PX); // debajo del robot y de sus anillos
  ctx.globalAlpha = 1;
}

// ------------------------------------------------------------------ medidor de FPS

// Solo con `?fps` en la URL. Mide el intervalo REAL entre fotogramas (lo que ve el jugador)
// y el tiempo de CPU de dibujar; el "1 % peor" es lo que se siente como tirones.
class PerfMeter {
  constructor(element) {
    this.element = element;
    this.deltas = new Float32Array(PERF_WINDOW_FRAMES);
    this.filled = 0;
    this.next = 0;
    this.lastNow = 0;
    this.drawMs = 0;
    this.lastReport = 0;
    this.result = { fps: 0, low1: 0, drawMs: 0, frames: 0, long: 0, worst: 0 };
    window.__perf = this.result; // para leerlo desde fuera (DevTools o un arnés de pruebas)
  }

  frame(now, drawMs) {
    if (this.lastNow) {
      this.deltas[this.next] = now - this.lastNow;
      this.next = (this.next + 1) % PERF_WINDOW_FRAMES;
      this.filled = Math.min(this.filled + 1, PERF_WINDOW_FRAMES);
      this.result.frames++;
      const delta = now - this.lastNow;
      if (delta > LONG_FRAME_MS) this.result.long++;
      if (delta > this.result.worst) this.result.worst = delta;
    }
    this.lastNow = now;
    this.drawMs += (drawMs - this.drawMs) * 0.05;
    if (now - this.lastReport < PERF_REPORT_MS || this.filled < 10) return;
    this.lastReport = now;
    const sorted = Array.from(this.deltas.subarray(0, this.filled)).sort((a, b) => a - b);
    const mean = sorted.reduce((sum, v) => sum + v, 0) / sorted.length;
    const p99 = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.99))];
    this.result.fps = 1000 / mean;
    this.result.low1 = 1000 / p99;
    this.result.drawMs = this.drawMs;
    if (this.element) {
      this.element.textContent = `${this.result.fps.toFixed(0)} FPS • 1 % peor ${this.result.low1.toFixed(0)} • dibujo ${this.drawMs.toFixed(2)} ms`;
    }
  }
}
