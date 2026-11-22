"""Física de los discos: mueve y resuelve choques. Nada más (SRP).

No decide eliminaciones ni fases: eso es de World y de los RoundState.
Se ejecuta con paso fijo (dt = 1 / TICK_HZ), así que el resultado es
determinista: los mismos datos de entrada dan siempre la misma simulación,
lo que permite probarla con pytest.
"""

import math

from server.config import Config
from server.domain.disc import Disc
from server.domain.vector import Vec2


class Physics:
    def __init__(self, config: Config) -> None:
        self._cfg = config

    def step(self, discs: list[Disc], dt: float) -> None:
        """Avanza un tick. El orden importa:

        1. Fricción primero, para que la velocidad máxima caminando sea
           exactamente MAX_SPEED (si fuera al final, la recortaría siempre).
        2. Aceleración según la intención del jugador.
        3. Carga y embestida (la embestida sale a su velocidad completa).
        4. Tope global de velocidad -> garantía contra el tunneling.
        5. Mover y, al final, resolver choques con las posiciones nuevas.
        """
        cfg = self._cfg
        walk_friction = math.exp(-cfg.FRICTION_PER_S * dt)
        slide_friction = math.exp(-cfg.SLIDE_FRICTION_PER_S * dt)
        for disc in discs:
            # Dos fricciones: al caminar frena rápido (control preciso); por
            # encima de MAX_SPEED (embestida o golpe) frena menos, para que el
            # impulso llegue lejos. Se decide por la velocidad al INICIO del
            # tick, antes de aplicar nada: así el resultado no depende del orden.
            sliding = disc.vel.length() > cfg.MAX_SPEED
            disc.vel = disc.vel * (slide_friction if sliding else walk_friction)
            if disc.dashing and disc.vel.length() <= cfg.MAX_SPEED:
                disc.dashing = False  # la embestida ya se frenó: vuelve a masa normal
            self._accelerate(disc, dt)
            self._update_charge(disc, dt)
            self._cap_speed(disc)
            disc.pos = disc.pos + disc.vel * dt
        self.resolve_collisions(discs)

    @staticmethod
    def _is_charging(disc: Disc) -> bool:
        """Carga de verdad: tecla pulsada Y sin enfriamiento. En enfriamiento
        mantener la tecla no carga nada, y entonces tampoco debe dejar al disco
        clavado sin motivo."""
        return disc.charging and disc.cooldown == 0.0

    def _accelerate(self, disc: Disc, dt: float) -> None:
        # Cargando, el disco no acelera: las teclas solo cambian hacia dónde
        # apunta (Disc.facing, que ya actualiza set_move). La velocidad que
        # traiga se pierde por fricción. Un golpe recibido sí lo mueve: eso
        # lo hace la colisión, no esta aceleración.
        if disc.move_dir.is_zero() or self._is_charging(disc):
            return
        new_vel = disc.vel + disc.move_dir * (self._cfg.ACCELERATION * dt)
        # Caminando no se pasa de MAX_SPEED; pero si el disco ya iba más
        # rápido (embestida o golpe), acelerar tampoco lo hace ir más rápido.
        limit = max(self._cfg.MAX_SPEED, disc.vel.length())
        if new_vel.length() > limit:
            new_vel = new_vel.with_length(limit)
        disc.vel = new_vel

    def _update_charge(self, disc: Disc, dt: float) -> None:
        cfg = self._cfg
        disc.cooldown = max(0.0, disc.cooldown - dt)
        # En enfriamiento se puede mantener la tecla, pero la carga no sube.
        if self._is_charging(disc):
            disc.charge_time = min(disc.charge_time + dt, cfg.CHARGE_MAX_S)
        if disc.release_pending:
            disc.release_pending = False
            # Soltar sin carga suficiente (p. ej. en enfriamiento, o un toque
            # corto) no hace nada y no reinicia el enfriamiento.
            if disc.charge_time < cfg.MIN_CHARGE_S:
                disc.charge_time = 0.0   # toque corto: se descarta sin gastar enfriamiento
            elif disc.charge_time > 0.0:
                fraction = disc.charge_time / cfg.CHARGE_MAX_S
                disc.vel = disc.facing * (cfg.DASH_MAX_SPEED * fraction)
                disc.dashing = True
                disc.cooldown = cfg.DASH_COOLDOWN_S
                disc.charge_time = 0.0

    def _cap_speed(self, disc: Disc) -> None:
        if disc.vel.length() > self._cfg.DASH_MAX_SPEED:
            disc.vel = disc.vel.with_length(self._cfg.DASH_MAX_SPEED)

    def mass(self, disc: Disc) -> float:
        factor = self._cfg.DASH_MASS_FACTOR if disc.dashing else 1.0
        return self._cfg.DISC_MASS * factor

    def resolve_collisions(self, discs: list[Disc]) -> None:
        """Choque elástico entre cada par que se solapa: O(n²), con 12 discos
        son 66 pares por tick, despreciable frente a los 33 ms del tick."""
        for i in range(len(discs)):
            for j in range(i + 1, len(discs)):
                self._collide(discs[i], discs[j])

    def _collide(self, a: Disc, b: Disc) -> None:
        min_dist = 2 * self._cfg.DISC_RADIUS
        delta = b.pos - a.pos
        dist = delta.length()
        if dist >= min_dist:
            return
        # Centros exactamente iguales: no hay normal definida; se elige una
        # cualquiera para no dividir entre cero.
        normal = delta * (1.0 / dist) if dist > 0.0 else Vec2(1.0, 0.0)
        ma, mb = self.mass(a), self.mass(b)
        total = ma + mb

        # 1) Separar: cada uno retrocede en proporción a la masa del OTRO
        #    (el más pesado se mueve menos).
        overlap = min_dist - dist
        a.pos = a.pos - normal * (overlap * mb / total)
        b.pos = b.pos + normal * (overlap * ma / total)

        # 2) Velocidades: solo si se acercan a lo largo de la normal. Si ya se
        #    alejan (p. ej. siguen solapados tras el choque anterior), no se
        #    vuelve a aplicar el impulso.
        approach = (a.vel - b.vel).dot(normal)
        if approach <= 0.0:
            return
        # Choque elástico 1D sobre la normal: conserva el momento
        # (ma*va + mb*vb) y la energía cinética. Las componentes tangenciales
        # no cambian.
        a.vel = a.vel - normal * (2 * mb / total * approach)
        b.vel = b.vel + normal * (2 * ma / total * approach)
