"""One injected fault: what it is, how big, and what it does to a request at a given moment."""

import random
from dataclasses import dataclass

SIZES = {"subtle": (0.30, 0.05), "moderate": (1.00, 0.10), "severe": (4.00, 0.40)}
# size -> (latency share, error share)
TYPES = ("latency_step", "latency_ramp", "errors")


@dataclass(frozen=True)
class Fault:
    type: str
    size: str
    started: float  # monotonic seconds
    duration_s: float
    base_p95_s: float  # the service's own recent p95 when the fault began (ruling P6)

    def __post_init__(self) -> None:
        if self.type not in TYPES:
            raise ValueError(f"unknown fault type {self.type!r}")
        if self.size not in SIZES:
            raise ValueError(f"unknown fault size {self.size!r}")

    def active(self, now: float) -> bool:
        return self.started <= now < self.started + self.duration_s

    def extra_delay(self, now: float) -> float:
        if self.type == "errors" or not self.active(now):
            return 0.0
        full = SIZES[self.size][0] * self.base_p95_s
        if self.type == "latency_ramp":
            return full * min(1.0, (now - self.started) / (self.duration_s / 2))
        return full

    def fails(self, now: float, rng: random.Random) -> bool:
        return self.type == "errors" and self.active(now) and rng.random() < SIZES[self.size][1]
