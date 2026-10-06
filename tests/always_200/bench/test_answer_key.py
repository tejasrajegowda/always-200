import pytest

from always_200.bench.answer_key import (
    AnswerKey,
    Event,
    Fault,
    derive,
    latest_run,
    on_intervals,
    read_key,
    scrape_gaps,
    write_key,
)
from always_200.bench.files import read_jsonl

# session_phase: 0 warm-up, 1 clean soak, 2 slots, 3 finished
PHASES = [(0.0, 0.0), (600.0, 0.0), (605.0, 1.0), (4200.0, 1.0), (4205.0, 2.0), (9000.0, 3.0)]


def test_on_intervals_uses_first_one_and_first_zero():
    pts = [(0, 0), (5, 0), (10, 1), (15, 1), (20, 0), (25, 0)]
    assert on_intervals(pts) == [(10, 20, True)]


def test_unclosed_fault_ends_at_last_point():
    assert on_intervals([(0, 1), (5, 1)]) == [(0, 5, False)]


def test_on_intervals_finds_every_run_and_sorts_its_input():
    pts = [(20, 1), (0, 1), (5, 0), (25, 0), (30, 1), (35, 0)]
    assert on_intervals(pts) == [(0, 5, True), (20, 25, True), (30, 35, True)]


def test_on_intervals_of_nothing_is_nothing():
    assert on_intervals([]) == []
    assert on_intervals([(0, 0), (5, 0)]) == []


def test_session_window_is_the_latest_contiguous_run():  # ruling P12
    pts = [(0, 1), (5, 1), (10, 0), (100, 1), (105, 1), (110, 0)]
    assert latest_run(pts) == (100, 110, True)


def test_latest_run_of_a_seed_never_seen_is_none():
    assert latest_run([(0, 0), (5, 0)]) is None


def test_derive_numbers_faults_by_start_and_reads_phases():
    faults = {
        ("payments", "errors", "severe"): [(5000.0, 0.0), (5005.0, 1.0), (5200.0, 0.0)],
        ("orders", "latency_ramp", "subtle"): [(4300.0, 0.0), (4305.0, 1.0), (4500.0, 0.0)],
        ("orders", "latency_step", "moderate"): [(8800.0, 1.0), (9000.0, 1.0)],
        ("storefront", "errors", "subtle"): [(4300.0, 0.0), (9000.0, 0.0)],  # never fired
    }
    events = {
        "traffic_spike": [(6000.0, 0.0), (6005.0, 1.0), (6150.0, 0.0)],
        "rolling_restart": [(4250.0, 1.0), (4260.0, 0.0)],
    }
    key = derive("A1", faults, events, PHASES)
    assert (key.seed, key.scored_start, key.soak_end, key.scored_end) == (
        "A1",
        605.0,
        4205.0,
        9000.0,
    )
    assert key.faults == (
        Fault("A1-f01", "latency_ramp", "orders", "subtle", 4305.0, 4500.0, True),
        Fault("A1-f02", "errors", "payments", "severe", 5005.0, 5200.0, True),
        Fault("A1-f03", "latency_step", "orders", "moderate", 8800.0, 9000.0, False),
    )
    assert key.events == (
        Event("rolling_restart", 4250.0, 4260.0),
        Event("traffic_spike", 6005.0, 6150.0),
    )


def test_derive_refuses_a_session_that_never_finished():
    with pytest.raises(ValueError, match="phase"):
        derive("A1", {}, {}, [(0.0, 0.0), (605.0, 1.0), (4205.0, 2.0)])


def test_derive_refuses_an_empty_phase_series():
    with pytest.raises(ValueError, match="phase"):
        derive("A1", {}, {}, [])


def test_scrape_gap_over_30s_is_reported():
    assert scrape_gaps([(0, 1), (5, 1), (45, 1)]) == [(5, 45)]


def test_a_gap_of_exactly_30s_is_not_a_hole():
    assert scrape_gaps([(0, 1), (30, 1), (35, 1)]) == []


def test_scrape_gaps_sorts_its_input_and_respects_the_limit():
    assert scrape_gaps([(45, 1), (0, 1), (5, 1)], max_gap_s=50.0) == []
    assert scrape_gaps([(45, 1), (0, 1), (5, 1)], max_gap_s=4.0) == [(0, 5), (5, 45)]


def test_key_round_trip_and_line_format(tmp_path):
    key = AnswerKey(
        seed="A1",
        scored_start=605.0,
        soak_end=4205.0,
        scored_end=9000.0,
        faults=(Fault("A1-f01", "latency_ramp", "orders", "subtle", 4305.0, 4500.0, True),),
        events=(Event("traffic_spike", 6005.0, 6150.0),),
    )
    path = tmp_path / "answer_key.jsonl"
    write_key(path, key)
    rows = read_jsonl(path)
    assert rows[0] == {
        "kind": "session",
        "seed": "A1",
        "scored_start": 605.0,
        "soak_end": 4205.0,
        "scored_end": 9000.0,
    }
    assert rows[1] == {
        "kind": "fault",
        "id": "A1-f01",
        "target": "orders",
        "type": "latency_ramp",
        "size": "subtle",
        "start": 4305.0,
        "end": 4500.0,
        "closed": True,
    }
    assert rows[2] == {"kind": "event", "event": "traffic_spike", "start": 6005.0, "end": 6150.0}
    assert read_key(path) == key


def test_a_key_file_needs_exactly_one_session_line(tmp_path):
    path = tmp_path / "answer_key.jsonl"
    path.write_text('{"kind": "event", "event": "deploy_only", "start": 1, "end": 2}\n')
    with pytest.raises(ValueError, match="session"):
        read_key(path)


def test_an_unknown_line_kind_is_refused(tmp_path):
    path = tmp_path / "answer_key.jsonl"
    path.write_text(
        '{"kind": "session", "seed": "A1", "scored_start": 1, "soak_end": 2, "scored_end": 3}\n'
        '{"kind": "rumour"}\n'
    )
    with pytest.raises(ValueError, match="rumour"):
        read_key(path)
