import time
from pathlib import Path

import pytest

import patient.fault
from always_200.bench.answer_key import read_key
from always_200.bench.tune import BAND_GRID
from always_200.config import load_config
from always_200.core import Sample, sample_order
from always_200.detect.band import BandConfig, BandDetector
from always_200.detect.threshold import ThresholdDetector, fit_thresholds, night_half
from always_200.sources.replay import ReplaySource
from traffic.load import PEAK_RPS, day_curve

FIXTURE = Path(__file__).parents[2] / "fixtures" / "soak_synthetic"
KEY = read_key(FIXTURE / "answer_key.jsonl")
SOAK = [
    s
    for s in ReplaySource(FIXTURE / "samples.jsonl").samples()
    if KEY.scored_start <= s.ts < KEY.soak_end
]


def feed(det, svc, sig, values, t0=0.0, rps=5.0):
    out = []
    for i, v in enumerate(values):
        t = t0 + 15 * i
        det.update(Sample(t, svc, "request_rate", rps))
        out.append(det.update(Sample(t, svc, sig, v)))
    return out


def shape(anomalies):
    """What an alert stream looks like, value-free: None, 'first' or 'more' per step."""
    return [None if a is None else ("first" if a.first else "more") for a in anomalies]


def day_soak(minutes: int = 60) -> list[Sample]:
    """Storefront over one compressed day: 50 ms p95 at the lowest load, 90 ms at the peak.

    Latency rises in a straight line with load. The peak is 90 ms, not 80, because the fitting
    rule (night-half p99 + a 20 ms margin) puts the line at ~89.5 ms on a straight day; with an
    80 ms peak no fixed line fitted this way could ever fire, and bending the curve to make it fire
    would prove nothing."""
    out = []
    for i in range(minutes * 4):
        t = 15.0 * i
        share = day_curve(t)
        busy = (share - 0.30) / 0.70  # 0 at the lowest load, 1 at the peak
        out.append(Sample(t, "storefront", "request_rate", PEAK_RPS * share))
        out.append(Sample(t, "storefront", "latency_p95", 0.050 + 0.040 * busy))
    return out


# The plan's tests (step 16.1).


def test_threshold_has_an_absolute_margin_when_p99_is_zero():
    soak = [Sample(15 * i, "payments", "error_ratio", 0.0) for i in range(100)]
    soak += [Sample(15 * i, "payments", "request_rate", 5.0) for i in range(100)]
    th = fit_thresholds(sorted(soak, key=sample_order), factor=1.5)
    assert th[("payments", "error_ratio")] == pytest.approx(0.01)


def test_threshold_uses_same_n_and_gate_as_band():
    band_cfg = BandConfig()
    plain = ThresholdDetector({})
    assert (plain._n_fire, plain._n_clear, plain._min_requests_1m) == (
        band_cfg.n_fire,
        band_cfg.n_clear,
        band_cfg.min_requests_1m,
    )
    cfg = load_config()
    shared = ThresholdDetector.from_config({}, cfg)
    assert (shared._n_fire, shared._n_clear) == (cfg["timing"]["n_fire"], cfg["timing"]["n_clear"])
    assert shared._min_requests_1m == cfg["band"]["min_requests_1m"]

    # The same out/in pattern gives the same alert stream: fire on the 3rd out step, emit only on
    # out steps, clear after 4 in steps, and a gated step (24 requests/min) changes nothing.
    warm = [0.10] * 20  # fills the band's window; all under the threshold of 0.3
    steps = [0.5, 0.5, 0.10, 0.5, 0.5, 0.5, 0.5, 0.10, 0.10, 0.5, 0.10, 0.10, 0.10, 0.10, 0.5]
    for rps in (5.0, 0.5):  # 300 requests/min, and exactly 30 (the gate's edge, judged)
        band, th = BandDetector(band_cfg), ThresholdDetector({("orders", "latency_p95"): 0.3})
        feed(band, "orders", "latency_p95", warm)
        feed(th, "orders", "latency_p95", warm)
        b = feed(band, "orders", "latency_p95", steps, t0=300, rps=rps)
        t = feed(th, "orders", "latency_p95", steps, t0=300, rps=rps)
        assert shape(b) == shape(t)
        assert shape(t)[5] == "first" and "first" not in shape(t)[:5]
    th = ThresholdDetector({("orders", "latency_p95"): 0.3})
    assert feed(th, "orders", "latency_p95", [5.0] * 6, rps=0.4) == [None] * 6  # gated

    # Monitoring gaps: the same three-in-a-row rule as the band, the same kind of alert.
    pattern = [1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    band, th = BandDetector(band_cfg), ThresholdDetector({})
    b = [
        band.update(Sample(15.0 * i, "payments", "telemetry_missing", v))
        for i, v in enumerate(pattern)
    ]
    t = [
        th.update(Sample(15.0 * i, "payments", "telemetry_missing", v))
        for i, v in enumerate(pattern)
    ]
    assert b == t and t[5].kind == "monitoring_gap"


def test_night_fitted_threshold_false_alarms_at_midday():  # ruling P5, synthetic day curve
    soak = day_soak()
    for factor in (1.0, 1.25):
        th = fit_thresholds(soak, factor=factor)
        assert th[("storefront", "latency_p95")] < 0.090  # night p99 + 20 ms margin, under the peak
        d = ThresholdDetector(th)
        fired = [a for a in (d.update(s) for s in soak) if a is not None]
        assert any(a.first for a in fired)
        assert all(20 * 60 <= a.ts <= 40 * 60 for a in fired)  # only around midday
    band = BandDetector(BandConfig())
    assert all(band.update(s) is None for s in soak)  # the band follows the day


def test_threshold_never_exists_or_fires_for_request_rate():  # ruling P5
    th = fit_thresholds(SOAK, factor=1.0)
    assert all(sig != "request_rate" for _, sig in th)
    d = ThresholdDetector(th)
    assert all(
        d.update(Sample(15 * i, "storefront", "request_rate", 1e6)) is None for i in range(10)
    )


def test_subtle_error_size_clears_every_band_grid_point():  # ruling P10
    worst = max(p["k"] * p["error_floor"] for p in BAND_GRID)
    assert patient.fault.SIZES["subtle"][1] > worst
    assert patient.fault.SIZES["subtle"][1] >= load_config()["band"]["error_min_delta"]


# Extra tests: the rest of ruling P5 and the detector's edges.


def test_the_latency_threshold_is_the_larger_of_factor_and_margin():
    soak = []
    for i in range(100):
        soak += [
            Sample(15.0 * i, "orders", "request_rate", 5.0),
            Sample(15.0 * i, "orders", "latency_p95", 0.050),
        ]
    assert fit_thresholds(soak, factor=1.0)[("orders", "latency_p95")] == pytest.approx(0.070)
    assert fit_thresholds(soak, factor=2.0)[("orders", "latency_p95")] == pytest.approx(0.100)
    margin = fit_thresholds(soak, factor=1.0, latency_min_delta_s=0.005)
    assert margin[("orders", "latency_p95")] == pytest.approx(0.055)


def test_the_fit_is_the_p99_not_the_maximum():
    soak = []
    for i in range(200):
        value = 1.0 if i == 7 else 0.050  # one wild step in 200
        soak += [
            Sample(15.0 * i, "orders", "request_rate", 5.0),
            Sample(15.0 * i, "orders", "latency_p95", value),
        ]
    assert fit_thresholds(soak, factor=1.0)[("orders", "latency_p95")] < 0.2


def test_night_half_keeps_whole_low_load_steps():
    soak = []
    for i, rps in enumerate([1.0, 4.0, 2.0, 3.0]):
        soak += [
            Sample(15.0 * i, "storefront", "request_rate", rps),
            Sample(15.0 * i, "payments", "latency_p95", 0.01 * i),
        ]
    kept = night_half(soak)
    assert sorted({s.ts for s in kept}) == [0.0, 30.0]  # rates 1 and 2; median 2.5
    assert len(kept) == 4  # every sample of those steps, any service


def test_night_half_without_a_storefront_rate_keeps_everything():
    soak = [Sample(15.0 * i, "payments", "request_rate", 5.0) for i in range(4)]
    assert night_half(soak) == soak


def test_gated_steps_are_not_fitted():
    soak = []
    for i in range(50):
        soak += [
            Sample(15.0 * i, "orders", "request_rate", 0.1),  # 6 requests a minute
            Sample(15.0 * i, "orders", "latency_p95", 5.0),
        ]
    assert fit_thresholds(soak, factor=1.0) == {}


def test_a_series_without_a_threshold_never_alerts():
    d = ThresholdDetector({("orders", "latency_p95"): 0.3})
    assert feed(d, "payments", "latency_p95", [9.0] * 6) == [None] * 6
    assert feed(d, "orders", "error_ratio", [1.0] * 6) == [None] * 6


def test_the_threshold_edge_is_out():
    d = ThresholdDetector({("payments", "error_ratio"): 0.01})
    a = feed(d, "payments", "error_ratio", [0.01] * 3)[-1]
    assert a is not None and a.first and (a.centre, a.spread, a.kind) == (0.01, 0.0, "fault")


def test_the_constructor_refuses_a_request_rate_threshold():
    with pytest.raises(ValueError, match="request_rate"):
        ThresholdDetector({("orders", "request_rate"): 100.0})
    with pytest.raises(ValueError):
        ThresholdDetector({}, n_fire=0)
    with pytest.raises(ValueError):
        fit_thresholds(SOAK, factor=0.0)


def test_the_threshold_detector_never_reads_the_wall_clock(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: (_ for _ in ()).throw(AssertionError("wall clock")))
    d = ThresholdDetector(fit_thresholds(SOAK, factor=1.5))
    assert all(d.update(s) is None for s in SOAK)


def test_the_synthetic_soak_fit_covers_both_signals_of_every_service():
    th = fit_thresholds(SOAK, factor=1.5)
    assert sorted(th) == sorted(
        (svc, sig)
        for svc in ("storefront", "orders", "payments")
        for sig in ("latency_p95", "error_ratio")
    )
