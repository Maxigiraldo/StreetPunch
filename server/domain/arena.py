"""Arena circular centrada en el origen (0, 0).

Las coordenadas del mundo pueden ser negativas; el cliente traslada el origen
al centro del canvas al dibujar.
"""

import math

from server.domain.disc import Disc
from server.domain.vector import Vec2


class Arena:
    def __init__(self, radius: float) -> None:
        self.radius = radius

    def contains(self, disc: Disc) -> bool:
        """Un disco sigue en la arena mientras su CENTRO esté dentro: puede
        quedar medio colgando del borde y todavía salvarse."""
        return disc.pos.length() <= self.radius

    def spawn_points(self, n: int, ring_fraction: float) -> list[Vec2]:
        """n posiciones repartidas por igual en un círculo concéntrico."""
        ring = self.radius * ring_fraction
        return [
            Vec2(ring * math.cos(2 * math.pi * i / n), ring * math.sin(2 * math.pi * i / n))
            for i in range(n)
        ]
