"""Arena circular centrada en el origen (0, 0).

Las coordenadas del mundo pueden ser negativas; el cliente traslada el origen
al centro del canvas al dibujar.
"""

import math

from server.domain.disc import Disc
from server.domain.vector import Vec2


class Arena:
    def __init__(self, radius: float, fall_margin: float = 0.0) -> None:
        self.radius = radius                      # el borde que se dibuja
        self.fall_margin = fall_margin            # cuánto más allá del borde aún no se cae
        self.elimination_radius = radius + fall_margin

    def contains(self, disc: Disc) -> bool:
        """Un disco sigue en juego mientras su CENTRO esté dentro del radio de
        eliminación (borde + margen): puede quedar colgando del borde, incluso
        con el disco casi entero fuera, y todavía salvarse."""
        return disc.pos.length() <= self.elimination_radius

    def spawn_points(self, n: int, ring_fraction: float) -> list[Vec2]:
        """n posiciones repartidas por igual en un círculo concéntrico."""
        ring = self.radius * ring_fraction
        return [
            Vec2(ring * math.cos(2 * math.pi * i / n), ring * math.sin(2 * math.pi * i / n))
            for i in range(n)
        ]
