"""The traffic machine, with a fake clock and a mock HTTP transport. No network and no Docker."""

import asyncio
import json
from dataclasses import dataclass, field

import httpx
import pytest

from traffic.metrics import (
    deploy_marker_total,
    event_active,
    session_phase,
    session_seed,
    traffic_offered_total,
    traffic_sent_total,
)
from traffic.runner import Targets, actions, run_session
from traffic.schedule import SERVICES, SessionPlan, Slot, build_session

TARGETS = Targets(
    base={
        "storefront": "http://storefront:8000",
        "orders": "http://orders:8000",
        "payments": "http://payments:8000",
    }
)


def test_actions_are_time_sorted_and_cover_every_slot():
    acts = actions(build_session("A1"))
    assert [a[0] for a in acts] == sorted(a[0] for a in acts)
    assert sum(1 for a in acts if a[1] == "fault") == 36


def test_rolling_restart_hits_each_service_20s_apart():
    acts = actions(build_session("A1"))
    restarts = [a for a in acts if a[1] == "restart"]
    assert len(restarts) == 4 * 3
    first = restarts[:3]
    assert [r[2]["service"] for r in first] == ["storefront", "orders", "payments"]
    assert first[1][0] - first[0][0] == 20 and first[2][0] - first[1][0] == 20


def test_phases_are_announced_in_order():
    phases = [a[2]["phase"] for a in actions(build_session("A1")) if a[1] == "phase"]
    assert phases == [0, 1, 2, 3]


def _counter(metric, **labels: str) -> float:
    for family in metric.collect():
        for sample in family.samples:
            if sample.name.endswith("_total") and sample.labels == labels:
                return sample.value
    return 0.0


def _gauge(metric, **labels: str) -> float:
    for family in metric.collect():
        for sample in family.samples:
            if sample.name == family.name and sample.labels == labels:
                return sample.value
    return 0.0


@dataclass
class VirtualClock:
    """Time moves only when every task is parked and `advance` jumps to the next wake-up."""

    now: float = 0.0
    waiters: list[tuple[float, asyncio.Future[None]]] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + delay, future))
        self.waiters.sort(key=lambda item: item[0])
        try:
            await future
        finally:
            self.waiters = [(at, fut) for at, fut in self.waiters if fut is not future]

    def advance(self) -> None:
        self.now = self.waiters[0][0]
        ready = [fut for at, fut in self.waiters if at <= self.now]
        self.waiters = [(at, fut) for at, fut in self.waiters if at > self.now]
        for fut in ready:
            if not fut.done():
                fut.set_result(None)


async def _pump(clock: VirtualClock, task: asyncio.Task[None]) -> None:
    """Run `task` on `clock`. A turn with an empty ready queue means everyone is parked."""
    loop = asyncio.get_running_loop()
    for _ in range(2_000_000):
        if task.done():
            break
        await asyncio.sleep(0)
        if task.done():
            break
        # Callbacks queued behind us must run before time jumps, or arrivals pile up.
        if loop._ready:  # the event loop's runnable queue; there is no public handle for it
            continue
        if not clock.waiters:
            raise AssertionError("session stalled before it finished")
        clock.advance()
    else:
        raise AssertionError("virtual clock did not finish")
    if task.exception() is not None:
        raise task.exception()


async def _play(plan: SessionPlan, handler, clock: VirtualClock) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        await handler(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        task = asyncio.create_task(
            run_session(plan, TARGETS, client, clock=clock, sleep=clock.sleep)
        )
        await _pump(clock, task)
    return seen


def _plan_ending_at(seed: str, total_s: float, **slot) -> SessionPlan:
    """A session whose total is `total_s`. One slot keeps `total_s` defined."""
    duration = float(slot.pop("duration_s", 0.1))
    start = total_s - duration
    return SessionPlan(
        seed=seed,
        warmup_s=0.0,
        soak_s=0.0,
        cooldown_s=0.0,
        slots=(Slot(index=0, start_s=start, duration_s=duration, **slot),),
    )


def test_quick_plan_posts_three_faults_in_order():
    plan = build_session("dev", quick=True, soak_s=0)

    async def handler(_request: httpx.Request) -> None:
        return None

    seen = asyncio.run(_play(plan, handler, VirtualClock()))
    faults = [request for request in seen if request.url.path == "/admin/fault"]
    assert [request.url.host for request in faults] == [slot.target for slot in plan.slots]
    for request, slot in zip(faults, plan.slots, strict=True):
        body = json.loads(request.content)
        assert body["type"] == slot.fault_type
        assert body["size"] == slot.size
        assert body["duration_s"] == pytest.approx(slot.duration_s)


def test_open_loop_offers_the_same_load_when_the_shop_is_slow():
    plan = _plan_ending_at(
        "loop", 4.0, kind="fault", target="storefront", fault_type="errors", size="severe"
    )

    async def play(delay: float) -> tuple[float, list[dict]]:
        clock = VirtualClock()
        offered_before = _counter(traffic_offered_total)

        async def handler(request: httpx.Request) -> None:
            if request.url.path == "/buy" and delay:
                await clock.sleep(delay)

        seen = await _play(plan, handler, clock)
        offered = _counter(traffic_offered_total) - offered_before
        buys = [json.loads(request.content) for request in seen if request.url.path == "/buy"]
        for body in buys:
            assert 1 <= body["customer_id"] <= 500
            assert 100 <= body["amount_cents"] <= 20000
        return offered, buys

    fast_offered, fast_buys = asyncio.run(play(0.0))
    slow_offered, slow_buys = asyncio.run(play(2.0))
    assert fast_offered > 10  # the span really did offer a stream, not an empty plan
    assert (slow_offered, slow_buys) == (fast_offered, fast_buys)
    assert len(fast_buys) == fast_offered  # nothing was dropped: the safety valve stayed shut


def test_event_active_and_deploy_marker_follow_the_action():
    plan = _plan_ending_at("marker", 3.0, kind="event", event_kind="deploy_only", duration_s=2.0)
    # start_s = 3 - 2 = 1, so the event covers [1, 3).
    marks = [act for act in actions(plan) if act[1] == "deploy_marker"]
    assert len(marks) == 1
    service = str(marks[0][2]["service"])
    assert service in SERVICES
    before = {name: _counter(deploy_marker_total, service=name) for name in SERVICES}
    child = event_active.labels(kind="deploy_only")
    original = child.set
    clock = VirtualClock()
    history: list[tuple[float, float]] = []

    def spy(value: float, _set=original) -> None:
        history.append((clock.now, float(value)))
        _set(value)

    child.set = spy

    async def handler(_request: httpx.Request) -> None:
        return None

    try:
        asyncio.run(_play(plan, handler, clock))
    finally:
        child.set = original

    # run_session clears the gauge, the event raises it, the event's end lowers it.
    assert [value for _, value in history] == [0, 1, 0]
    assert history[1][0] == pytest.approx(1.0)
    assert history[2][0] == pytest.approx(3.0)
    assert _gauge(event_active, kind="deploy_only") == 0
    assert _gauge(session_phase) == 3  # the session announced "done"
    assert _gauge(session_seed, seed="marker") == 0  # the seed gauge drops when the session ends
    assert _counter(deploy_marker_total, service=service) - before[service] == 1
    for other in SERVICES:
        if other != service:
            assert _counter(deploy_marker_total, service=other) == before[other]


def test_full_gate_drops_instead_of_waiting(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("traffic.runner.MAX_IN_FLIGHT", 1)
    plan = _plan_ending_at(
        "drop", 3.0, kind="fault", target="orders", fault_type="latency_step", size="severe"
    )
    offered_before = _counter(traffic_offered_total)
    dropped_before = _counter(traffic_sent_total, outcome="dropped")
    clock = VirtualClock()

    async def handler(request: httpx.Request) -> None:
        if request.url.path == "/buy":
            await clock.sleep(2.0)

    seen = asyncio.run(_play(plan, handler, clock))
    offered = _counter(traffic_offered_total) - offered_before
    dropped = _counter(traffic_sent_total, outcome="dropped") - dropped_before
    buys = sum(1 for request in seen if request.url.path == "/buy")
    assert offered > buys > 0
    assert dropped == offered - buys


def test_rolling_restart_posts_each_service_in_order():
    plan = _plan_ending_at(
        "roll", 50.0, kind="event", event_kind="rolling_restart", duration_s=50.0
    )

    async def handler(_request: httpx.Request) -> None:
        return None

    seen = asyncio.run(_play(plan, handler, VirtualClock()))
    hosts = [request.url.host for request in seen if request.url.path == "/admin/restart"]
    assert hosts == ["storefront", "orders", "payments"]
