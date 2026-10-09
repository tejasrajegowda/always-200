"""The evidence the model reads, built to ruling P17 (r2 section 9).

For an incident opening at t+0, the context holds:
- the dependency graph, as plain sentences;
- one table of every detector signal for ALL three services, minute by minute from t-10 to t+0
  (the last sample in each minute), with latency as whole milliseconds, errors as % with one
  decimal and request rate as req/s with one decimal;
- the minutes where a service's telemetry was missing;
- the deploy markers of the last 15 minutes, as relative minutes;
- the runbooks.

It never says which service the incident builder blamed: the model is asked for its own suspect,
and the culprit-agreement score (scores.py) compares the two. A hint would make that score
meaningless.

`Context.values` holds every number printed in the table's value column, parsed back from the
printed text, and nothing else. Minute labels, timestamps and runbook digits are never in it, so
they can never "ground" a number in a write-up (scores.grounded).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from always_200.core import SERVICES, Sample
from always_200.graph import Graph
from always_200.incidents import Incident

EVIDENCE_SIGNALS = ("request_rate", "latency_p95", "error_ratio")  # the schema's signal enum
UNITS = {"request_rate": "req/s", "latency_p95": "ms", "error_ratio": "%"}
MINUTES = range(-10, 1)  # t-10 .. t+0
DEPLOY_LOOKBACK_S = 900.0


@dataclass(frozen=True)
class Context:
    text: str  # the prompt context
    values: tuple[float, ...]  # every number printed in the metric-value column, as printed (P17)
    samples: tuple[Sample, ...] = ()  # the sample behind each printed cell, in table order


def fmt_value(signal: str, value: float) -> str:
    """A signal value as ruling P17 prints it."""
    if signal == "latency_p95":
        return str(round(value * 1000))  # seconds -> whole milliseconds
    if signal == "error_ratio":
        return f"{value * 100:.1f}"  # share -> % with one decimal
    if signal == "request_rate":
        return f"{value:.1f}"
    raise ValueError(f"{signal!r} is not an evidence signal")


def minute_label(minute: int) -> str:
    return f"t{minute:+d}"  # t-10, t-1, t+0


def relative_minute(ts: float, t0: float) -> int:
    return round((ts - t0) / 60)


def _last_in_minute(samples: list[Sample], t0: float, minute: int) -> Sample | None:
    """The last sample in the minute ending at t0 + 60 * minute."""
    end = t0 + 60 * minute
    inside = [s for s in samples if end - 60 < s.ts <= end]
    return max(inside, key=lambda s: s.ts) if inside else None


def load_runbooks(folder: Path) -> dict[str, str]:
    """One `<service>.md` per known service; a missing file is simply left out."""
    return {
        svc: (folder / f"{svc}.md").read_text(encoding="utf-8").strip()
        for svc in SERVICES
        if (folder / f"{svc}.md").is_file()
    }


def build_context(
    incident: Incident,
    samples: Sequence[Sample],
    deploys: Sequence[dict],
    graph: Graph,
    runbooks: dict[str, str],
) -> Context:
    """Plain text: the graph; 10-minute evidence windows (t-10 .. t+0, 1-minute resolution) of all
    signals for ALL three services, as a table whose value column holds latency as integer ms,
    errors as % with 1 decimal and request rate as req/s with 1 decimal; deploy markers of the last
    15 minutes as relative minutes; the runbooks. Never names the culprit. `values` collects only
    the value-column numbers: minute labels, timestamps and runbook digits are never in it."""
    t0 = incident.opened_at
    lo = t0 + 60 * (MINUTES.start - 1)
    window = [s for s in samples if lo < s.ts <= t0]

    lines = [
        "An incident opened at t+0. Minutes are relative to it: t-10 is ten minutes earlier.",
        "",
    ]
    lines.append("Service calls:")
    for svc, callees in graph.calls.items():
        lines.append(f"- {svc} calls {', '.join(callees)}" if callees else f"- {svc} calls nothing")

    lines += ["", "Evidence (the last sample of each minute; '-' means no sample):", ""]
    lines += ["| minute | service | signal | value | unit |", "|---|---|---|---|---|"]
    values: list[float] = []
    cells: list[Sample] = []
    for svc in SERVICES:
        for sig in EVIDENCE_SIGNALS:
            series = [s for s in window if s.service == svc and s.signal == sig]
            for m in MINUTES:
                s = _last_in_minute(series, t0, m)
                shown = "-" if s is None else fmt_value(sig, s.value)
                if s is not None:
                    values.append(float(shown))
                    cells.append(s)
                lines.append(f"| {minute_label(m)} | {svc} | {sig} | {shown} | {UNITS[sig]} |")

    lines += ["", "Telemetry missing (the service could not be scraped):"]
    missing_any = False
    for svc in SERVICES:
        series = [s for s in window if s.service == svc and s.signal == "telemetry_missing"]
        gone = [m for m in MINUTES if (s := _last_in_minute(series, t0, m)) and s.value >= 0.5]
        if gone:
            missing_any = True
            lines.append(f"- {svc} at {', '.join(minute_label(m) for m in gone)}")
    if not missing_any:
        lines.append("- none")

    lines += ["", "Deploys in the last fifteen minutes:"]
    recent = sorted(
        (d for d in deploys if t0 - DEPLOY_LOOKBACK_S <= d["ts"] <= t0), key=lambda d: d["ts"]
    )
    lines += [f"- {d['service']} at {minute_label(relative_minute(d['ts'], t0))}" for d in recent]
    if not recent:
        lines.append("- none")

    lines += ["", "Runbooks:"]
    for svc in SERVICES:
        if svc in runbooks:
            lines += ["", f"## {svc}", runbooks[svc]]

    return Context("\n".join(lines) + "\n", tuple(dict.fromkeys(values)), tuple(cells))
