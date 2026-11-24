"use strict";
/*
 * Sonido de StreetPunch: dos pistas de música y cuatro efectos. Sin librerías.
 *
 * - MÚSICA: se descarga ENTERA con fetch y se reproduce desde un Blob. El servidor no entiende
 *   `Range` (docs/protocolo.md §1): con un <audio src="/archivo"> el navegador pide trozos y
 *   aborta conexiones. Con el Blob hay una sola petición y el bucle sale de memoria.
 * - EFECTOS: se decodifican una vez a AudioBuffer (Web Audio) y cada disparo es un
 *   BufferSource nuevo: pueden solaparse (8 robots chocando) y empiezan sin latencia.
 * - AUTOPLAY: los navegadores no dejan sonar nada hasta que el jugador interactúa. `unlock()`
 *   se llama desde un gesto (enviar el nombre, primera tecla o clic). Antes de eso todo es mudo.
 * - Si un archivo no carga, el juego sigue sin ese sonido: el audio nunca debe romper la partida.
 * Los archivos y su origen están en client/CREDITS.txt. Para cambiar una pista, cambia una entrada de aquí.
 */

// Música: volumen 0..1 de cada pista. Medido (RMS, ya con el volumen aplicado): los efectos quedan
// entre -26 y -31 dB; la batalla a -28 dB y el vestíbulo a -33 dB. El archivo del vestíbulo es
// MUCHO más fuerte que el resto (RMS -15 dB, pico 1,0), por eso su volumen es tan bajo: la música
// de menú tiene que ser un fondo, no competir con nada.
const MUSIC_TRACKS = {
  lobby: { url: "/music-lobby.mp3", volume: 0.12 },
  battle: { url: "/music-battle.mp3", volume: 0.28 },
};
// Efectos: volumen base 0..1. `rate` acelera la reproducción (1 = normal).
const SFX_TRACKS = {
  hit: { url: "/sfx-hit.mp3", volume: 1.5 },   // el archivo es flojo (RMS 0,08 frente a 0,19 de la eliminación): se compensa
  eliminated: { url: "/sfx-eliminated.mp3", volume: 1 },
  charge: { url: "/sfx-charge.mp3", volume: 0.5 },
  win: { url: "/sfx-win.mp3", volume: 0.9 },
  whoosh: { url: "/sfx-whoosh.mp3", volume: 0.55 },   // el archivo llega al pico 1,0: a 0,55 queda a la par de los demás
};
// El sonido de carga dura 3,24 s y SUBE hasta su clímax al final, pero cargar a tope tarda
// CHARGE_MAX_S = 1 s (config.py). A 3,2x su clímax cae justo al llegar a carga completa. Si
// cambia CHARGE_MAX_S en el servidor, este número hay que reajustarlo (el cliente no lo recibe).
const CHARGE_PLAYBACK_RATE = 3.2;
const MUSIC_FADE_OUT_MS = 450;     // la pista que sale se apaga ENTERA antes de que entre la siguiente:
const MUSIC_FADE_IN_MS = 600;      // nunca suenan dos músicas a la vez (antes era un fundido cruzado)
const MUSIC_FADE_STEP_MS = 40;     // paso del fundido
const SFX_MIN_GAP_MS = 70;         // un mismo efecto no suena más de una vez cada 70 ms: 8 choques a la vez no deben saturar
const CHARGE_STOP_S = 0.06;        // al soltar la carga el sonido se apaga en ~60 ms, sin clic
const MUTE_STORAGE_KEY = "streetpunch.muted";

class GameAudio {
  constructor() {
    this.muted = readMutedPreference();
    this.context = null;              // AudioContext: se crea en el primer gesto
    this.sfxGain = null;
    this.buffers = new Map();         // nombre de efecto -> AudioBuffer
    this.music = new Map();           // nombre de pista -> HTMLAudioElement ya descargado
    this.musicLoading = new Map();    // nombre de pista -> Promise (para no descargar dos veces)
    this.wanted = null;               // pista que pide el juego ahora (null = silencio)
    this.intended = undefined;        // a qué transición se apuntó la última vez (undefined = ninguna todavía)
    this.sounding = null;             // pista que de verdad tiene volumen ahora
    this.chain = Promise.resolve();   // las transiciones se encadenan: una empieza cuando la anterior terminó
    this.lastPlayed = new Map();      // nombre de efecto -> instante del último disparo
    this.charge = null;               // { source, gain } de la carga en curso
  }

  get unlocked() { return this.context !== null; }

  // Debe llamarse desde un gesto del usuario (clic, tecla, envío del formulario).
  unlock() {
    if (this.context) {
      if (this.context.state === "suspended") this.context.resume().catch(() => {});
      this.applyMusic(); // reintenta si un play() anterior fue rechazado por falta de gesto
      return;
    }
    const Context = window.AudioContext ?? window.webkitAudioContext;
    if (!Context) return; // navegador sin Web Audio: el juego queda mudo, sin error
    this.context = new Context();
    this.sfxGain = this.context.createGain();
    this.sfxGain.gain.value = this.muted ? 0 : 1;
    this.sfxGain.connect(this.context.destination);
    for (const name of Object.keys(SFX_TRACKS)) this.loadEffect(name);
    // Las dos pistas se bajan ya (en el vestíbulo, sin nada que se pueda trabar): si la de la
    // batalla se pidiera al empezar la primera ronda, bajarla y decodificarla caería en plena partida.
    for (const name of Object.keys(MUSIC_TRACKS)) this.loadMusic(name);
    this.applyMusic();
  }

  async loadEffect(name) {
    try {
      const response = await fetch(SFX_TRACKS[name].url);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      this.buffers.set(name, await this.context.decodeAudioData(await response.arrayBuffer()));
    } catch (error) {
      console.warn(`Sin efecto "${name}":`, error.message);
    }
  }

  // ---------------------------------------------------------------- música

  // Pide que suene esta pista (o ninguna). Si ya suena, no hace nada; si es otra, fundido cruzado.
  setMusic(name) {
    this.wanted = name;
    this.applyMusic();
  }

  applyMusic() {
    if (!this.unlocked) return; // hasta el primer gesto no se puede sonar: quedará pendiente en `wanted`
    const target = this.muted ? null : this.wanted;
    if (target === this.intended) return;
    this.intended = target;
    // En cadena: aunque lleguen varios cambios seguidos (una ronda tras otra), cada transición
    // espera a que la anterior apague su pista. Es lo que evita que se solapen.
    this.chain = this.chain.then(() => this.transitionTo(target)).catch(() => {});
  }

  async transitionTo(target) {
    if (target !== this.intended) return;   // obsoleta: mientras esperaba se pidió otra cosa
    const current = this.sounding;
    if (current === target) return;
    if (current !== null) {
      const old = this.music.get(current);
      this.sounding = null;
      await fadeTo(old, 0, MUSIC_FADE_OUT_MS);
      old.pause();
    }
    if (target === null || target !== this.intended) return;
    const audio = await this.loadMusic(target);
    if (!audio || target !== this.intended) return;
    try {
      await audio.play();
    } catch {
      this.intended = undefined; // el navegador la rechazó (falta un gesto): el próximo unlock() reintenta
      return;
    }
    this.sounding = target;
    fadeTo(audio, MUSIC_TRACKS[target].volume, MUSIC_FADE_IN_MS);
  }

  loadMusic(name) {
    if (this.music.has(name)) return Promise.resolve(this.music.get(name));
    if (!this.musicLoading.has(name)) {
      const loading = fetch(MUSIC_TRACKS[name].url)
        .then((response) => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          return response.blob();
        })
        .then((blob) => {
          const audio = new Audio(URL.createObjectURL(blob));
          audio.loop = true;
          audio.volume = 0;
          this.music.set(name, audio);
          return audio;
        })
        .catch((error) => {
          console.warn(`Sin música "${name}":`, error.message);
          return null;
        })
        .finally(() => this.musicLoading.delete(name));
      this.musicLoading.set(name, loading);
    }
    return this.musicLoading.get(name);
  }

  // ---------------------------------------------------------------- efectos

  // Dispara un efecto. `volume` (0..1) lo escala; devuelve { source, gain } o null si no sonó.
  play(name, { volume = 1, rate = 1 } = {}) {
    const buffer = this.buffers.get(name);
    if (!this.context || !buffer || this.muted) return null;
    const now = performance.now();
    if (now - (this.lastPlayed.get(name) ?? -Infinity) < SFX_MIN_GAP_MS) return null;
    this.lastPlayed.set(name, now);
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.playbackRate.value = rate;
    const gain = this.context.createGain();
    gain.gain.value = SFX_TRACKS[name].volume * volume;
    source.connect(gain).connect(this.sfxGain);
    source.start();
    return { source, gain };
  }

  // La carga dura mientras se mantiene la tecla: empieza al pulsar y se corta al soltar.
  startCharge() {
    this.stopCharge();
    this.charge = this.play("charge", { rate: CHARGE_PLAYBACK_RATE });
  }

  stopCharge() {
    if (!this.charge) return;
    const { source, gain } = this.charge;
    this.charge = null;
    const t = this.context.currentTime;
    gain.gain.setTargetAtTime(0, t, CHARGE_STOP_S / 3);
    try { source.stop(t + CHARGE_STOP_S * 2); } catch { /* ya había terminado */ }
  }

  // ---------------------------------------------------------------- silencio

  setMuted(muted) {
    this.muted = muted;
    try { localStorage.setItem(MUTE_STORAGE_KEY, muted ? "1" : "0"); } catch { /* sin almacenamiento: no se recuerda */ }
    if (this.sfxGain) this.sfxGain.gain.value = muted ? 0 : 1;
    if (muted) this.stopCharge();
    this.applyMusic();
  }
}

function readMutedPreference() {
  try { return localStorage.getItem(MUTE_STORAGE_KEY) === "1"; } catch { return false; }
}

// Fundido de volumen de un <audio>. Devuelve una promesa que se cumple al terminar. Un fundido
// nuevo sobre el mismo elemento cancela al anterior y LIBERA a quien lo esperaba (si no, la
// cadena de transiciones se quedaría colgada).
function fadeTo(audio, target, ms) {
  audio.fadeResolve?.();
  clearInterval(audio.fadeTimer);
  return new Promise((resolve) => {
    audio.fadeResolve = resolve;
    const steps = Math.max(1, Math.round(ms / MUSIC_FADE_STEP_MS));
    const delta = (target - audio.volume) / steps;
    let left = steps;
    audio.fadeTimer = setInterval(() => {
      left -= 1;
      audio.volume = Math.min(1, Math.max(0, left <= 0 ? target : audio.volume + delta));
      if (left > 0) return;
      clearInterval(audio.fadeTimer);
      audio.fadeResolve = null;
      resolve();
    }, MUSIC_FADE_STEP_MS);
  });
}

const gameAudio = new GameAudio();
