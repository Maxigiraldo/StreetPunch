"""Vector 2D inmutable para posiciones, velocidades y direcciones.

Es inmutable (frozen) para que nadie modifique por accidente una posición que
otro objeto también guarda: cada operación devuelve un vector nuevo.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Vec2:
    x: float = 0.0
    y: float = 0.0

    def __add__(self, other: "Vec2") -> "Vec2":
        return Vec2(self.x + other.x, self.y + other.y)

    def __sub__(self, other: "Vec2") -> "Vec2":
        return Vec2(self.x - other.x, self.y - other.y)

    def __mul__(self, k: float) -> "Vec2":
        return Vec2(self.x * k, self.y * k)

    def __neg__(self) -> "Vec2":
        return Vec2(-self.x, -self.y)

    def dot(self, other: "Vec2") -> float:
        return self.x * other.x + self.y * other.y

    def length(self) -> float:
        return math.hypot(self.x, self.y)

    def is_zero(self) -> bool:
        return self.x == 0.0 and self.y == 0.0

    def normalized(self) -> "Vec2":
        """Mismo sentido, longitud 1. El vector cero se queda en cero (no
        hay dirección que conservar y así se evita dividir entre 0)."""
        n = self.length()
        if n == 0.0:
            return Vec2()
        return Vec2(self.x / n, self.y / n)

    def with_length(self, length: float) -> "Vec2":
        return self.normalized() * length
