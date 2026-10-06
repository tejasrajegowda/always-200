"""How many requests per second to send at a given moment of a session.

A compressed "day" of one hour: quiet at 'midnight' (t = 0), busiest at 'midday' (t = 30 min),
with a little seeded noise on top so no two minutes look exactly alike.
"""

import math
import random

PEAK_RPS = 25.0
DAY_S = 3600.0
NOISE_KNOT_S = 60.0
NOISE_SHARE = 0.05
SPIKE_FACTOR = 2.5


def day_curve(t_s: float) -> float:
    """Share of peak load: 0.30 at 'midnight' (t=0), 1.00 at 'midday' (t=30 min)."""
    return 0.65 - 0.35 * math.cos(2 * math.pi * t_s / DAY_S)


class Load:
    """The seeded load curve of one session."""

    def __init__(self, seed: str) -> None:
        self._rng = random.Random(f"always-200-load:{seed}")
        self._knots: list[float] = []  # noise at t = 0, 60, 120, ... seconds

    def _knot(self, i: int) -> float:
        # Knots are drawn in order and kept, so knot i is the same whatever t was asked first.
        while len(self._knots) <= i:
            self._knots.append(self._rng.uniform(-NOISE_SHARE, NOISE_SHARE))
        return self._knots[i]

    def noise(self, t_s: float) -> float:
        """Relative noise at t: straight lines between the knots, so always within +-5%."""
        if t_s < 0:
            raise ValueError(f"t_s must be >= 0, got {t_s}")
        i = int(t_s // NOISE_KNOT_S)
        frac = (t_s - i * NOISE_KNOT_S) / NOISE_KNOT_S
        return self._knot(i) + (self._knot(i + 1) - self._knot(i)) * frac

    def rate(self, t_s: float, spike: bool = False) -> float:
        """Requests per second at t; a traffic-spike event multiplies it by 2.5."""
        base = PEAK_RPS * day_curve(t_s) * (1 + self.noise(t_s))
        return base * (SPIKE_FACTOR if spike else 1.0)
