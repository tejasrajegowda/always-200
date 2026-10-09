"""Shared write-up fixtures: a payments latency fault seen by all three services.

Every 15 s from t-11 to t+0 the stream holds all four signals for all three services. Latency
jumps just after t-3 (payments 50 -> 412 ms, orders 80 -> 445 ms, storefront 120 -> 480 ms) and
payments' errors rise to 4.1%. No printed value is near 10, so a "10" can only come from a minute
label or a runbook digit.
"""

import pytest

from always_200.core import Anomaly, Sample
from always_200.graph import Graph
from always_200.incidents import Incident
from always_200.writeup.context import build_context
from always_200.writeup.writeup import load_schema

T0 = 36000.0
BEFORE = {"storefront": 0.120, "orders": 0.080, "payments": 0.050}
AFTER = {"storefront": 0.480, "orders": 0.445, "payments": 0.412}


def make_samples(t0: float = T0) -> list[Sample]:
    out = []
    ts = t0 - 660
    while ts <= t0:
        sick = ts > t0 - 180
        for svc in ("storefront", "orders", "payments"):
            lat = (AFTER if sick else BEFORE)[svc]
            err = 0.041 if sick and svc == "payments" else 0.0
            out += [
                Sample(ts, svc, "request_rate", 25.3),
                Sample(ts, svc, "latency_p95", lat),
                Sample(ts, svc, "error_ratio", err),
                Sample(ts, svc, "telemetry_missing", 0.0),
            ]
        ts += 15
    return out


def make_incident(culprit: str | None = "payments", t0: float = T0) -> Incident:
    alerts = [
        Anomaly(t0, "storefront", "latency_p95", "fault", 0.48, 0.12, 0.01, True),
        Anomaly(t0 + 15, "payments", "latency_p95", "fault", 0.412, 0.05, 0.01, False),
    ]
    return Incident(f"fault-{int(t0)}", "fault", t0, t0 + 15, alerts, culprit)


@pytest.fixture
def G() -> Graph:
    return Graph({"storefront": ("orders",), "orders": ("payments",), "payments": ()})


@pytest.fixture
def SAMPLES() -> list[Sample]:
    return make_samples()


@pytest.fixture
def INC() -> Incident:
    return make_incident()


@pytest.fixture
def RUNBOOKS() -> dict[str, str]:
    return {
        "storefront": "Retries 3 times, then answers 502.",
        "orders": "A restart takes 10 s; the pool holds 20 connections.",
        "payments": "Inserts 1 row per payment.",
    }


@pytest.fixture
def CTX(INC, SAMPLES, G, RUNBOOKS):
    return build_context(INC, SAMPLES, [], G, RUNBOOKS)


@pytest.fixture
def SCHEMA() -> dict:
    return load_schema()


@pytest.fixture
def VALID() -> dict:
    return {
        "summary": "payments p95 latency rose from 50 ms to 412 ms and its errors reached 4.1%.",
        "suspected_service": "payments",
        "evidence": [
            {"service": "payments", "signal": "latency_p95", "ts": 0, "value": 412},
            {"service": "payments", "signal": "error_ratio", "ts": 0, "value": 4.1},
        ],
        "first_check": "Check the latest payments deploy and the payments insert time.",
    }
