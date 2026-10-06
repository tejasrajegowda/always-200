import math
import time

import pytest

from always_200.__main__ import DETECTORS
from always_200.config import load_config
from always_200.core import Sample
from always_200.detect.band import MAD_TO_SIGMA, BandConfig, BandDetector


def feed(det, svc, sig, values, t0=0.0, rps=5.0):
    out = []
    for i, v in enumerate(values):
        t = t0 + 15 * i
        det.update(Sample(t, svc, "request_rate", rps))
        out.append(det.update(Sample(t, svc, sig, v)))
    return out


def gaps(det, svc, values, t0=0.0):
    return [
        det.update(Sample(t0 + 15 * i, svc, "telemetry_missing", v)) for i, v in enumerate(values)
    ]


# The plan's tests (step 14.1): one per r2 section 8 rule.


def test_silent_until_window_full():
    d = BandDetector(BandConfig())
    assert all(a is None for a in feed(d, "orders", "latency_p95", [0.1] * 19 + [5.0]))


def test_fires_on_third_consecutive_out_step():
    d = BandDetector(BandConfig())
    res = feed(d, "orders", "latency_p95", [0.10] * 20 + [0.5, 0.5, 0.5])
    assert res[20] is None and res[21] is None and res[22].first is True


def test_single_spike_does_not_fire():
    d = BandDetector(BandConfig())
    res = feed(d, "orders", "latency_p95", [0.10] * 20 + [0.5, 0.10, 0.5, 0.5, 0.10, 0.5])
    assert all(a is None for a in res)


def test_clears_after_four_in_band_steps_and_is_silent_during_countdown():
    d = BandDetector(BandConfig())
    feed(d, "orders", "latency_p95", [0.10] * 20 + [0.5] * 3)  # fired
    countdown = feed(d, "orders", "latency_p95", [0.10] * 3, t0=345)
    assert countdown == [None, None, None]  # P1: countdown steps return None
    still = feed(d, "orders", "latency_p95", [0.5], t0=390)[0]
    assert still is not None and still.first is False  # still alerting: emits, not a new first
    cleared = feed(d, "orders", "latency_p95", [0.10] * 4, t0=405)
    assert cleared == [None] * 4
    again = feed(d, "orders", "latency_p95", [0.5] * 3, t0=465)
    assert again[0] is None and again[1] is None and again[2].first is True  # a new alert


def test_value_exactly_at_the_band_edge_fires():
    cfg = BandConfig()
    edge = 0.0 + cfg.k * cfg.error_floor  # zeros: median 0, MAD 0, spread = error floor
    d = BandDetector(cfg)
    assert feed(d, "payments", "error_ratio", [0.0] * 20 + [edge] * 3)[-1] is not None
    below = math.nextafter(edge, 0.0)
    d = BandDetector(cfg)
    assert all(a is None for a in feed(d, "payments", "error_ratio", [0.0] * 20 + [below] * 3))


def test_baseline_is_frozen_while_alerting():
    d = BandDetector(BandConfig())
    feed(d, "orders", "latency_p95", [0.10] * 20)
    before = d.window("orders", "latency_p95")
    feed(d, "orders", "latency_p95", [0.5] * 5 + [0.10] * 3, t0=300)
    assert d.window("orders", "latency_p95") == before


def test_error_ratio_floor_stops_one_error_from_firing():
    d = BandDetector(BandConfig())
    assert all(a is None for a in feed(d, "payments", "error_ratio", [0.0] * 20 + [0.004] * 5))


def test_real_error_burst_fires():
    d = BandDetector(BandConfig())
    assert feed(d, "payments", "error_ratio", [0.0] * 20 + [0.05] * 3)[-1] is not None


def test_min_absolute_delta_on_a_very_quiet_series():
    # At k = 3 (the lowest grid k) a 10 ms series has its band edge at 10 + 3 x 5 = 25 ms, so
    # 26 ms is out of band; it is still only 16 ms above the median (< 20 ms): no alert.
    cfg = BandConfig(k=3.0)
    d = BandDetector(cfg)
    assert all(a is None for a in feed(d, "orders", "latency_p95", [0.010] * 20 + [0.026] * 5))
    d = BandDetector(cfg)
    assert feed(d, "orders", "latency_p95", [0.010] * 20 + [0.031] * 3)[-1] is not None
    # The plan's own example: 10 ms -> 25 ms is not an alert at the default k either.
    d = BandDetector(BandConfig())
    assert all(a is None for a in feed(d, "orders", "latency_p95", [0.010] * 20 + [0.025] * 5))


def test_min_absolute_delta_on_the_error_ratio():
    # k = 3 with the lowest error floor: edge 0 + 3 x 0.0025 = 0.75%, but 0.9% is < 1 pp above 0.
    d = BandDetector(BandConfig(k=3.0, error_floor=0.0025))
    assert all(a is None for a in feed(d, "payments", "error_ratio", [0.0] * 20 + [0.009] * 5))


def test_request_gate_skips_low_traffic_steps():
    d = BandDetector(BandConfig())
    res = feed(d, "orders", "latency_p95", [0.1] * 20 + [5.0] * 5, rps=0.4)  # 24 per minute
    assert all(a is None for a in res)
    assert d.window("orders", "latency_p95") == ()  # nothing learned either


def test_downward_moves_never_alert():
    d = BandDetector(BandConfig())
    assert all(a is None for a in feed(d, "orders", "latency_p95", [0.5] * 20 + [0.001] * 10))
    d = BandDetector(BandConfig())
    assert all(a is None for a in feed(d, "payments", "error_ratio", [0.2] * 20 + [0.0] * 10))


def test_three_missing_steps_raise_a_monitoring_gap():
    d = BandDetector(BandConfig())
    res = gaps(d, "payments", [1.0, 1.0, 1.0])
    assert res[0] is None and res[1] is None
    a = res[2]
    assert a.kind == "monitoring_gap" and a.signal == "telemetry_missing" and a.first is True
    assert (a.service, a.value, a.centre, a.spread) == ("payments", 1.0, 0.0, 0.0)


def test_detector_never_reads_the_wall_clock(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: (_ for _ in ()).throw(AssertionError("wall clock")))
    monkeypatch.setattr(
        time, "monotonic", lambda: (_ for _ in ()).throw(AssertionError("wall clock"))
    )
    feed(BandDetector(BandConfig()), "orders", "latency_p95", [0.1] * 25)


def test_same_input_same_output():
    values = [0.10, 0.11, 0.09, 0.12, 0.10] * 4 + [0.4, 0.5, 0.45, 0.6, 0.1, 0.1, 0.1, 0.1, 0.5]
    runs = [feed(BandDetector(BandConfig()), "orders", "latency_p95", values) for _ in range(2)]
    assert runs[0] == runs[1]
    assert any(a is not None for a in runs[0])


# Extra tests: the remaining rules of the step text, pinned one by one.


def test_out_of_band_values_before_firing_are_not_learned():
    d = BandDetector(BandConfig())
    feed(d, "orders", "latency_p95", [0.10] * 20 + [0.5, 0.11])
    window = d.window("orders", "latency_p95")
    assert 0.5 not in window and window[-1] == 0.11 and len(window) == 20


def test_the_clearing_step_is_learned_but_the_countdown_is_not():
    d = BandDetector(BandConfig())
    feed(d, "orders", "latency_p95", [0.10] * 20 + [0.5] * 3 + [0.11, 0.12, 0.13, 0.125])
    window = d.window("orders", "latency_p95")
    assert window[-1] == 0.125 and window[-2] == 0.10  # 0.11-0.13 (countdown) never learned


def test_window_slides_once_full():
    d = BandDetector(BandConfig())
    feed(d, "orders", "latency_p95", [0.10] * 20 + [0.11] * 5)
    window = d.window("orders", "latency_p95")
    assert len(window) == 20 and window[-5:] == (0.11,) * 5


def test_the_anomaly_reports_value_centre_and_floored_spread():
    d = BandDetector(BandConfig())
    a = feed(d, "orders", "latency_p95", [0.10] * 20 + [0.5] * 3, t0=1000.0)[-1]
    assert (a.ts, a.service, a.signal, a.kind, a.value) == (
        1030.0 + 15 * 20,
        "orders",
        "latency_p95",
        "fault",
        0.5,
    )
    assert a.centre == pytest.approx(0.10) and a.spread == pytest.approx(0.010)  # 10% floor


def test_latency_absolute_floor_on_a_tiny_median():
    d = BandDetector(BandConfig())
    a = feed(d, "orders", "latency_p95", [0.002] * 20 + [0.5] * 3)[-1]
    assert a.spread == 0.005  # max(10% x 2 ms, 5 ms)


def test_mad_is_used_when_it_beats_the_floor():
    values = [0.08, 0.12] * 10  # median 0.10, MAD 0.02 -> spread 0.0297 > 10% floor
    d = BandDetector(BandConfig())
    a = feed(d, "orders", "latency_p95", values + [0.5] * 3)[-1]
    assert a.centre == pytest.approx(0.10)
    assert a.spread == pytest.approx(MAD_TO_SIGMA * 0.02)


def test_one_wild_value_in_the_window_does_not_move_the_band():
    # The 20th (warm-up) value is wild, yet the median and MAD ignore it: 0.15 still fires.
    d = BandDetector(BandConfig())
    a = feed(d, "orders", "latency_p95", [0.10] * 19 + [9.0] + [0.15] * 3)[-1]
    assert a is not None and a.centre == pytest.approx(0.10)


def test_request_gate_edge_is_evaluated():
    d = BandDetector(BandConfig())
    res = feed(d, "orders", "latency_p95", [0.10] * 20 + [0.5] * 3, rps=0.5)  # exactly 30
    assert res[-1] is not None


def test_gated_steps_do_not_break_the_n_in_a_row_count():
    d = BandDetector(BandConfig())
    feed(d, "orders", "latency_p95", [0.10] * 20 + [0.5] * 2)
    feed(d, "orders", "latency_p95", [0.10] * 3, t0=330, rps=0.1)  # gated: no state change
    assert feed(d, "orders", "latency_p95", [0.5], t0=375)[0].first is True


def test_request_rate_never_yields_an_anomaly():
    d = BandDetector(BandConfig())
    assert all(
        d.update(Sample(15.0 * i, "orders", "request_rate", 5.0 if i < 20 else 500.0)) is None
        for i in range(40)
    )


def test_series_are_independent():
    d = BandDetector(BandConfig())
    feed(d, "orders", "latency_p95", [0.10] * 20)
    feed(d, "payments", "latency_p95", [0.30] * 20)
    feed(d, "orders", "error_ratio", [0.0] * 20)
    assert d.window("orders", "latency_p95") == (0.10,) * 20
    assert d.window("payments", "latency_p95") == (0.30,) * 20
    assert d.window("orders", "error_ratio") == (0.0,) * 20
    assert d.window("storefront", "latency_p95") == ()


def test_monitoring_gap_needs_three_in_a_row_and_clears_after_four():
    d = BandDetector(BandConfig())
    assert gaps(d, "orders", [1.0, 1.0, 0.0, 1.0, 1.0]) == [None] * 5
    res = gaps(d, "orders", [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0], t0=75)
    assert res[0].first is True  # the third missing step in a row
    assert res[1:4] == [None] * 3 and res[4].first is False  # countdown, then still alerting
    assert res[5:9] == [None] * 4 and res[9] is None  # cleared; one missing step is not a gap


def test_gap_samples_never_touch_the_baseline():
    d = BandDetector(BandConfig())
    feed(d, "orders", "latency_p95", [0.10] * 20)
    before = d.window("orders", "latency_p95")
    gaps(d, "orders", [1.0] * 6, t0=300)
    assert d.window("orders", "latency_p95") == before
    assert feed(d, "orders", "latency_p95", [0.5] * 3, t0=400)[-1].kind == "fault"


def test_from_config_matches_the_shared_config():
    cfg = load_config()
    band = BandConfig.from_config(cfg)
    assert band == BandConfig()  # the defaults in code and in config/default.yaml agree
    assert (band.n_fire, band.n_clear) == (cfg["timing"]["n_fire"], cfg["timing"]["n_clear"])


def test_from_config_reads_overrides_and_refuses_unknown_keys():
    cfg = {"band": {"k": 5, "error_floor": 0.0025}, "timing": {"n_fire": 2, "n_clear": 3}}
    band = BandConfig.from_config(cfg)
    assert (band.k, band.error_floor, band.n_fire, band.n_clear) == (5.0, 0.0025, 2, 3)
    with pytest.raises(ValueError, match="kk"):
        BandConfig.from_config({"band": {"kk": 5}})


def test_the_bench_command_knows_the_band():
    assert isinstance(DETECTORS["band"](), BandDetector)
