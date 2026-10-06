import math
import random

import pytest
import yaml

from always_200.__main__ import main
from always_200.bench.answer_key import AnswerKey, Fault, write_key
from always_200.bench.files import write_samples
from always_200.bench.tune import BAND_GRID, THRESHOLD_GRID, OperatingPoint, select, tune
from always_200.config import load_config
from always_200.core import SERVICES, Sample

T0 = 1_800_000_000.0
WARMUP_S, SOAK_S, SLOTS_S = 600, 1200, 1800
SLOTS = T0 + WARMUP_S + SOAK_S
# (id, target, type, start offset into the slots, how the value changes)
FAULTS = [
    ("f01", "orders", "latency_step", 120, lambda v: v * 2.0),
    ("f02", "payments", "errors", 720, lambda v: 0.10),
    # +60% on 50 ms: caught by a threshold at factor 1.0 or 1.25 (night p99 + 20 ms ~ 74 ms),
    # missed at 1.5 or 2.0 (and at 2.0 the +100% fault, 100 ms < 2 x 54 ms, is missed too);
    # caught by the band only where k x floor is small enough.
    ("f03", "storefront", "latency_step", 1320, lambda v: v * 1.6),
]
FAULT_S = 180


def build_session(folder, seed="T1", burst=False):
    """A one-hour synthetic session: 10 min warm-up, 20 min clean soak, 30 min of slots with three
    3-minute faults. `burst` adds a 1-minute latency burst on payments in clean time, a false
    alarm for every grid point."""
    rng = random.Random("always-200-tune-test")
    samples = []
    for i in range((WARMUP_S + SOAK_S + SLOTS_S) // 15 + 1):
        ts = T0 + 15 * i
        for service in SERVICES:
            p95 = 0.050 * math.exp(rng.gauss(0.0, 0.03))
            err = 0.0
            for _fid, target, ftype, offset, change in FAULTS:
                if target == service and SLOTS + offset <= ts < SLOTS + offset + FAULT_S:
                    if ftype == "errors":
                        err = change(err)
                    else:
                        p95 = change(p95)
            if burst and service == "payments" and SLOTS + 1695 <= ts < SLOTS + 1755:
                p95 *= 4.0
            samples += [
                Sample(ts, service, "request_rate", 10.0),
                Sample(ts, service, "latency_p95", round(p95, 6)),
                Sample(ts, service, "error_ratio", err),
                Sample(ts, service, "telemetry_missing", 0.0),
            ]
    faults = tuple(
        Fault(f"{seed}-{fid}", ftype, target, "subtle", SLOTS + off, SLOTS + off + FAULT_S, True)
        for fid, target, ftype, off, _ in FAULTS
    )
    key = AnswerKey(seed, T0 + WARMUP_S, SLOTS, SLOTS + SLOTS_S, faults, ())
    folder.mkdir(parents=True, exist_ok=True)
    write_samples(folder / "samples.jsonl.gz", samples)
    write_key(folder / "answer_key.jsonl", key)
    return folder


@pytest.fixture(scope="module")
def tuned(tmp_path_factory):
    root = tmp_path_factory.mktemp("tune")
    session = build_session(root / "T1")
    results = tune(session, load_config(), root / "out")
    return results, root / "out"


# The plan's tests (step 16.1).


def test_select_prefers_detection_under_the_fa_limit():
    pts = [
        OperatingPoint("b", {"k": 3}, 0.95, 0.9),
        OperatingPoint("b", {"k": 4}, 0.85, 0.4),
        OperatingPoint("b", {"k": 5}, 0.80, 0.1),
    ]
    assert select(pts) == (pts[1], True)


def test_select_ties_break_on_lower_fa_then_grid_order():
    pts = [
        OperatingPoint("b", {"k": 3}, 0.80, 0.4),
        OperatingPoint("b", {"k": 4}, 0.80, 0.2),
        OperatingPoint("b", {"k": 5}, 0.80, 0.2),
        OperatingPoint("b", {"k": 6}, 0.70, 0.0),
    ]
    assert select(pts) == (pts[1], True)  # equal detection: lower FA, then the first in order
    same = [OperatingPoint("b", {"k": k}, 0.5, 0.3) for k in (3, 4, 5)]
    assert select(same)[0] is same[0]


def test_select_reports_infeasible_when_nothing_qualifies():  # Review Focus 3
    pts = [OperatingPoint("b", {"k": 3}, 0.9, 2.0), OperatingPoint("b", {"k": 6}, 0.5, 0.8)]
    assert select(pts) == (pts[1], False)


# Extra tests: the rule's edges, the grids, and tune end to end on a synthetic session.


def test_select_counts_the_limit_itself_as_feasible():
    pts = [OperatingPoint("b", {"k": 3}, 0.9, 0.51), OperatingPoint("b", {"k": 4}, 0.6, 0.5)]
    assert select(pts) == (pts[1], True)
    assert select(pts, max_fa_per_h=1.0) == (pts[0], True)


def test_select_infeasible_ties_break_on_detection_then_grid_order():
    pts = [
        OperatingPoint("b", {"k": 3}, 0.5, 0.8),
        OperatingPoint("b", {"k": 4}, 0.7, 0.8),
        OperatingPoint("b", {"k": 5}, 0.7, 0.8),
    ]
    assert select(pts) == (pts[1], False)


def test_select_refuses_an_empty_grid():
    with pytest.raises(ValueError):
        select([])


def test_the_grids_are_the_ones_ruling_p10_fixed():
    assert len(BAND_GRID) == 36
    assert {p["k"] for p in BAND_GRID} == {3, 4, 5, 6}
    assert {p["latency_floor_frac"] for p in BAND_GRID} == {0.05, 0.10, 0.20}
    assert {p["error_floor"] for p in BAND_GRID} == {0.0025, 0.005, 0.0075}
    assert len({tuple(sorted(p.items())) for p in BAND_GRID}) == 36
    assert [p["factor"] for p in THRESHOLD_GRID] == [1.0, 1.25, 1.5, 2.0]
    assert load_config()["threshold"]["factor"] in [p["factor"] for p in THRESHOLD_GRID]


def test_tune_scores_both_grids_and_selects_by_the_rule(tuned):
    results, out = tuned
    band, band_ok = results["band"]
    threshold, th_ok = results["threshold"]
    assert band_ok and th_ok
    assert band.detection == pytest.approx(1.0) and band.fa_per_h == 0.0
    assert threshold.params == {"factor": 1.0} and threshold.detection == pytest.approx(1.0)

    th_file = yaml.safe_load((out / "threshold.yaml").read_text(encoding="utf-8"))
    by_factor = {c["params"]["factor"]: c for c in th_file["candidates"]}
    assert by_factor[1.0]["detection"] == pytest.approx(1.0)
    assert by_factor[1.5]["detection"] == pytest.approx(2 / 3, abs=1e-4)  # +60% missed
    assert by_factor[2.0]["detection"] == pytest.approx(1 / 3, abs=1e-4)  # +100% missed too
    assert th_file["selected"]["feasible"] is True and th_file["session"] == "T1"
    assert set(th_file["selected"]["thresholds"]) == {
        f"{svc}/{sig}" for svc in SERVICES for sig in ("latency_p95", "error_ratio")
    }

    band_file = yaml.safe_load((out / "band.yaml").read_text(encoding="utf-8"))
    assert len(band_file["candidates"]) == 36
    assert [c["params"] for c in band_file["candidates"]] == BAND_GRID
    assert band_file["selected"]["params"] == band.params
    assert len(band_file["sweep"]) == 4  # k varies at the chosen floors (P10)
    assert {c["params"]["k"] for c in band_file["sweep"]} == {3, 4, 5, 6}
    assert min(c["detection"] for c in band_file["candidates"]) < 1.0  # the knobs matter


def test_tune_prints_both_grids(tmp_path, capsys):
    session = build_session(tmp_path / "T1")
    tune(session, load_config(), tmp_path / "out")
    printed = capsys.readouterr().out
    assert "band (36 points)" in printed and "threshold (4 points)" in printed
    assert printed.count("<- selected") == 2 and "INFEASIBLE" not in printed


def test_tune_flags_an_infeasible_session_and_still_picks(tmp_path, capsys):  # Review Focus 3
    session = build_session(tmp_path / "T1", burst=True)
    results = tune(session, load_config(), tmp_path / "out")
    assert results["band"][1] is False and results["threshold"][1] is False
    assert "INFEASIBLE" in capsys.readouterr().out
    band_file = yaml.safe_load((tmp_path / "out" / "band.yaml").read_text(encoding="utf-8"))
    assert band_file["selected"]["feasible"] is False
    fa = [c["fa_per_h"] for c in band_file["candidates"]]
    assert band_file["selected"]["fa_per_h"] == min(fa)


def test_tune_refuses_a_held_out_session(tmp_path):
    session = build_session(tmp_path / "B1", seed="B1")
    with pytest.raises(ValueError, match="session A only"):
        tune(session, load_config(), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_tune_never_overwrites_earlier_output(tmp_path):
    session = build_session(tmp_path / "T1")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "band.yaml").write_text("earlier\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        tune(session, load_config(), tmp_path / "out")
    assert (tmp_path / "out" / "band.yaml").read_text(encoding="utf-8") == "earlier\n"


def test_the_tune_command(tmp_path, capsys):
    session = build_session(tmp_path / "T1")
    assert main(["tune", "--session", str(session), "--out", str(tmp_path / "out")]) == 0
    assert "threshold (4 points)" in capsys.readouterr().out
    assert (tmp_path / "out" / "band.yaml").is_file()
    assert (tmp_path / "out" / "threshold.yaml").is_file()
