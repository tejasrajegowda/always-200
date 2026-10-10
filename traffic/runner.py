"""Play a session plan against the shop: open-loop load, faults, events, deploy markers.

Open loop means the next arrival is scheduled from the session clock, never from when the
previous response came back. A slow shop therefore cannot slow the sender, and the same seed
offers the same number of requests every time.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

import httpx

from traffic.load import Load
from traffic.metrics import (
    deploy_marker_total,
    event_active,
    session_phase,
    session_seed,
    traffic_offered_total,
    traffic_sent_total,
)
from traffic.schedule import EVENT_KINDS, SERVICES, SessionPlan, Slot

# At 25 req/s a 2 s fault holds about 50 requests. 200 is a safety valve, not the normal path:
# past it, an arrival is counted as dropped instead of waiting, so the sender never blocks.
MAX_IN_FLIGHT = 200
BUY_TIMEOUT_S = 10.0

Action = tuple[float, str, dict[str, object]]


@dataclass(frozen=True)
class Targets:
    """Base URL of each service, for example ``http://payments:8000``."""

    base: dict[str, str]


def _deploy_only_service(plan: SessionPlan) -> dict[int, str]:
    """Which service a deploy-only event marks.

    Its own generator, so it never shifts the schedule or the arrival stream.
    """
    rng = random.Random(f"always-200-deploy-only:{plan.seed}")
    return {
        slot.index: rng.choice(SERVICES)
        for slot in plan.slots
        if slot.kind == "event" and slot.event_kind == "deploy_only"
    }


def _spike_at(plan: SessionPlan, t_s: float) -> bool:
    """True while a traffic-spike event covers t. The end instant is already over."""
    return any(
        slot.kind == "event"
        and slot.event_kind == "traffic_spike"
        and slot.start_s <= t_s < slot.start_s + slot.duration_s
        for slot in plan.slots
    )


def actions(plan: SessionPlan) -> list[Action]:
    """The timed actions of a plan, sorted by offset from the session start.

    Phase changes sit at 0 (warm-up), the end of warm-up (soak), the end of the soak (slots)
    and the session total (done). Equal times keep this function's order: the phase is
    announced, then the slot's own action.
    """
    marked = _deploy_only_service(plan)
    acts: list[Action] = [
        (0.0, "phase", {"phase": 0}),
        (plan.warmup_s, "phase", {"phase": 1}),
        (plan.warmup_s + plan.soak_s, "phase", {"phase": 2}),
        (plan.total_s, "phase", {"phase": 3}),
    ]
    for slot in plan.slots:
        acts.extend(_slot_actions(slot, marked))
    acts.sort(key=lambda item: item[0])
    return acts


def _slot_actions(slot: Slot, marked: dict[int, str]) -> list[Action]:
    if slot.kind == "fault":
        found: list[Action] = []
        if slot.deploy_marker_s is not None:
            found.append((slot.deploy_marker_s, "deploy_marker", {"service": slot.target}))
        found.append(
            (
                slot.start_s,
                "fault",
                {
                    "service": slot.target,
                    "type": slot.fault_type,
                    "size": slot.size,
                    "duration_s": slot.duration_s,
                },
            )
        )
        return found
    start: list[Action] = [(slot.start_s, "event_start", {"kind": slot.event_kind})]
    if slot.event_kind == "rolling_restart":
        # Fixed order, 20 s apart. The event is at least 120 s, so all three land before it ends.
        start.extend(
            (slot.start_s + 20.0 * i, "restart", {"service": service})
            for i, service in enumerate(SERVICES)
        )
    elif slot.event_kind == "deploy_only":
        start.append((slot.start_s, "deploy_marker", {"service": marked[slot.index]}))
    start.append((slot.start_s + slot.duration_s, "event_end", {"kind": slot.event_kind}))
    return start


def _url(targets: Targets, service: object, path: str) -> str:
    if not isinstance(service, str):
        raise TypeError(f"service must be a string, got {service!r}")
    return f"{targets.base[service].rstrip('/')}{path}"


async def _perform(
    kind: str, payload: dict[str, object], targets: Targets, client: httpx.AsyncClient
) -> None:
    if kind == "phase":
        session_phase.set(payload["phase"])
    elif kind == "event_start":
        event_active.labels(kind=payload["kind"]).set(1)
    elif kind == "event_end":
        event_active.labels(kind=payload["kind"]).set(0)
    elif kind == "deploy_marker":
        deploy_marker_total.labels(service=payload["service"]).inc()
    elif kind == "fault":
        response = await client.post(
            _url(targets, payload["service"], "/admin/fault"),
            json={
                "type": payload["type"],
                "size": payload["size"],
                "duration_s": payload["duration_s"],
            },
            timeout=BUY_TIMEOUT_S,
        )
        response.raise_for_status()
    elif kind == "restart":
        # The process exits just after answering, so a reset connection is a normal restart.
        with suppress(httpx.HTTPError):
            response = await client.post(
                _url(targets, payload["service"], "/admin/restart"), timeout=BUY_TIMEOUT_S
            )
            response.raise_for_status()
    else:
        raise ValueError(f"unknown action {kind!r}")


async def _send_buy(
    client: httpx.AsyncClient,
    url: str,
    customer_id: int,
    amount_cents: int,
    gate: asyncio.Semaphore,
) -> None:
    outcome = "error"
    try:
        response = await client.post(
            url,
            json={"customer_id": customer_id, "amount_cents": amount_cents},
            timeout=BUY_TIMEOUT_S,
        )
        outcome = "ok" if response.status_code < 400 else "error"
    except httpx.TimeoutException:
        outcome = "timeout"
    except httpx.HTTPError:
        outcome = "error"
    finally:
        traffic_sent_total.labels(outcome=outcome).inc()
        gate.release()


async def _arrivals(
    plan: SessionPlan,
    targets: Targets,
    client: httpx.AsyncClient,
    sleep_until: Callable[[float], Awaitable[None]],
) -> None:
    """Offer one Poisson stream. This function never awaits an HTTP response."""
    rng = random.Random(f"{plan.seed}:arrivals")
    buys = random.Random(f"{plan.seed}:buys")
    load = Load(plan.seed)
    gate = asyncio.Semaphore(MAX_IN_FLIGHT)
    url = _url(targets, "storefront", "/buy")
    pending: set[asyncio.Task[None]] = set()
    t_next = 0.0
    while t_next < plan.total_s:
        gap = rng.expovariate(load.rate(t_next, _spike_at(plan, t_next)))
        t_next += gap
        if t_next >= plan.total_s:
            break
        await sleep_until(t_next)
        traffic_offered_total.inc()
        if gate.locked():
            traffic_sent_total.labels(outcome="dropped").inc()
            continue
        await gate.acquire()
        task = asyncio.create_task(
            _send_buy(client, url, buys.randint(1, 500), buys.randint(100, 20000), gate)
        )
        pending.add(task)
        task.add_done_callback(pending.discard)
    if pending:
        await asyncio.gather(*pending)


async def _play(
    plan: SessionPlan,
    targets: Targets,
    client: httpx.AsyncClient,
    sleep_until: Callable[[float], Awaitable[None]],
) -> None:
    for at, kind, payload in actions(plan):
        await sleep_until(at)
        await _perform(kind, payload, targets, client)


async def run_session(
    plan: SessionPlan,
    targets: Targets,
    client: httpx.AsyncClient,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run one session from t = 0 to the plan's total. The caller chains seeds back to back."""
    origin = clock()

    async def sleep_until(t_s: float) -> None:
        delay = t_s - (clock() - origin)
        await sleep(delay if delay > 0 else 0)

    # Phase goes to warm-up before the seed gauge flips, so a scrape cannot see this seed
    # still wearing the previous session's "done".
    for kind in EVENT_KINDS:
        event_active.labels(kind=kind).set(0)
    session_phase.set(0)
    session_seed.labels(seed=plan.seed).set(1)
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(_arrivals(plan, targets, client, sleep_until))
            group.create_task(_play(plan, targets, client, sleep_until))
    finally:
        session_seed.labels(seed=plan.seed).set(0)
