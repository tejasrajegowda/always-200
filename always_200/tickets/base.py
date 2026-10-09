"""What every ticket sink agrees on (D14), plus the ticket text both sinks share.

A sink is anything with `open`, `comment` and `resolve`. `TicketFlow` (flow.py) talks only to
this interface, so the default `JsonlSink` and the later Jira sink are swapped without the flow,
the incident builder or the bench knowing which one is in use.

The ticket text is built here, not in a sink, so a ticket reads the same whichever sink files it:
- `title`: "[always-200] payments: latency p95 high", or "[always-200] orders: monitoring gap";
- `opening_body`: the write-up, then every alert that joined before the ticket opened (ruling P14
  folds the pre-settle joins into the body), then the evidence rows and where the write-up came
  from;
- `comment_text`: one line for an alert that joined after the ticket opened;
- `resolve_text`: one line for the close.

`SampleClock` holds the time of the event being handled, in sample seconds. A sink that writes a
timestamp reads it, so a replay writes sample time, never the wall clock (D5).
"""

from typing import Protocol

from always_200.core import Anomaly
from always_200.incidents import Incident
from always_200.writeup.writeup import WriteUp

PRODUCT = "always-200"
PROBLEMS = {
    "latency_p95": "latency p95 high",
    "error_ratio": "error ratio high",
    "telemetry_missing": "monitoring gap",
}


class TicketSink(Protocol):
    def open(self, incident: Incident, writeup: WriteUp, assignee: str) -> str: ...  # a key

    def comment(self, key: str, incident: Incident, text: str) -> None: ...

    def resolve(self, key: str, incident: Incident) -> None: ...


class SampleClock:
    """The current time in sample seconds; `TicketFlow` moves it to each event's timestamp."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _service(incident: Incident) -> str:
    return incident.culprit or incident.alerts[0].service


def _offset(a: Anomaly, incident: Incident) -> str:
    return f"+{a.ts - incident.opened_at:.0f} s"


def title(incident: Incident) -> str:
    """The culprit and the problem: its first alerting signal, or "monitoring gap"."""
    service = _service(incident)
    if incident.kind == "monitoring_gap":
        return f"[{PRODUCT}] {service}: {PROBLEMS['telemetry_missing']}"
    signal = next((a.signal for a in incident.alerts if a.service == service), None)
    return f"[{PRODUCT}] {service}: {PROBLEMS[signal or incident.alerts[0].signal]}"


def opening_body(incident: Incident, writeup: WriteUp) -> str:
    """The write-up, the alerts so far (each service and signal once, at its first alert), the
    evidence rows and the write-up's source."""
    lines = [writeup.data["summary"], "", f"First check: {writeup.data['first_check']}", ""]
    lines.append("Alerts before this ticket opened (time after the incident opened):")
    first: dict[tuple[str, str], Anomaly] = {}
    for a in incident.alerts:
        first.setdefault((a.service, a.signal), a)
    lines += [f"- {a.service} {a.signal} at {_offset(a, incident)}" for a in first.values()]
    if writeup.data["evidence"]:
        lines += ["", "Evidence (minute relative to the incident start):"]
        lines += [
            f"- {r['service']} {r['signal']} at t{r['ts']:+d}: {r['value']:g}"
            for r in writeup.data["evidence"]
        ]
    lines += ["", f"Write-up: {writeup.source}"]
    return "\n".join(lines)


def comment_text(incident: Incident, alert: Anomaly) -> str:
    return f"Also alerting: {alert.service} {alert.signal} at {_offset(alert, incident)}."


def resolve_text(incident: Incident) -> str:
    closed = incident.closed_at if incident.closed_at is not None else incident.last_alert_at
    return (
        f"Resolved: no alert since +{incident.last_alert_at - incident.opened_at:.0f} s; "
        f"closed at +{closed - incident.opened_at:.0f} s."
    )
