from collections import Counter
from itertools import pairwise

import pytest

from patient.fault import SIZES as PATIENT_SIZES
from patient.fault import TYPES as PATIENT_TYPES
from traffic.schedule import FAULT_TYPES, SERVICES, SIZES, build_session


def test_same_seed_same_plan():
    assert build_session("A1") == build_session("A1")


def test_different_seeds_differ():
    assert build_session("A1").slots != build_session("B1").slots


def test_counts_are_exactly_balanced():
    p = build_session("B2")
    faults = [s for s in p.slots if s.kind == "fault"]
    events = [s for s in p.slots if s.kind == "event"]
    assert len(faults) == 36 and len(events) == 12
    assert set(Counter(s.size for s in faults).values()) == {12}
    assert set(Counter(s.fault_type for s in faults).values()) == {12}
    assert set(Counter(s.event_kind for s in events).values()) == {4}


def test_durations_and_cooldowns():
    p = build_session("A1")
    for s in p.slots:
        lo, hi = (120, 300) if s.kind == "fault" else (120, 240)
        assert lo <= s.duration_s <= hi
    for a, b in pairwise(p.slots):
        assert abs(b.start_s - (a.start_s + a.duration_s + 360)) < 1e-6  # never overlapping


def test_deploy_markers_sit_in_the_previous_cooldown_after_grace():
    p = build_session("A1")
    for prev, s in pairwise(p.slots):
        if s.deploy_marker_s is not None:
            assert prev.start_s + prev.duration_s + 90 < s.deploy_marker_s < s.start_s


def test_session_length_is_about_8_8_hours():
    assert 8.0 * 3600 < build_session("A1").total_s < 9.6 * 3600


def test_quick_mode_is_three_severe_faults_one_per_target():
    p = build_session("demo", quick=True)
    assert sorted(s.target for s in p.slots) == ["orders", "payments", "storefront"]
    assert {s.size for s in p.slots} == {"severe"}


# Extra checks beyond the plan's list.


def test_slots_start_after_warmup_and_soak_and_are_indexed_in_order():
    p = build_session("A1")
    assert (p.warmup_s, p.soak_s, p.cooldown_s) == (600, 3600, 360)
    assert p.slots[0].start_s == 600 + 3600
    assert [s.index for s in p.slots] == list(range(48))


def test_faults_and_events_carry_only_their_own_fields():
    for s in build_session("B3").slots:
        if s.kind == "fault":
            assert s.target in SERVICES and s.event_kind is None
        else:
            assert s.target is s.fault_type is s.size is s.deploy_marker_s is None
            assert s.event_kind is not None


def test_about_half_of_the_faults_have_a_deploy_marker():
    for seed in ("A1", "B1", "B2", "B3"):
        faults = [s for s in build_session(seed).slots if s.kind == "fault"]
        with_marker = sum(s.deploy_marker_s is not None for s in faults)
        assert 8 <= with_marker <= 28  # 18 expected; this band holds for every real seed


def test_quick_mode_types_in_order_and_soak_override():
    p = build_session("demo", quick=True, soak_s=60)
    assert [s.fault_type for s in p.slots] == ["latency_step", "latency_ramp", "errors"]
    assert p.soak_s == 60 and p.slots[0].start_s == 600 + 60
    assert build_session("demo", quick=True).soak_s == 300
    with pytest.raises(ValueError):
        build_session("demo", quick=True, soak_s=-1)


def test_the_menu_matches_the_patients_sick_switch():
    assert set(FAULT_TYPES) == set(PATIENT_TYPES)
    assert set(SIZES) == set(PATIENT_SIZES)
