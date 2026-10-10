"""The live source against canned Prometheus answers. No server, and no real waiting."""

from itertools import islice

import httpx
import pytest

from always_200.core import SIGNALS, Sample
from always_200.detect.band import BandConfig, BandDetector
from always_200.selfmetrics import SelfMetrics
from always_200.sources.prom_api import PromAPI
from always_200.sources.prometheus import PrometheusSource
from always_200.sources.queries import QUERIES

SERVICES = ("storefront", "orders", "payments")
WALL = 10_000.7  # a wall clock. The step it is allowed to ask for is 9_990.0.
STEP = 9_990.0


def _series(service: str, ts: float, value: float, name: str | None = None) -> dict:
    metric: dict[str, str] = {"service": service}
    if name is not None:
        metric["__name__"] = name
    return {"metric": metric, "values": [[ts, str(value)]]}


def _matrix(series: list[dict]) -> dict:
    return {"status": "success", "data": {"resultType": "matrix", "result": series}}


class Clock:
    """A fake wall clock. `sleep` moves it, unless a subclass swallows the first wake."""

    def __init__(self, t: float) -> None:
        self.t = t
        self.sleeps = 0

    def now(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.sleeps += 1
        self.t += dt


class EarlyWake(Clock):
    """Odd sleeps return without moving time, so the source is polled twice per wait."""

    def sleep(self, dt: float) -> None:
        self.sleeps += 1
        if self.sleeps % 2 == 1:
            return
        self.t += dt


def _api(
    skip_up: set[str] | None = None,
    skip_rate: set[str] | None = None,
    plant: bool = False,
    seen: list[tuple[str, float]] | None = None,
) -> PromAPI:
    """Answers the four committed queries. `plant` adds fault_active=12345 beside latency."""
    missing_up = skip_up or set()
    idle = skip_rate or set()

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        start = float(request.url.params["start"])
        assert float(request.url.params["end"]) == start  # one step, not a range
        if seen is not None:
            seen.append((query, start))
        series: list[dict] = []
        if query == QUERIES["up"]:
            series = [_series(s, start, 1.0) for s in SERVICES if s not in missing_up]
        elif query == QUERIES["request_rate"]:
            series = [_series(s, start, 2.0) for s in SERVICES if s not in idle]
        elif query == QUERIES["latency_p95"]:
            series = [_series(s, start, 0.02) for s in SERVICES if s not in idle]
            if plant:
                series.append(_series("orders", start, 12345.0, name="fault_active"))
        elif query == QUERIES["error_ratio"]:
            series = [_series(s, start, 0.0) for s in SERVICES if s not in idle]
        else:
            raise AssertionError(f"unexpected query: {query}")
        return httpx.Response(200, json=_matrix(series))

    return PromAPI(transport=httpx.MockTransport(handler))


def _source(
    clock: Clock,
    metrics: SelfMetrics | None = None,
    skip_up: set[str] | None = None,
    skip_rate: set[str] | None = None,
    plant: bool = False,
    seen: list[tuple[str, float]] | None = None,
) -> PrometheusSource:
    return PrometheusSource(
        _api(skip_up=skip_up, skip_rate=skip_rate, plant=plant, seen=seen),
        now=clock.now,
        sleep=clock.sleep,
        metrics=metrics,
    )


def _take(source: PrometheusSource, steps: int) -> list[Sample]:
    """Every sample of the first `steps` steps, and not the first sample of the next one."""
    got: list[Sample] = []
    seen: list[float] = []
    for sample in source.samples():
        if sample.ts not in seen:
            if len(seen) == steps:
                break
            seen.append(sample.ts)
        got.append(sample)
    return got


def _metric(metrics: SelfMetrics, name: str) -> float:
    for family in metrics.registry.collect():
        for item in family.samples:
            if item.name == name and not item.labels:
                return item.value
    raise AssertionError(name)


class _RecordingDetector:
    def __init__(self) -> None:
        self.seen: list[Sample] = []

    def update(self, sample: Sample) -> None:
        self.seen.append(sample)
        return None


def test_samples_carry_prometheus_timestamps_not_wall_clock():
    seen: list[tuple[str, float]] = []
    clock = Clock(WALL)
    source = _source(clock, seen=seen)
    sample = next(source.samples())
    assert sample.ts == STEP
    assert sample.ts != WALL
    assert clock.sleeps == 0  # the step had already settled; nothing waited on the wall clock
    assert all(start == STEP for _, start in seen)
    assert WALL not in {start for _, start in seen}


def test_blocked_series_in_a_response_never_reach_the_detector():
    clock = Clock(WALL)
    source = _source(clock, plant=True)
    samples = _take(source, 2)
    assert {s.ts for s in samples} == {STEP, STEP + 15.0}
    detector = _RecordingDetector()
    for sample in samples:
        detector.update(sample)
    assert detector.seen
    assert all(sample.signal in SIGNALS for sample in detector.seen)
    assert all(sample.value != 12345.0 for sample in detector.seen)
    assert any(
        sample.service == "orders" and sample.signal == "latency_p95" and sample.value == 0.02
        for sample in detector.seen
    )


def test_one_step_is_yielded_once_even_if_polled_twice():
    seen: list[tuple[str, float]] = []
    clock = EarlyWake(WALL)
    source = _source(clock, seen=seen)
    samples = _take(source, 2)
    keys = [(s.ts, s.service, s.signal) for s in samples]
    assert len(keys) == len(set(keys))
    assert {s.ts for s in samples} == {STEP, STEP + 15.0}
    assert clock.sleeps >= 2  # the wait woke once before the clock moved, then again
    first = [query for query, start in seen if start == STEP]
    assert len(first) == len(QUERIES)
    assert len(first) == len(set(first))


def test_service_with_no_up_sample_becomes_telemetry_missing():
    clock = Clock(WALL)
    samples = _take(_source(clock, skip_up={"orders"}), 3)
    orders = [s for s in samples if s.service == "orders"]
    assert orders
    assert all(s.signal == "telemetry_missing" and s.value == 1.0 for s in orders)
    assert any(s.service == "storefront" and s.signal == "request_rate" for s in samples)
    detector = BandDetector(BandConfig())
    anomalies = [a for s in samples if (a := detector.update(s)) is not None]
    gaps = [a for a in anomalies if a.service == "orders"]
    assert len(gaps) == 1
    assert gaps[0].kind == "monitoring_gap" and gaps[0].first
    assert gaps[0].ts == STEP + 30.0  # the third missing step is the one that pages


def test_idle_service_with_up_one_is_skipped_not_paged():
    clock = Clock(WALL)
    samples = _take(_source(clock, skip_rate={"payments"}), 5)
    payments = [s for s in samples if s.service == "payments"]
    assert payments
    assert all(s.signal == "telemetry_missing" and s.value == 0.0 for s in payments)
    detector = BandDetector(BandConfig())
    anomalies = [a for s in samples if (a := detector.update(s)) is not None]
    assert anomalies == []


def test_self_metrics_count_samples_and_lag():
    clock = Clock(WALL)
    metrics = SelfMetrics()
    source = _source(clock, metrics=metrics)
    # One healthy step is 3 services x 4 signals. Stop before the next step is counted.
    samples = list(islice(source.samples(), 12))
    assert len(samples) == 12
    assert all(s.ts == STEP for s in samples)
    assert _metric(metrics, "always200_samples_total") == 12
    assert _metric(metrics, "always200_sample_lag_seconds") == pytest.approx(WALL - STEP)
