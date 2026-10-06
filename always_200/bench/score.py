"""The scorer: incidents against an answer key (scoring spec sections 4-9, rulings P3 and P4).

`score_session` classifies every fault incident of one session exactly once, in open-time order:
- outside the scored span: **unscored**;
- in no fault window `[start, end + grace]`: a **false alarm** (and also one **in an event** when
  it opens in `[event_start, event_end + grace]`, P4);
- in a fault window with an alert of the fault's signal on the target or a service upstream of
  it: the **detection** if it is the first such incident, else a **duplicate**;
- otherwise, after the fault ended: a **recovery tail**; during the fault touching neither the
  target nor upstream: a **false alarm**; during the fault touching them with the wrong signal:
  **wrong signal**.
A fault missed because an incident opened before it and was still open at its start is also
counted as **swallowed** (spec section 4). Monitoring-gap incidents are never scored here (P2).

`summarise` pools several sessions: it sums counts and clean hours and concatenates latencies,
then attaches the intervals from `always_200.bench.stats`.
"""

from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from always_200.bench.answer_key import AnswerKey
from always_200.bench.stats import bootstrap_median_ci, poisson_rate_ci, wilson
from always_200.graph import Graph
from always_200.incidents import Incident, choose_culprit

SIGNAL_FOR_FAULT = {
    "latency_step": "latency_p95",
    "latency_ramp": "latency_p95",
    "errors": "error_ratio",
}
SIZES = ("subtle", "moderate", "severe")
INFO_KEYS = ("duplicate", "wrong_signal", "recovery_tail", "unscored", "swallowed")


@dataclass
class SessionScore:
    key: AnswerKey
    detected: dict[str, Incident]  # fault id -> detecting incident
    false_alarms: list[Incident]
    fa_in_events: list[Incident]
    info: Counter  # duplicate | wrong_signal | recovery_tail | unscored | swallowed
    clean_hours: float
    # fault id -> the comparison culprit: the most downstream service that alerted at any time
    # in the detecting incident (no settle window), earliest on ties
    downstream_any: dict[str, str] = field(default_factory=dict)


def union_length(intervals: list[tuple[float, float]]) -> float:
    """Total length covered by the intervals, counting overlaps once."""
    total = 0.0
    cur_start = cur_end = None
    for start, end in sorted(intervals):
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_end is not None:
        total += cur_end - cur_start
    return total


def clean_seconds(key: AnswerKey, grace_s: float) -> float:
    """Scored time minus every fault window plus grace, each clipped to the scored span."""
    lo, hi = key.scored_start, key.scored_end
    windows = [(max(f.start, lo), min(f.end + grace_s, hi)) for f in key.faults]
    return (hi - lo) - union_length([(a, b) for a, b in windows if b > a])


def score_session(
    incidents: list[Incident], key: AnswerKey, graph: Graph, grace_s: float = 90.0
) -> SessionScore:
    detected: dict[str, Incident] = {}
    false_alarms: list[Incident] = []
    fa_in_events: list[Incident] = []
    info: Counter = Counter({k: 0 for k in INFO_KEYS})

    for inc in sorted((i for i in incidents if i.kind == "fault"), key=lambda i: i.opened_at):
        if not (key.scored_start <= inc.opened_at <= key.scored_end):
            info["unscored"] += 1
            continue
        f = next((f for f in key.faults if f.start <= inc.opened_at <= f.end + grace_s), None)
        if f is None:
            false_alarms.append(inc)
            if any(e.start <= inc.opened_at <= e.end + grace_s for e in key.events):
                fa_in_events.append(inc)
            continue
        near = {f.target} | graph.upstream(f.target)
        relevant = any(
            a.service in near and a.signal == SIGNAL_FOR_FAULT[f.type] for a in inc.alerts
        )
        touches = any(a.service in near for a in inc.alerts)
        if relevant:
            if f.id in detected:
                info["duplicate"] += 1
            else:
                detected[f.id] = inc
        elif inc.opened_at > f.end:
            info["recovery_tail"] += 1
        elif not touches:
            false_alarms.append(inc)
        else:
            info["wrong_signal"] += 1

    for f in key.faults:  # a miss caused by an incident already open at fault start
        if f.id not in detected and any(
            i.kind == "fault"
            and i.opened_at < f.start
            and (i.closed_at is None or i.closed_at > f.start)
            for i in incidents
        ):
            info["swallowed"] += 1

    downstream_any = {
        fid: choose_culprit(inc, graph, settle_s=float("inf")) for fid, inc in detected.items()
    }
    return SessionScore(
        key,
        detected,
        false_alarms,
        fa_in_events,
        info,
        clean_seconds(key, grace_s) / 3600,
        downstream_any,
    )


CI = tuple[float, float, float] | None  # None = denominator 0, printed "n/a"


@dataclass(frozen=True)
class Summary:
    detection: CI  # wilson
    n_faults: int
    swallowed: int  # misses swallowed by an incident opened before the fault
    latency_median_ci: CI  # seconds
    latency_p90: float | None
    fa_rate_ci: tuple[float, float, float]  # per clean hour
    fa_in_events: int
    culprit: CI  # wilson over detected faults, the D7 rule (60 s settle)
    culprit_downstream_any: CI  # comparison: most downstream alerting service, whole incident
    by_size: dict[str, CI]
    by_type: dict[str, CI]
    info: dict[str, int]


def summarise(scores: list[SessionScore]) -> Summary:
    """Pools sessions: sums counts and clean hours, concatenates latencies."""
    faults = [(s, f) for s in scores for f in s.key.faults]
    hits = [(s, f) for s, f in faults if f.id in s.detected]
    latencies = [s.detected[f.id].opened_at - f.start for s, f in hits]
    culprit_hits = sum(1 for s, f in hits if s.detected[f.id].culprit == f.target)
    any_hits = sum(1 for s, f in hits if s.downstream_any.get(f.id) == f.target)

    def share(test) -> CI:
        group = [(s, f) for s, f in faults if test(f)]
        return wilson(sum(1 for s, f in group if f.id in s.detected), len(group))

    info: Counter = Counter({k: 0 for k in INFO_KEYS})
    for s in scores:
        info.update(s.info)
    return Summary(
        detection=wilson(len(hits), len(faults)),
        n_faults=len(faults),
        swallowed=info["swallowed"],
        latency_median_ci=bootstrap_median_ci(latencies),
        latency_p90=float(np.percentile(latencies, 90)) if latencies else None,
        fa_rate_ci=poisson_rate_ci(
            sum(len(s.false_alarms) for s in scores), sum(s.clean_hours for s in scores)
        ),
        fa_in_events=sum(len(s.fa_in_events) for s in scores),
        culprit=wilson(culprit_hits, len(hits)),
        culprit_downstream_any=wilson(any_hits, len(hits)),
        by_size={size: share(lambda f, size=size: f.size == size) for size in SIZES},
        by_type={t: share(lambda f, t=t: f.type == t) for t in SIGNAL_FOR_FAULT},
        info=dict(info),
    )
