"""Disco de un jugador dentro de la ronda.

El disco guarda dos tipos de datos:
- Estado físico (pos, vel, cooldown, alive): solo lo cambia Physics o World.
- Intenciones del jugador (move_dir, charging, release_pending): las dejan los
  comandos. Los comandos NO mueven el disco; Physics lee la intención en el
  siguiente paso. Así toda la física ocurre en un solo lugar.
"""

from dataclasses import dataclass, field

from server.domain.vector import Vec2


@dataclass
class Disc:
    id: int
    pos: Vec2
    vel: Vec2 = field(default_factory=Vec2)

    # Hacia dónde sale la embestida: la última dirección no nula que pidió el
    # jugador (si suelta las teclas, la embestida va hacia donde miraba).
    facing: Vec2 = field(default_factory=lambda: Vec2(1.0, 0.0))

    # Intenciones (las escriben los comandos).
    move_dir: Vec2 = field(default_factory=Vec2)
    charging: bool = False
    release_pending: bool = False

    # Estado físico (lo escribe Physics).
    charge_time: float = 0.0   # segundos cargados, hasta CHARGE_MAX_S
    cooldown: float = 0.0      # segundos que faltan para poder cargar
    dashing: bool = False      # en embestida: empuja con masa extra
    alive: bool = True

    def set_move(self, direction: Vec2) -> None:
        """Guarda la dirección deseada, siempre de longitud 1 o 0: un cliente
        que mande dx=1000 no se mueve más rápido."""
        self.move_dir = direction.normalized()
        if not self.move_dir.is_zero():
            self.facing = self.move_dir

    def start_charge(self) -> None:
        self.charging = True

    def release_charge(self) -> None:
        # Soltar sin estar cargando (p. ej. un "charge off" repetido) no hace nada.
        if self.charging:
            self.charging = False
            self.release_pending = True
