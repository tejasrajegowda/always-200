"""The scorer, pinned branch by branch (step 10.2, ruling P3, rulings P1b and P4).

One hand-built answer key serves every test: scored span 0-3600 s, three faults (payments
latency 600-780 s, orders errors 1500-1680 s, storefront latency 2400-2580 s) and one
traffic-spike event 3000-3150 s. Grace is 90 s.
"""

from always_200.bench.answer_key import AnswerKey, Event, Fault
from always_200.bench.score import SIGNAL_FOR_FAULT, score_session, summarise
from always_200.core import Anomaly, Sample
from always_200.detect.trivial import AlwaysDetector, NeverDetector
from always_200.graph import Graph
from always_200.incidents import Incident, IncidentBuilder
from always_200.pipeline import run_detector

G = Graph({"storefront": ("orders",), "orders": ("payments",), "payments": ()})

F_PAY = Fault("T1-f01", "latency_step", "payments", "subtle", 600.0, 780.0, True)
F_ORD = Fault("T1-f02", "errors", "orders", "moderate", 1500.0, 1680.0, True)
F_SHOP = Fault("T1-f03", "latency_ramp", "storefront", "severe", 2400.0, 2580.0, True)
KEY = AnswerKey(
    "T1",
    scored_start=0.0,
    soak_end=300.0,
    scored_end=3600.0,
    faults=(F_PAY, F_ORD, F_SHOP),
    events=(Event("traffic_spike", 3000.0, 3150.0),),
)


def A(ts, svc, sig="latency_p95", kind="fault"):
    return Anomaly(ts, svc, sig, kind, 1.0, 0.5, 0.1, False)


def inc(opened_at, *alerts, closed_at=None, culprit=None, kind="fault"):
    """An incident built by hand; alerts are (ts, service, signal) triples."""
    anomalies = [A(ts, svc, sig, kind) for ts, svc, sig in alerts]
    last = max((a.ts for a in anomalies), default=opened_at)
    return Incident(
        f"{kind}-{int(opened_at)}",
        kind,
        opened_at,
        last,
        anomalies,
        culprit or (anomalies[0].service if anomalies else None),
        closed_at,
    )


def built(*alerts):
    """Incidents from the real builder (so the culprit is the real D7 one)."""
    b = IncidentBuilder(G)
    for ts, svc, sig in alerts:
        b.feed(A(ts, svc, sig))
    return b.finish(max(ts for ts, _, _ in alerts) + 600)


def stream(start=-600.0, end=3600.0, step=15.0):
    """Every service and signal once per step, in ruling P7 order (no telemetry_missing)."""
    ts = start
    while ts < end:
        for svc in ("storefront", "orders", "payments"):
            for sig in ("request_rate", "latency_p95", "error_ratio"):
                yield Sample(ts, svc, sig, 1.0)
        ts += step


# The plan's tests (step 10.2).


def test_never_detector_detects_nothing_and_alarms_never():
    incidents = run_detector(stream(), NeverDetector(), IncidentBuilder(G), KEY.scored_start)
    s = score_session(incidents, KEY, G)
    assert incidents == [] and s.detected == {} and s.false_alarms == []
    summary = summarise([s])
    assert summary.detection[0] == 0.0 and summary.n_faults == 3
    assert summary.fa_rate_ci[0] == 0.0


def test_always_detector_opens_one_incident_that_swallows_everything():
    # warm-up samples reach the detector but not the builder (P1b): the incident opens at the
    # first scored sample, before every fault -> 3 misses, all 3 swallowed, 1 false alarm
    incidents = run_detector(stream(), AlwaysDetector(), IncidentBuilder(G), KEY.scored_start)
    assert len(incidents) == 1 and incidents[0].opened_at == 0.0
    s = score_session(incidents, KEY, G)
    assert s.detected == {}
    assert s.info["swallowed"] == 3
    assert s.false_alarms == incidents and s.fa_in_events == []


def test_warmup_samples_reach_the_detector_but_never_open_an_incident():  # P1b
    seen: list[float] = []

    class Spy(AlwaysDetector):
        def update(self, sample):
            seen.append(sample.ts)
            return super().update(sample)

    incidents = run_detector(stream(), Spy(), IncidentBuilder(G), scored_start=0.0)
    assert min(seen) == -600.0  # the detector was fed warm-up
    assert all(i.opened_at >= 0.0 for i in incidents)
    # Without the scored_start filter the same stream opens its incident inside warm-up.
    unfiltered = run_detector(stream(), AlwaysDetector(), IncidentBuilder(G))
    assert unfiltered[0].opened_at == -600.0


def test_swallowed_counts_a_fault_inside_an_incident_opened_before_it():
    early = inc(500.0, (500.0, "payments", "latency_p95"), closed_at=900.0)
    s = score_session([early], KEY, G)
    assert F_PAY.id not in s.detected
    assert s.info["swallowed"] == 1
    assert s.false_alarms == [early]  # it opened in clean time


def test_detection_needs_right_signal_on_target_or_upstream():
    # storefront latency alert counts for a payments latency fault: storefront is upstream
    hit = inc(630.0, (630.0, "storefront", "latency_p95"), closed_at=900.0)
    s = score_session([hit], KEY, G)
    assert s.detected == {F_PAY.id: hit} and s.false_alarms == []


def test_downstream_only_alert_does_not_detect_upstream_fault():
    # payments alert vs storefront fault -> FA (touches neither the target nor upstream)
    down = inc(2450.0, (2450.0, "payments", "latency_p95"), closed_at=2700.0)
    s = score_session([down], KEY, G)
    assert s.detected == {} and s.false_alarms == [down]


def test_wrong_signal_is_info_not_detection_not_fa():
    wrong = inc(630.0, (630.0, "payments", "error_ratio"), closed_at=900.0)
    s = score_session([wrong], KEY, G)
    assert s.detected == {} and s.false_alarms == []
    assert s.info["wrong_signal"] == 1


def test_incident_inside_grace_after_end_is_a_late_detection():
    late = inc(840.0, (840.0, "payments", "latency_p95"), closed_at=1100.0)  # end + 60 s
    s = score_session([late], KEY, G)
    assert s.detected == {F_PAY.id: late}


def test_recovery_tail_is_ignored():
    first = inc(630.0, (630.0, "payments", "latency_p95"), closed_at=790.0)
    tail = inc(800.0, (800.0, "orders", "error_ratio"), closed_at=1000.0)  # in (end, end+grace]
    elsewhere = inc(850.0, (850.0, "storefront", "error_ratio"), closed_at=1050.0)
    s = score_session([first, tail, elsewhere], KEY, G)
    assert s.detected == {F_PAY.id: first}
    assert s.info["recovery_tail"] == 2 and s.false_alarms == []


def test_fa_during_event_is_counted_twice_over():
    during = inc(3100.0, (3100.0, "storefront", "latency_p95"), closed_at=3300.0)
    s = score_session([during], KEY, G)
    assert s.false_alarms == [during] and s.fa_in_events == [during]


def test_clean_hours_exclude_fault_windows_plus_grace():
    # 3600 - 3 * (180 + 90) = 2790 s
    s = score_session([], KEY, G)
    assert abs(s.clean_hours - 2790 / 3600) < 1e-12


def test_culprit_hit_uses_final_culprit():
    hit = built((620.0, "storefront", "latency_p95"), (630.0, "payments", "latency_p95"))
    miss = built((1520.0, "storefront", "error_ratio"), (1600.0, "orders", "error_ratio"))
    assert hit[0].culprit == "payments" and miss[0].culprit == "storefront"
    summary = summarise([score_session(hit + miss, KEY, G)])
    assert summary.culprit[0] == 0.5  # 1 hit out of 2 detected faults


def test_comparison_culprit_uses_all_alerts_without_settle_window():
    # orders alerts 100 s after the opening: too late for the D7 culprit, counted by the
    # comparison row, which looks at every alert of the incident
    late = built((1520.0, "storefront", "error_ratio"), (1620.0, "orders", "error_ratio"))
    s = score_session(late, KEY, G)
    assert s.downstream_any == {F_ORD.id: "orders"}
    summary = summarise([s])
    assert summary.culprit[0] == 0.0 and summary.culprit_downstream_any[0] == 1.0


def test_latency_is_open_time_minus_fault_start():
    hits = [
        inc(645.0, (645.0, "payments", "latency_p95"), closed_at=900.0),
        inc(1530.0, (1530.0, "orders", "error_ratio"), closed_at=1800.0),
    ]
    summary = summarise([score_session(hits, KEY, G)])
    assert summary.latency_median_ci[0] == 37.5  # median of 45 s and 30 s
    assert summary.detection[0] == 2 / 3


# Extra tests for the remaining rules of the step text.


def test_signal_for_fault_covers_every_fault_type():
    assert set(SIGNAL_FOR_FAULT) == {"latency_step", "latency_ramp", "errors"}


def test_a_second_relevant_incident_is_a_duplicate():
    first = inc(630.0, (630.0, "payments", "latency_p95"), closed_at=650.0)
    second = inc(700.0, (700.0, "orders", "latency_p95"), closed_at=900.0)
    s = score_session([second, first], KEY, G)  # order of the input list does not matter
    assert s.detected == {F_PAY.id: first} and s.info["duplicate"] == 1


def test_incident_touching_neither_during_the_fault_is_a_false_alarm_not_in_events():
    other = inc(650.0, (650.0, "payments", "error_ratio"), closed_at=900.0)
    s = score_session([other], KEY, G)
    assert s.info["wrong_signal"] == 1  # payments is the target: it touches, wrong signal
    lost = inc(2450.0, (2450.0, "orders", "latency_p95"), closed_at=2700.0)
    s = score_session([lost], KEY, G)  # orders is downstream of the storefront target
    assert s.false_alarms == [lost] and s.fa_in_events == []


def test_fa_in_event_grace_and_after_it():
    in_grace = inc(3210.0, (3210.0, "orders", "latency_p95"), closed_at=3400.0)
    after = inc(3400.0, (3400.0, "orders", "latency_p95"), closed_at=3590.0)
    s = score_session([in_grace, after], KEY, G)
    assert s.false_alarms == [in_grace, after] and s.fa_in_events == [in_grace]


def test_incidents_outside_the_scored_span_are_unscored_and_gaps_are_ignored():
    outside = inc(3700.0, (3700.0, "orders", "latency_p95"))
    gap = inc(630.0, (630.0, "payments", "telemetry_missing"), kind="monitoring_gap")
    s = score_session([outside, gap], KEY, G)
    assert s.info["unscored"] == 1 and s.false_alarms == [] and s.detected == {}
    assert s.info["wrong_signal"] == 0  # the monitoring gap never reached fault scoring


def test_an_incident_closed_at_or_before_the_fault_start_swallows_nothing():
    gone = inc(300.0, (300.0, "payments", "latency_p95"), closed_at=600.0)
    s = score_session([gone], KEY, G)
    assert s.info["swallowed"] == 0


def test_clean_time_clips_windows_to_the_scored_span_and_merges_overlaps():
    edge = Fault("T2-f01", "errors", "orders", "subtle", 3500.0, 3600.0, False)
    a = Fault("T2-f02", "errors", "orders", "subtle", 100.0, 200.0, True)
    b = Fault("T2-f03", "errors", "orders", "subtle", 250.0, 300.0, True)  # inside a's grace
    key = AnswerKey("T2", 0.0, 50.0, 3600.0, (a, b, edge), ())
    s = score_session([], key, G)
    # a + grace = 100-290, b + grace = 250-390 -> union 100-390 (290 s); edge clipped: 100 s
    assert abs(s.clean_hours - (3600 - 290 - 100) / 3600) < 1e-12


def test_summarise_pools_sessions_and_splits_by_size_and_type():
    one = score_session([inc(645.0, (645.0, "payments", "latency_p95"), closed_at=900.0)], KEY, G)
    two = score_session([inc(100.0, (100.0, "orders", "latency_p95"), closed_at=300.0)], KEY, G)
    summary = summarise([one, two])
    assert summary.n_faults == 6
    assert summary.detection[0] == 1 / 6
    assert summary.fa_rate_ci[0] == 1 / (2 * 2790 / 3600)  # 1 FA over both sessions' clean hours
    assert summary.by_size["subtle"][0] == 0.5 and summary.by_size["severe"][0] == 0.0
    assert summary.by_type["latency_step"][0] == 0.5 and summary.by_type["errors"][0] == 0.0
    assert set(summary.info) == {
        "duplicate",
        "wrong_signal",
        "recovery_tail",
        "unscored",
        "swallowed",
    }


def test_summarise_with_no_detections_has_no_latency_and_no_culprit():
    summary = summarise([score_session([], KEY, G)])
    assert summary.latency_median_ci is None and summary.latency_p90 is None
    assert summary.culprit is None and summary.culprit_downstream_any is None
