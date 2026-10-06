"""The fixed-threshold baseline (r2 §5.1, ruling P5): the alert most teams run today.

One number per service and signal: "alert when latency p95 is at or above 180 ms". It is the
baseline the band has to beat, so it is tuned fairly, on the same data and with the same rules:

- **Fitted on the night.** The threshold comes from the clean soak of session A, and only from its
  low-load half (`night_half`): the steps where storefront's request rate is at or below its soak
  median. A threshold set while things are quiet is the classic case; a whole-soak p99 would
  already be the midday value.
- **p99 plus a margin.** `value = max(factor * p99, p99 + min_delta)`. The p99 is the value 99% of
  the night's gated values are at or below. The clean error ratio's p99 is usually 0, and
  `factor * 0` is still 0, which would page on one failed request; the absolute margin (20 ms for
  latency, 1 percentage point for errors) stops that straw man.
- **Same gate, same N, same gaps as the band.** Steps with fewer than `min_requests_1m` requests in
  the last minute are neither fitted nor judged; an alert needs `n_fire` out steps in a row and
  clears after `n_clear` in steps (the shared `FireClear`); `telemetry_missing` raises a
  `monitoring_gap` alert exactly as the band does. So a score difference comes from the detection
  idea alone, never from different debouncing.
- Thresholds exist only for `latency_p95` and `error_ratio`. `request_rate` feeds the gate and
  never yields an `Anomaly`.

Comparisons use `>=`, as the band does (ruling P10). Time comes only from the samples (D5).
"""

import statistics
from collections.abc import Iterable

import numpy as np

from always_200.core import Anomaly, Sample
from always_200.detect.firing import FireClear

THRESHOLD_SIGNALS = ("latency_p95", "error_ratio")
LOAD_SERVICE = "storefront"  # ruling P5: the load of a step is storefront's request rate


def night_half(soak: Iterable[Sample]) -> list[Sample]:
    """The low-load half of the soak (ruling P5): every sample of the steps where storefront's
    request_rate is at or below its soak median, in the order given.

    A soak with no storefront request rate at all has no load to split on, so all of it is kept.
    """
    samples = list(soak)
    load = {
        s.ts: s.value for s in samples if s.service == LOAD_SERVICE and s.signal == "request_rate"
    }
    if not load:
        return samples
    median = statistics.median(load.values())
    quiet = {ts for ts, rate in load.items() if rate <= median}
    return [s for s in samples if s.ts in quiet]


def fit_thresholds(
    soak: Iterable[Sample],
    factor: float,
    latency_min_delta_s: float = 0.020,
    error_min_delta: float = 0.01,
    min_requests_1m: float = 30.0,
) -> dict[tuple[str, str], float]:
    """Per (service, signal) for signal in ("latency_p95", "error_ratio") ONLY (never request_rate):
    p99 of the gated values in the LOW-LOAD half of the soak (ruling P5: steps where storefront's
    request_rate is at or below its soak median); value = max(factor*p99, p99 + min_delta).

    `soak` must be in stream order (ruling P7), so each step's request rate is read before the
    same step's latency and error ratio. A series with no gated value gets no threshold.
    """
    if factor <= 0:
        raise ValueError("the threshold factor must be positive")
    min_delta = {"latency_p95": latency_min_delta_s, "error_ratio": error_min_delta}
    requests_1m: dict[str, float] = {}
    values: dict[tuple[str, str], list[float]] = {}
    for s in night_half(soak):
        if s.signal == "request_rate":
            requests_1m[s.service] = s.value * 60.0
        elif s.signal in THRESHOLD_SIGNALS and requests_1m.get(s.service, 0.0) >= min_requests_1m:
            values.setdefault((s.service, s.signal), []).append(s.value)
    thresholds = {}
    for (service, signal), series in sorted(values.items()):
        p99 = float(np.percentile(series, 99))
        thresholds[(service, signal)] = max(factor * p99, p99 + min_delta[signal])
    return thresholds


class ThresholdDetector:
    def __init__(
        self,
        thresholds: dict[tuple[str, str], float],
        n_fire: int = 3,
        n_clear: int = 4,
        min_requests_1m: float = 30.0,
    ) -> None:
        wrong = sorted(sig for _, sig in thresholds if sig not in THRESHOLD_SIGNALS)
        if wrong:
            raise ValueError(f"no threshold may exist for {', '.join(wrong)} (ruling P5)")
        self._thresholds = dict(thresholds)
        self._n_fire = n_fire
        self._n_clear = n_clear
        self._min_requests_1m = min_requests_1m
        self._requests_1m: dict[str, float] = {}  # service -> requests in the last minute
        self._states: dict[tuple[str, str], FireClear] = {}  # (service, signal) -> fire/clear
        self._gaps: dict[str, FireClear] = {}  # service -> monitoring-gap fire/clear
        FireClear(n_fire, n_clear)  # refuses a zero N here, at construction

    @classmethod
    def from_config(
        cls, thresholds: dict[tuple[str, str], float], cfg: dict
    ) -> "ThresholdDetector":
        """The N's from `timing` and the request gate from `band`: the same as the band's."""
        return cls(
            thresholds,
            n_fire=int(cfg["timing"]["n_fire"]),
            n_clear=int(cfg["timing"]["n_clear"]),
            min_requests_1m=float(cfg["band"]["min_requests_1m"]),
        )

    def _fire_clear(self) -> FireClear:
        return FireClear(self._n_fire, self._n_clear)

    def update(self, s: Sample) -> Anomaly | None:
        if s.signal == "request_rate":
            self._requests_1m[s.service] = s.value * 60.0
            return None

        if s.signal == "telemetry_missing":
            gap = self._gaps.setdefault(s.service, self._fire_clear())
            emit, first = gap.step(s.value >= 1.0)
            if not emit:
                return None
            return Anomaly(s.ts, s.service, s.signal, "monitoring_gap", s.value, 0.0, 0.0, first)

        key = (s.service, s.signal)
        threshold = self._thresholds.get(key)
        if threshold is None:  # no gated value in the soak: nothing to compare with
            return None
        # The same request gate as the band: too few requests to judge.
        if self._requests_1m.get(s.service, 0.0) < self._min_requests_1m:
            return None
        out = s.value >= threshold
        emit, first = self._states.setdefault(key, self._fire_clear()).step(out)
        if not emit:
            return None
        return Anomaly(s.ts, s.service, s.signal, "fault", s.value, threshold, 0.0, first)
