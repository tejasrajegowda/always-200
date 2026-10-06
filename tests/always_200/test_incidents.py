import dataclasses

import pytest

from always_200.core import Anomaly
from always_200.graph import Graph
from always_200.incidents import IncidentBuilder, choose_culprit

G = Graph({"storefront": ("orders",), "orders": ("payments",), "payments": ()})


def A(ts, svc, sig="latency_p95", kind="fault", first=False):
    return Anomaly(ts, svc, sig, kind, 1.0, 0.5, 0.1, first)


# The plan's tests (step 9.1).


def test_most_downstream_wins():
    b = IncidentBuilder(G)
    for ev in [A(0, "storefront"), A(15, "orders"), A(30, "payments")]:
        b.feed(ev)
    b.tick(60)
    assert b.incidents[0].culprit == "payments"


def test_alert_after_settle_window_cannot_change_culprit():
    b = IncidentBuilder(G)
    b.feed(A(0, "storefront"))
    b.feed(A(75, "payments"))
    b.tick(90)
    assert b.incidents[0].culprit == "storefront"


def test_depth_tie_breaks_by_earliest():
    d = Graph({"a": ("b", "c"), "b": (), "c": ()})
    b = IncidentBuilder(d)
    b.feed(A(0, "a"))
    b.feed(A(30, "c"))
    b.feed(A(15, "b"))
    b.tick(60)
    assert b.incidents[0].culprit == "b"


def test_closes_after_three_quiet_minutes_and_reopens_as_new():
    b = IncidentBuilder(G)
    b.feed(A(0, "orders"))
    events = b.tick(180)
    assert [e.type for e in events] == ["culprit", "close"]
    b.feed(A(200, "orders"))
    assert len(b.incidents) == 2 and b.incidents[1].id != b.incidents[0].id


def test_join_only_for_new_service_signal_pairs():
    b = IncidentBuilder(G)
    evs = b.feed(A(0, "orders")) + b.feed(A(15, "orders")) + b.feed(A(30, "payments"))
    assert [e.type for e in evs] == ["open", "join"]


def test_monitoring_gap_is_a_separate_incident_stream():
    b = IncidentBuilder(G)
    b.feed(A(0, "orders"))
    b.feed(A(10, "payments", "telemetry_missing", kind="monitoring_gap"))
    assert sorted(i.kind for i in b.incidents) == ["fault", "monitoring_gap"]


def test_continuous_alerting_keeps_the_incident_open():
    b = IncidentBuilder(G)
    for t in range(0, 600, 15):
        b.feed(A(t, "orders"))
    assert b.incidents[0].closed_at is None


# Extra tests: the remaining rules of the step text, pinned one by one.


def test_incident_id_and_fields_on_open():
    b = IncidentBuilder(G)
    (ev,) = b.feed(A(120.7, "orders"))
    inc = ev.incident
    assert ev.type == "open" and ev.ts == 120.7 and ev.alert == A(120.7, "orders")
    assert inc.id == "fault-120" and inc.kind == "fault"
    assert inc.opened_at == inc.last_alert_at == 120.7
    assert inc.culprit is None and inc.closed_at is None


def test_closed_at_is_last_alert_plus_close_after_not_the_tick_time():
    b = IncidentBuilder(G)
    b.feed(A(0, "orders"))
    b.feed(A(30, "orders"))
    b.tick(500)  # a late tick (for example the next alert came much later)
    assert b.incidents[0].closed_at == 30 + 180


def test_one_short_of_three_minutes_stays_open():
    b = IncidentBuilder(G)
    b.feed(A(0, "orders"))
    assert [e.type for e in b.tick(179)] == ["culprit"]
    assert b.incidents[0].closed_at is None


def test_culprit_settles_exactly_at_the_settle_window_and_only_once():
    b = IncidentBuilder(G)
    b.feed(A(0, "orders"))
    assert b.tick(59) == []
    events = b.tick(60)
    assert [(e.type, e.ts) for e in events] == [("culprit", 60)]
    assert b.tick(75) == []


def test_an_alert_that_closes_the_old_incident_opens_a_new_one_in_one_call():
    b = IncidentBuilder(G)
    b.feed(A(0, "orders"))
    events = b.feed(A(200, "payments"))
    assert [e.type for e in events] == ["culprit", "close", "open"]
    assert events[2].incident is b.incidents[1]


def test_repeated_alerts_are_kept_but_do_not_join():
    b = IncidentBuilder(G)
    b.feed(A(0, "orders"))
    assert b.feed(A(15, "orders")) == []
    assert b.feed(A(30, "orders", "error_ratio"))[0].type == "join"
    inc = b.incidents[0]
    assert len(inc.alerts) == 3 and inc.last_alert_at == 30


def test_kinds_have_their_own_clocks():
    b = IncidentBuilder(G)
    b.feed(A(0, "orders"))
    b.feed(A(100, "payments", "telemetry_missing", kind="monitoring_gap"))
    events = b.tick(200)
    closed = [e.incident.kind for e in events if e.type == "close"]
    assert closed == ["fault"]


def test_finish_settles_a_pending_culprit_and_returns_everything():
    b = IncidentBuilder(G)
    b.feed(A(0, "storefront"))
    b.feed(A(15, "orders"))
    incidents = b.finish(20)
    assert len(incidents) == 1
    assert incidents[0].culprit == "orders" and incidents[0].closed_at is None


def test_finish_closes_quiet_incidents_by_sample_time():
    b = IncidentBuilder(G)
    b.feed(A(0, "orders"))
    (inc,) = b.finish(400)
    assert inc.culprit == "orders" and inc.closed_at == 180


def test_choose_culprit_ignores_alerts_after_the_window():
    b = IncidentBuilder(G)
    b.feed(A(0, "storefront"))
    inc = b.incidents[0]
    late = dataclasses.replace(inc, alerts=[*inc.alerts, A(61, "payments")])
    assert choose_culprit(late, G, 60.0) == "storefront"


def test_an_unknown_service_is_refused_by_the_graph():
    b = IncidentBuilder(G)
    b.feed(A(0, "mystery"))
    with pytest.raises(KeyError):
        b.tick(60)


def test_a_close_before_settle_still_reports_the_culprit_first():
    b = IncidentBuilder(G, close_after_s=30.0, settle_s=60.0)
    b.feed(A(0, "orders"))
    events = b.tick(45)
    assert [e.type for e in events] == ["culprit", "close"]
    assert b.incidents[0].culprit == "orders"
