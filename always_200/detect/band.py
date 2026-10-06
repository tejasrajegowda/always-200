"""always-200's normal band detector (r2 §8).

For each service and signal (`latency_p95`, `error_ratio`) it keeps the last `window` normal
values and asks of each new value: is it unusually high compared with them?

- **Centre and spread are robust.** The centre is the median and the spread is the MAD (median
  absolute deviation) times 1.4826, which makes it comparable to a standard deviation for
  bell-shaped data. One wild value cannot drag either of them, unlike a mean and a standard
  deviation.
- **Spread floors.** A perfectly steady series has a MAD of 0, and then any tiny wobble would be
  "infinitely unusual". The spread is never below a floor: `max(frac * median, abs)` for latency,
  `error_floor` for the error ratio.
- **Minimum absolute delta.** On a very quiet series even a band edge can be a few milliseconds;
  a value must also be at least `min_delta` above the median to count (20 ms, 1 percentage point).
- **Request gate.** A service with fewer than `min_requests_1m` requests in the last minute has a
  p95 and an error ratio made of a handful of requests; those steps are not judged and not learned.
- **N in a row.** `FireClear` fires after 3 out-of-band steps and clears after 4 in-band ones.
- **Frozen baseline.** A value is learned only if it is in band and the series is not alerting,
  countdown included, so a fault can never teach the band that the fault is normal.
- **Warm-up.** Until the window is full the detector only learns and never alerts.
- **Monitoring gaps are alerts.** `telemetry_missing` goes through its own `FireClear` and comes
  out as `kind="monitoring_gap"`, a separate incident stream (ruling P2, D16).
- `request_rate` is never judged; it only feeds the gate.

Comparisons use `>=` (ruling P10): a value exactly on the band edge is out. Time comes only from
the samples, never the wall clock (D5).
"""

import statistics
from collections import deque
from dataclasses import dataclass, fields

from always_200.core import Anomaly, Sample
from always_200.detect.firing import FireClear

MAD_TO_SIGMA = 1.4826  # MAD x 1.4826 estimates the standard deviation of normal data
BAND_SIGNALS = ("latency_p95", "error_ratio")


@dataclass(frozen=True)
class BandConfig:
    k: float = 4.0
    window: int = 20
    n_fire: int = 3
    n_clear: int = 4
    min_requests_1m: float = 30.0
    latency_floor_frac: float = 0.10
    latency_floor_abs_s: float = 0.005
    latency_min_delta_s: float = 0.020
    error_floor: float = 0.005
    error_min_delta: float = 0.01

    @classmethod
    def from_config(cls, cfg: dict) -> "BandConfig":
        """Builds the band from the loaded config: the `band` section plus the timing N's.

        An unknown key in the `band` section is refused, so a typo in a frozen config can never
        silently fall back to a default.
        """
        band = dict(cfg["band"])
        names = {f.name for f in fields(cls)}
        unknown = sorted(set(band) - names)
        if unknown:
            raise ValueError(f"unknown band setting(s): {', '.join(unknown)}")
        timing = cfg.get("timing", {})
        for name in ("n_fire", "n_clear"):
            if name in timing and name not in band:
                band[name] = timing[name]
        return cls(
            **{
                name: (int(value) if name in ("window", "n_fire", "n_clear") else float(value))
                for name, value in band.items()
            }
        )


class BandDetector:
    def __init__(self, cfg: BandConfig) -> None:
        if cfg.window < 1:
            raise ValueError("the band window must hold at least one value")
        self._cfg = cfg
        self._requests_1m: dict[str, float] = {}  # service -> requests in the last minute
        self._windows: dict[tuple[str, str], deque[float]] = {}  # learned normal values
        self._states: dict[tuple[str, str], FireClear] = {}  # (service, signal) -> fire/clear
        self._gaps: dict[str, FireClear] = {}  # service -> monitoring-gap fire/clear

    def window(self, service: str, signal: str) -> tuple[float, ...]:
        """The learned values of one series, oldest first (for tests and lessons)."""
        return tuple(self._windows.get((service, signal), ()))

    def _fire_clear(self) -> FireClear:
        return FireClear(self._cfg.n_fire, self._cfg.n_clear)

    def _band(self, signal: str, values: deque[float]) -> tuple[float, float, float]:
        """(centre, spread, min_delta) of a full window."""
        cfg = self._cfg
        m = statistics.median(values)
        mad = statistics.median(abs(x - m) for x in values)
        if signal == "latency_p95":
            floor = max(cfg.latency_floor_frac * m, cfg.latency_floor_abs_s)
            min_delta = cfg.latency_min_delta_s
        else:
            floor = cfg.error_floor
            min_delta = cfg.error_min_delta
        return m, max(MAD_TO_SIGMA * mad, floor), min_delta

    def update(self, s: Sample) -> Anomaly | None:
        cfg = self._cfg
        if s.signal == "request_rate":
            self._requests_1m[s.service] = s.value * 60.0
            return None

        if s.signal == "telemetry_missing":
            gap = self._gaps.setdefault(s.service, self._fire_clear())
            emit, first = gap.step(s.value >= 1.0)
            if not emit:
                return None
            return Anomaly(s.ts, s.service, s.signal, "monitoring_gap", s.value, 0.0, 0.0, first)

        if s.signal not in BAND_SIGNALS:  # unreachable while core.SIGNALS has four entries
            return None
        # 1. The request gate: too few requests to judge; nothing is evaluated or learned.
        if self._requests_1m.get(s.service, 0.0) < cfg.min_requests_1m:
            return None
        key = (s.service, s.signal)
        window = self._windows.setdefault(key, deque(maxlen=cfg.window))
        # 2. Warm-up: learn silently until the window is full.
        if len(window) < cfg.window:
            window.append(s.value)
            return None
        # 3-4. Robust centre and floored spread; out only if far enough above both ways.
        m, spread, min_delta = self._band(s.signal, window)
        out = s.value >= m + cfg.k * spread and s.value - m >= min_delta
        # 5. N in a row to fire, n_clear in a row to clear.
        state = self._states.setdefault(key, self._fire_clear())
        emit, first = state.step(out)
        # 6. Learn only normal values, and nothing at all while alerting (frozen baseline).
        if not out and not state.alerting:
            window.append(s.value)
        # 7. Emit only on out-of-band steps while alerting; countdown steps return None (P1).
        if not emit:
            return None
        return Anomaly(s.ts, s.service, s.signal, "fault", s.value, m, spread, first)
