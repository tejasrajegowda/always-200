"""Prometheus metrics for the traffic machine, served on port 9100.

These series are the answer key and the load counters. A detector is never allowed to read
them (decision D11). `event_active` exists for all three event kinds from import, at 0, so a
quiet session is a row of zeros rather than a missing series.
"""

from prometheus_client import Counter, Gauge

from traffic.schedule import EVENT_KINDS, SERVICES

METRICS_PORT = 9100

event_active = Gauge(
    "event_active",
    "1 while this non-incident event is running.",
    ["kind"],
)
# The constructed name is deploy_marker_total; Prometheus counters add the suffix.
deploy_marker_total = Counter(
    "deploy_marker",
    "Deploys announced by the traffic machine, by service.",
    ["service"],
)
session_phase = Gauge("session_phase", "0 warm-up, 1 soak, 2 slots, 3 done")
session_seed = Gauge(
    "session_seed",
    "1 while this seed's session runs",
    ["seed"],
)
traffic_offered_total = Counter(
    "traffic_offered",
    "Arrivals placed on the schedule, whether or not a request was sent.",
)
traffic_sent_total = Counter(
    "traffic_sent",
    "Buy attempts finished or refused, by outcome.",
    ["outcome"],
)

OUTCOMES = ("ok", "error", "timeout", "dropped")


def _init_series() -> None:
    for kind in EVENT_KINDS:
        event_active.labels(kind=kind).set(0)
    # inc(0) creates the series at zero without counting a deploy or a request.
    for service in SERVICES:
        deploy_marker_total.labels(service=service).inc(0)
    for outcome in OUTCOMES:
        traffic_sent_total.labels(outcome=outcome).inc(0)
    session_phase.set(0)


_init_series()
