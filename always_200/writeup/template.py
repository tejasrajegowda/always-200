"""The template write-up: the fallback whenever the model is absent, slow or wrong (D12).

It fills the same schema as the model, from the incident builder's own decision and the context's
own evidence cells, so it is valid and grounded by construction:
- `suspected_service` is the incident's culprit (the builder decided it; the template only says it);
- every number in the prose is a context cell printed with the same P17 format as the table, so
  it is always one of `Context.values`;
- the prose spells out its times ("at the incident start"), so no minute label or other digit
  sneaks in.
"""

from collections.abc import Sequence

from always_200.core import Anomaly, Sample
from always_200.incidents import Incident
from always_200.writeup.context import EVIDENCE_SIGNALS, UNITS, fmt_value, relative_minute

LABELS = {"latency_p95": "p95 latency", "error_ratio": "error rate", "request_rate": "request rate"}
MAX_EVIDENCE = 10


def _shown(signal: str, value: float) -> str:
    text = fmt_value(signal, value)
    return f"{text}%" if signal == "error_ratio" else f"{text} {UNITS[signal]}"


def _row(s: Sample | Anomaly, t0: float) -> dict:
    return {
        "service": s.service,
        "signal": s.signal,
        "ts": relative_minute(s.ts, t0),
        "value": float(fmt_value(s.signal, s.value)),
    }


def gap_prose(service: str) -> tuple[str, str]:
    """The summary and first check of a monitoring gap: the service could not be scraped."""
    return (
        f"Telemetry from {service} went missing: it could not be scraped.",
        f"Check that {service} is running and that its metrics page answers, "
        "then its most recent deploy and restarts.",
    )


def template_writeup(incident: Incident, context_samples: Sequence[Sample]) -> dict:
    t0 = incident.opened_at
    culprit = incident.culprit or incident.alerts[0].service
    alerting = list(dict.fromkeys(a.service for a in incident.alerts))
    usable = [a for a in incident.alerts if a.signal in EVIDENCE_SIGNALS]
    focus = next((a.signal for a in usable if a.service == culprit), None)
    focus = focus or (usable[0].signal if usable else None)
    cells = sorted(context_samples, key=lambda s: s.ts)
    series = [s for s in cells if s.service == culprit and s.signal == focus]

    if incident.kind == "monitoring_gap" or focus is None:
        summary, first_check = gap_prose(culprit)
    else:
        label = LABELS[focus]
        summary = f"{culprit} is the suspected source, alerting on its {label}."
        if series:
            summary = (
                f"{culprit} is the suspected source. Its {label} is "
                f"{_shown(focus, series[-1].value)} at the incident start, against "
                f"{_shown(focus, series[0].value)} at the start of the evidence window."
            )
        first_check = (
            f"Check {culprit} first: its most recent deploy, its logs around the incident start, "
            f"and its {label} panel. Then follow its runbook."
        )
    others = [s for s in alerting if s != culprit]
    if others:
        summary += f" Alerts also came from {', '.join(others)}."

    # Evidence: the focus series at its newest and oldest cell, then the newest cell of every
    # other series (culprit first). Only with no cells at all do the alerts themselves serve.
    latest = {(s.service, s.signal): s for s in cells}
    picks = [series[-1], series[0]] if series else []
    picks += sorted(latest.values(), key=lambda s: (s.service != culprit, s.signal != focus))
    picks = picks or usable
    rows: list[dict] = []
    for p in picks:
        row = _row(p, t0)
        if row not in rows and len(rows) < MAX_EVIDENCE:
            rows.append(row)
    if not rows:
        raise ValueError(f"incident {incident.id}: no evidence at all for a template write-up")

    return {
        "summary": summary,
        "suspected_service": culprit,
        "evidence": rows,
        "first_check": first_check,
    }
