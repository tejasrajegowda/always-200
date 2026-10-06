"""Intervals for the results table (scoring spec sections 5, 6 and 9).

- `wilson`: a 95% interval for a proportion (detection rate, culprit %). Unlike the textbook
  p ± 1.96·√(p(1−p)/n), it never leaves [0, 1] and it does not collapse to zero width at 0 of n
  or n of n, which is exactly where a small bench lives.
- `poisson_rate_ci`: the exact (Garwood) 95% interval for a count of rare events over an exposure,
  used for false alarms per clean hour. A count of 0 still gets a real upper bound.
- `bootstrap_median_ci`: a seeded percentile bootstrap for the median detection latency, so the
  same latencies always give the same interval.

A denominator of 0 gives None, which the report prints as "n/a" (spec section 9).
"""

import math

import numpy as np
from scipy.stats import chi2

Z95 = 1.959963984540054  # the standard normal's 97.5% point: a two-sided 95% interval


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float, float] | None:
    """(share, low, high) for k successes out of n; None when n == 0."""
    if n == 0:
        return None
    if not 0 <= k <= n:
        raise ValueError(f"wilson needs 0 <= k <= n, got k={k}, n={n}")
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    lo = 0.0 if k == 0 else max(0.0, centre - half)
    hi = 1.0 if k == n else min(1.0, centre + half)
    return (p, lo, hi)


def poisson_rate_ci(count: int, exposure_h: float) -> tuple[float, float, float]:
    """(rate, low, high) per hour for `count` events in `exposure_h` hours, exact 95%."""
    if exposure_h <= 0:
        raise ValueError(f"exposure must be positive, got {exposure_h}")
    if count < 0:
        raise ValueError(f"count must not be negative, got {count}")
    lo = 0.0 if count == 0 else float(chi2.ppf(0.025, 2 * count)) / 2 / exposure_h
    hi = float(chi2.ppf(0.975, 2 * count + 2)) / 2 / exposure_h
    return (count / exposure_h, lo, hi)


def bootstrap_median_ci(
    values: list[float], n: int = 2000, seed: int = 0
) -> tuple[float, float, float] | None:
    """(median, low, high): resample `values` with replacement `n` times, take each median, and
    report the 2.5th and 97.5th percentiles of those medians. None for an empty list."""
    if not values:
        return None
    data = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, len(data), size=(n, len(data)))
    medians = np.median(data[picks], axis=1)
    lo, hi = np.percentile(medians, [2.5, 97.5])
    return (float(np.median(data)), float(lo), float(hi))
