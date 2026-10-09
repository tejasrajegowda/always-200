"""Turns incident events into ticket actions: open, comment, resolve (D14, ruling P14).

De-duplication is by incident state, not by time buckets: one incident is one ticket. The builder
already decides what an incident is, so the flow only follows its events:

- `open`: nothing yet. The culprit is not final, and a ticket must name its assignee.
- `culprit`: the culprit is final (end of the 60 s settle window). The flow asks the writer for a
  write-up and opens the ticket, assigned to `owners(culprit)`. Every alert that arrived before
  this moment is in the opening body (P14), so a pre-settle join never becomes a comment.
- `join` after the ticket opened: one comment. A join before it does nothing; the body has it.
- `close`: resolve the ticket. A later alert opens a new incident, and so a new ticket.

The assignee comes only from the incident's culprit, never from the write-up: a model that
suspects another service changes the note, never who is paged (D7, D12). A monitoring-gap incident
never asks the writer: it opens a "monitoring gap" ticket from the template's own text.

Time to ticket (r2 §4.5) is recorded per ticket as three parts: the settle wait (sample seconds
from the incident opening to the final culprit), the write-up's seconds and the sink's seconds.
It is reported apart from detection latency.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from always_200.incidents import Incident, IncidentEvent
from always_200.tickets.base import SampleClock, TicketSink, comment_text, title
from always_200.writeup.template import gap_prose
from always_200.writeup.writeup import WriteUp

__all__ = ["TicketFlow", "TicketTiming", "gap_writeup", "placeholder_owner", "title"]


def placeholder_owner(service: str) -> str:
    """The bench and CI assignee; real account ids arrive with the owners file (step 24)."""
    return f"owner:{service}"


def gap_writeup(incident: Incident) -> WriteUp:
    """The template note of a monitoring gap. It is no model output and is never scored, so it
    carries no evidence rows: the body lists the missing-telemetry alerts instead."""
    service = incident.culprit or incident.alerts[0].service
    summary, first_check = gap_prose(service)
    data = {
        "summary": summary,
        "suspected_service": service,
        "evidence": [],
        "first_check": first_check,
    }
    return WriteUp("template", data, 0.0, None)


@dataclass(frozen=True)
class TicketTiming:
    incident: str
    kind: str  # "fault" | "monitoring_gap"
    settle_s: float  # sample seconds from the incident opening to the final culprit
    writeup_s: float
    sink_s: float

    @property
    def total_s(self) -> float:
        return self.settle_s + self.writeup_s + self.sink_s


class TicketFlow:
    def __init__(
        self,
        sink: TicketSink,
        writer: Callable[[Incident], WriteUp],
        owners: Callable[[str], str],
        clock: SampleClock | None = None,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sink = sink
        self._writer = writer
        self._owners = owners
        self._clock = clock
        self._timer = timer
        self._keys: dict[str, str] = {}  # incident id -> the key of its open ticket
        self.timings: list[TicketTiming] = []

    def _open(self, ev: IncidentEvent) -> None:
        inc = ev.incident
        if inc.id in self._keys or inc.culprit is None:
            return
        # The incident as it stood when the culprit became final: an alert fed at this same
        # timestamp arrived after that decision, so it is a comment, not part of the body.
        snapshot = replace(inc, alerts=[a for a in inc.alerts if a.ts < ev.ts])
        snapshot = snapshot if snapshot.alerts else replace(inc, alerts=inc.alerts[:1])
        writeup = gap_writeup(inc) if inc.kind == "monitoring_gap" else self._writer(snapshot)
        start = self._timer()
        self._keys[inc.id] = self._sink.open(snapshot, writeup, self._owners(inc.culprit))
        sink_s = self._timer() - start
        self.timings.append(
            TicketTiming(inc.id, inc.kind, ev.ts - inc.opened_at, writeup.seconds, sink_s)
        )

    def handle(self, events: list[IncidentEvent]) -> None:
        """culprit -> write-up + open (pre-settle joins folded into the body); join after open ->
        comment; close -> resolve. Monitoring-gap incidents open a 'monitoring gap' ticket from
        the template."""
        for ev in events:
            if self._clock is not None:
                self._clock.now = ev.ts
            key = self._keys.get(ev.incident.id)
            if ev.type == "culprit":
                self._open(ev)
            elif ev.type == "join" and key is not None and ev.alert is not None:
                self._sink.comment(key, ev.incident, comment_text(ev.incident, ev.alert))
            elif ev.type == "close" and key is not None:
                self._sink.resolve(key, ev.incident)
                del self._keys[ev.incident.id]
