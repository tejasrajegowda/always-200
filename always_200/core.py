"""The seam: the types every source, detector and scorer agree on.

A source turns telemetry (live Prometheus, a recording file, later a stream) into one
time-ordered iterator of `Sample`s. A detector reads those samples one at a time and returns an
`Anomaly` or None. Nothing else crosses between them, so any source works with any detector.

Two rules are enforced here, at construction, so no later code can break them:
- only the four detector signals may become a `Sample`; the answer-key series (`fault_active`,
  `event_active`, `deploy_marker_total`, `session_phase`, `session_seed`) never can (D11);
- a value must be a finite number; NaN and infinity never cross the seam (Review Focus 2).

Detectors read `Sample.ts`, never the wall clock (D5), so a replay gives the same answer as live.
"""

import math
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

SERVICES = ("storefront", "orders", "payments")
SIGNALS = ("request_rate", "latency_p95", "error_ratio", "telemetry_missing")  # ruling P7 order
ANOMALY_KINDS = ("fault", "monitoring_gap")


@dataclass(frozen=True)
class Sample:
    ts: float
    service: str
    signal: str
    value: float

    def __post_init__(self) -> None:
        if self.signal not in SIGNALS:
            raise ValueError(f"signal {self.signal!r} is not allowed past the seam")
        if self.service not in SERVICES:
            raise ValueError(f"unknown service {self.service!r}")
        if not math.isfinite(self.value):
            raise ValueError("non-finite sample value")


def sample_order(s: Sample) -> tuple[float, int, int]:
    """The sort key of the stream: time, then service, then signal (ruling P7)."""
    return (s.ts, SERVICES.index(s.service), SIGNALS.index(s.signal))


@dataclass(frozen=True)
class Anomaly:
    ts: float
    service: str
    signal: str
    kind: str  # "fault" | "monitoring_gap"
    value: float
    centre: float  # band median, or the threshold reference
    spread: float
    first: bool  # True only on the step the alert fires (ruling P1)

    def __post_init__(self) -> None:
        if self.kind not in ANOMALY_KINDS:
            raise ValueError(f"anomaly kind {self.kind!r} is not one of {ANOMALY_KINDS}")


class Source(Protocol):
    def samples(self) -> Iterator[Sample]: ...


class Detector(Protocol):
    def update(self, sample: Sample) -> Anomaly | None: ...
