"""Turns per-step query results into the time-ordered `Sample` stream.

The live source (Prometheus) and the recorder both run the same queries and get back, for each
signal and service, a value per step timestamp. This module is the one place that decides what
those numbers mean at the seam, so live and recorded data can never disagree about it.

`results` maps signal -> service -> step timestamp -> value. Besides the three detector signals
it holds `up`, Prometheus's own "the scrape worked" series (1 = reachable, 0 = not). A value is
looked up by the exact step timestamp; nothing is borrowed from a neighbouring step.

Per step and service (ruling P2), in ruling-P7 order:
- `up == 0`, or no `up` value at all (a missing scrape): only `telemetry_missing = 1.0`;
- `up == 1` but no `request_rate` (an idle counter gives an empty series): only
  `telemetry_missing = 0.0`; the signals are skipped, so no baseline learns from the step;
- otherwise `request_rate`, `latency_p95` and `error_ratio` when present and finite, then
  `telemetry_missing = 0.0`. A NaN (for example `histogram_quantile` at zero traffic) is skipped,
  never yielded. A real 0 (no errors) is a sample of 0, never dropped.
"""

import math
from collections.abc import Iterable, Iterator

from always_200.core import SERVICES, Sample

StepResults = dict[str, dict[str, dict[float, float]]]  # signal -> service -> ts -> value

_MEASURED = ("request_rate", "latency_p95", "error_ratio")  # ruling P7 order


def _value(results: StepResults, signal: str, service: str, ts: float) -> float | None:
    """The finite value at exactly this step, or None if absent or not a number."""
    value = results.get(signal, {}).get(service, {}).get(ts)
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def assemble(results: StepResults, steps: Iterable[float]) -> Iterator[Sample]:
    for ts in sorted(steps):
        for service in SERVICES:
            up = _value(results, "up", service, ts)
            if up is None or up < 0.5:
                yield Sample(ts, service, "telemetry_missing", 1.0)
                continue
            if _value(results, "request_rate", service, ts) is not None:
                for signal in _MEASURED:
                    value = _value(results, signal, service, ts)
                    if value is not None:
                        yield Sample(ts, service, signal, value)
            yield Sample(ts, service, "telemetry_missing", 0.0)
