"""The shared incident builder and the culprit rule (r2 §4.3, D7, rulings P1, P2, P8, P14).

A detector emits one `Anomaly` per out-of-band step. People are paged per incident, not per alert,
so this module groups alerts into incidents:

- one open incident per kind (`fault`, `monitoring_gap`), so a monitoring gap never mixes with a
  fault and fault scoring never sees it (P2);
- an alert joins the open incident of its kind; with none open, it opens a new one;
- the culprit is the most downstream service that alerted within `settle_s` of the opening,
  earliest first on a tie (D7), and it is final from then on;
- an incident closes once no alert has arrived for `close_after_s` of sample time (P1).

Time comes only from the alerts and from `tick(now)`, never from the wall clock (D5), so a replay
gives the same incidents as live. Every change is returned as an `IncidentEvent`, so later code
(tickets, step 23) can react to it without the builder knowing that code exists.
"""

from dataclasses import dataclass, field

from always_200.core import Anomaly
from always_200.graph import Graph


@dataclass
class Incident:
    id: str  # f"{kind}-{int(opened_at)}"
    kind: str  # "fault" | "monitoring_gap"
    opened_at: float
    last_alert_at: float
    alerts: list[Anomaly] = field(default_factory=list)
    culprit: str | None = None  # final at opened_at + settle_s
    closed_at: float | None = None


@dataclass(frozen=True)
class IncidentEvent:
    type: str  # "open" | "culprit" | "join" | "close"
    ts: float
    incident: Incident
    alert: Anomaly | None = None


def choose_culprit(incident: Incident, graph: Graph, settle_s: float) -> str:
    """The most downstream service alerting within the settle window; earliest wins a tie."""
    first_seen: dict[str, float] = {}
    for a in incident.alerts:
        if a.ts <= incident.opened_at + settle_s:
            first_seen.setdefault(a.service, a.ts)
    return max(first_seen, key=lambda s: (graph.depth(s), -first_seen[s]))


class IncidentBuilder:
    def __init__(self, graph: Graph, close_after_s: float = 180.0, settle_s: float = 60.0) -> None:
        self._graph = graph
        self._close_after_s = close_after_s
        self._settle_s = settle_s
        self._all: list[Incident] = []
        self._open: dict[str, Incident] = {}  # kind -> its one open incident

    @property
    def incidents(self) -> list[Incident]:
        """Every incident so far, open or closed, in the order they opened."""
        return list(self._all)

    def _settle(self, inc: Incident, now: float) -> IncidentEvent:
        inc.culprit = choose_culprit(inc, self._graph, self._settle_s)
        return IncidentEvent("culprit", now, inc)

    def tick(self, now: float) -> list[IncidentEvent]:
        """Settle culprits whose window has passed, then close incidents that went quiet."""
        events: list[IncidentEvent] = []
        for kind, inc in list(self._open.items()):
            closing = now - inc.last_alert_at >= self._close_after_s
            if inc.culprit is None and (now >= inc.opened_at + self._settle_s or closing):
                events.append(self._settle(inc, now))
            if closing:
                inc.closed_at = inc.last_alert_at + self._close_after_s
                del self._open[kind]
                events.append(IncidentEvent("close", now, inc))
        return events

    def feed(self, a: Anomaly) -> list[IncidentEvent]:
        """Advance the clock to the alert, then open a new incident or join the open one."""
        events = self.tick(a.ts)
        inc = self._open.get(a.kind)
        if inc is None:
            inc = Incident(f"{a.kind}-{int(a.ts)}", a.kind, a.ts, a.ts, [a])
            self._open[a.kind] = inc
            self._all.append(inc)
            events.append(IncidentEvent("open", a.ts, inc, a))
            return events
        is_new_pair = all((x.service, x.signal) != (a.service, a.signal) for x in inc.alerts)
        inc.alerts.append(a)
        inc.last_alert_at = max(inc.last_alert_at, a.ts)
        if is_new_pair:
            events.append(IncidentEvent("join", a.ts, inc, a))
        return events

    def finish(self, now: float) -> list[Incident]:
        """End of the stream: tick once more, settle any pending culprit, return all incidents."""
        self.tick(now)
        for inc in self._open.values():
            if inc.culprit is None:
                self._settle(inc, now)
        return self.incidents
