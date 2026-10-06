"""Turn a seed into a session plan: warm-up, clean soak, then 48 slots of faults and events.

Pure and deterministic: the same seed always gives the same plan, so a session can be recorded
again and its answer key rebuilt from the seed alone.
"""

import random
from dataclasses import dataclass

SERVICES = ("storefront", "orders", "payments")
FAULT_TYPES = ("latency_step", "latency_ramp", "errors")
SIZES = ("subtle", "moderate", "severe")
EVENT_KINDS = ("traffic_spike", "rolling_restart", "deploy_only")

WARMUP_S = 600.0
SOAK_S = 3600.0
QUICK_SOAK_S = 300.0
COOLDOWN_S = 360.0
N_FAULTS = 36
N_EVENTS = 12
FAULT_DURATION_S = (120.0, 300.0)
EVENT_DURATION_S = (120.0, 240.0)
DEPLOY_LEAD_S = (30.0, 120.0)  # a deploy marker lands this long before its fault starts
DEPLOY_SHARE = 0.5


@dataclass(frozen=True)
class Slot:
    index: int
    kind: str  # "fault" | "event"
    start_s: float  # offset from session start
    duration_s: float
    target: str | None = None
    fault_type: str | None = None
    size: str | None = None
    event_kind: str | None = None
    deploy_marker_s: float | None = None  # a deploy marker before about half of the faults


@dataclass(frozen=True)
class SessionPlan:
    seed: str
    warmup_s: float
    soak_s: float
    cooldown_s: float
    slots: tuple[Slot, ...]

    @property
    def total_s(self) -> float:
        last = self.slots[-1]
        return last.start_s + last.duration_s + self.cooldown_s


def _balanced(rng: random.Random, values: tuple[str, ...], total: int) -> list[str]:
    """Exactly total / len(values) of each value, in a seeded random order."""
    items = [v for v in values for _ in range(total // len(values))]
    rng.shuffle(items)
    return items


def build_session(seed: str, quick: bool = False, soak_s: float | None = None) -> SessionPlan:
    """The full plan for one session; `quick` is the short demo / smoke plan."""
    if soak_s is not None and soak_s < 0:
        raise ValueError(f"soak_s must be >= 0, got {soak_s}")
    rng = random.Random(f"always-200:{seed}")
    if quick:
        soak = QUICK_SOAK_S if soak_s is None else soak_s
        targets = list(SERVICES)
        rng.shuffle(targets)
        specs = [
            {"kind": "fault", "target": t, "fault_type": ft, "size": "severe"}
            for t, ft in zip(targets, FAULT_TYPES, strict=True)
        ]
    else:
        soak = SOAK_S if soak_s is None else soak_s
        sizes = _balanced(rng, SIZES, N_FAULTS)
        types = _balanced(rng, FAULT_TYPES, N_FAULTS)
        event_kinds = _balanced(rng, EVENT_KINDS, N_EVENTS)
        kinds = ["fault"] * N_FAULTS + ["event"] * N_EVENTS
        rng.shuffle(kinds)
        specs = []
        for kind in kinds:
            if kind == "fault":
                specs.append({"kind": "fault", "fault_type": types.pop(), "size": sizes.pop()})
            else:
                specs.append({"kind": "event", "event_kind": event_kinds.pop()})

    slots = []
    t = WARMUP_S + soak
    for index, spec in enumerate(specs):
        if spec["kind"] == "fault":
            duration = rng.uniform(*FAULT_DURATION_S)
            target = spec.get("target") or rng.choice(SERVICES)
            marker = t - rng.uniform(*DEPLOY_LEAD_S) if rng.random() < DEPLOY_SHARE else None
            slot = Slot(
                index=index,
                kind="fault",
                start_s=t,
                duration_s=duration,
                target=target,
                fault_type=spec["fault_type"],
                size=spec["size"],
                deploy_marker_s=marker,
            )
        else:
            duration = rng.uniform(*EVENT_DURATION_S)
            slot = Slot(
                index=index,
                kind="event",
                start_s=t,
                duration_s=duration,
                event_kind=spec["event_kind"],
            )
        slots.append(slot)
        t += duration + COOLDOWN_S
    return SessionPlan(
        seed=seed, warmup_s=WARMUP_S, soak_s=soak, cooldown_s=COOLDOWN_S, slots=tuple(slots)
    )
