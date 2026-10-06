import dataclasses

import pytest

from always_200.config import load_config
from always_200.core import SERVICES, SIGNALS, Anomaly, Sample, sample_order


def test_answer_key_series_cannot_become_a_sample():
    with pytest.raises(ValueError):
        Sample(1.0, "orders", "fault_active", 1.0)


@pytest.mark.parametrize(
    "signal", ["fault_active", "event_active", "deploy_marker_total", "session_phase", "up"]
)
def test_no_answer_key_or_raw_series_passes_the_seam(signal):
    with pytest.raises(ValueError, match="not allowed past the seam"):
        Sample(1.0, "orders", signal, 1.0)


def test_nan_cannot_cross_the_seam():
    with pytest.raises(ValueError):
        Sample(1.0, "orders", "latency_p95", float("nan"))


def test_infinity_cannot_cross_the_seam():
    with pytest.raises(ValueError, match="non-finite"):
        Sample(1.0, "orders", "latency_p95", float("inf"))


def test_an_unknown_service_is_refused():
    with pytest.raises(ValueError, match="unknown service"):
        Sample(1.0, "postgres", "latency_p95", 0.1)


def test_a_sample_is_read_only():
    s = Sample(1.0, "orders", "latency_p95", 0.1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.value = 9.9  # type: ignore[misc]


def test_sample_order_is_time_then_service_then_signal():
    a = Sample(15.0, "payments", "request_rate", 1.0)
    b = Sample(15.0, "orders", "telemetry_missing", 0.0)
    c = Sample(15.0, "orders", "request_rate", 1.0)
    d = Sample(0.0, "payments", "telemetry_missing", 0.0)
    assert sorted([a, b, c, d], key=sample_order) == [d, c, b, a]


def test_core_services_match_the_config_in_order():
    assert list(SERVICES) == load_config()["services"]


def test_signal_order_is_ruling_p7():
    assert SIGNALS == ("request_rate", "latency_p95", "error_ratio", "telemetry_missing")


def test_an_anomaly_kind_must_be_known():
    ok = Anomaly(1.0, "orders", "latency_p95", "fault", 0.5, 0.1, 0.01, True)
    assert ok.first is True
    with pytest.raises(ValueError, match="kind"):
        Anomaly(1.0, "orders", "latency_p95", "page", 0.5, 0.1, 0.01, True)
