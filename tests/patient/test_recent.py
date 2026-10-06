import pytest

from patient.recent import RecentLatency


def test_p95_of_1_to_100_ms_is_95_ms():
    r = RecentLatency()
    for ms in range(1, 101):
        r.add(ms / 1000)
    assert r.p95() == pytest.approx(0.095, abs=0.001)


def test_default_until_the_first_request():
    assert RecentLatency().p95() == 0.05
    assert RecentLatency().p95(default=0.2) == 0.2


def test_only_the_last_500_values_count():
    r = RecentLatency()
    for _ in range(1000):
        r.add(10.0)  # old and slow: all pushed out below
    for _ in range(500):
        r.add(0.01)
    assert r.p95() == 0.01
