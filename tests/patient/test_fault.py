import random

import pytest

from patient.fault import Fault


def f(type_, size="severe", started=100.0, dur=200.0, base=0.1):
    return Fault(type_, size, started, dur, base)


def test_step_adds_its_full_share_of_base_p95():
    assert f("latency_step").extra_delay(150.0) == 0.4  # severe = +400% of 0.1 s


def test_ramp_reaches_full_size_at_half_duration():
    x = f("latency_ramp")
    assert abs(x.extra_delay(150.0) - 0.2) < 1e-9  # a quarter of the way = half size
    assert abs(x.extra_delay(250.0) - 0.4) < 1e-9


def test_inactive_fault_adds_nothing():
    assert f("latency_step").extra_delay(300.0) == 0.0
    assert f("latency_step").extra_delay(99.0) == 0.0


def test_error_share_matches_size():
    rng = random.Random(1)
    fails = sum(f("errors", "moderate").fails(150.0, rng) for _ in range(10_000))
    assert 800 <= fails <= 1200  # 10% ± 2 pp


def test_latency_faults_never_fail_requests():
    assert not f("latency_step").fails(150.0, random.Random(1))


def test_error_faults_add_no_delay():
    assert f("errors").extra_delay(150.0) == 0.0


def test_unknown_type_or_size_is_refused():
    with pytest.raises(ValueError, match="type"):
        f("latency_spike")
    with pytest.raises(ValueError, match="size"):
        f("errors", size="huge")
