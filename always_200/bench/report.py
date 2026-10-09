"""The results table (scoring spec section 11, r2 §4.11).

`render_table` prints, for each detector: the main row, the culprit comparison row, detection by
fault size and detection by fault type. The culprit rule is printed in the header, there is no
"chance" baseline line (spec section 8), and a cell whose denominator is 0 prints `n/a`.
`to_json` gives the same numbers as a plain dict, rounded to 4 decimal places, so CI can compare
two runs exactly.

`ticket_lines` prints one time-to-ticket line per detector (step 23): the settle wait, the write-up
and the sink, apart from detection latency (r2 §4.5).

The detection ceiling, the 4-point sweep and the notes on the PromQL band are added by the steps
that build those detectors and the tuning (steps 16, 17 and 19).
"""

from dataclasses import asdict
from statistics import median

from always_200.bench.score import CI, SIGNAL_FOR_FAULT, SIZES, Summary
from always_200.tickets.flow import TicketTiming

CULPRIT_RULE = "culprit = most downstream service alerting in the first 60 s, earliest on ties"
NA = "n/a"
SIZE_HEADERS = {
    "subtle": "subtle (+30% latency / 5% errors)",
    "moderate": "moderate (+100% / 10%)",
    "severe": "severe (+400% / 40%)",
}


def _pct(ci: CI) -> str:
    if ci is None:
        return NA
    p, lo, hi = ci
    return f"{p * 100:.1f}% ({lo * 100:.1f}-{hi * 100:.1f})"


def _latency(s: Summary) -> str:
    if s.latency_median_ci is None or s.latency_p90 is None:
        return f"{NA} / {NA}"
    med, lo, hi = s.latency_median_ci
    return f"{med:.0f} s ({lo:.0f}-{hi:.0f}) / {s.latency_p90:.0f} s"


def _rate(ci: tuple[float, float, float]) -> str:
    r, lo, hi = ci
    return f"{r:.2f} ({lo:.2f}-{hi:.2f})"


def _grid(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    return [
        "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip()
        for r in [header, *rows]
    ]


def render_table(rows: dict[str, Summary]) -> str:
    out = [CULPRIT_RULE, ""]
    out += _grid(
        [
            "Detector",
            "Detection (95% CI)",
            "Swallowed",
            "Latency median / p90",
            "FA per clean h (95% CI)",
            "FA in events",
            "Culprit % (95% CI)",
        ],
        [
            [
                name,
                _pct(s.detection),
                str(s.swallowed),
                _latency(s),
                _rate(s.fa_rate_ci),
                str(s.fa_in_events),
                _pct(s.culprit),
            ]
            for name, s in rows.items()
        ],
    )
    out += [
        "",
        'Culprit comparison row: "most downstream alerting service" (whole incident, no '
        "settle window)",
    ]
    out += _grid(
        ["Detector", "Culprit % (95% CI)"],
        [[name, _pct(s.culprit_downstream_any)] for name, s in rows.items()],
    )
    out += ["", "By fault size (detection %)"]
    out += _grid(
        ["Detector", *(SIZE_HEADERS[z] for z in SIZES)],
        [[name, *(_pct(s.by_size[z]) for z in SIZES)] for name, s in rows.items()],
    )
    out += ["", "By fault type (detection %)"]
    out += _grid(
        ["Detector", *SIGNAL_FOR_FAULT],
        [[name, *(_pct(s.by_type[t]) for t in SIGNAL_FOR_FAULT)] for name, s in rows.items()],
    )
    out += ["", f"{NA} = the denominator is 0 (nothing to measure)."]
    return "\n".join(out) + "\n"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def ticket_lines(times: dict[str, list[TicketTiming]]) -> str:
    """Per detector: the median time to ticket of its fault tickets and the median of each part
    (settle + write-up + sink). Monitoring-gap tickets are a separate stream, only counted."""
    out = ["Time to ticket (settle + write-up + sink), apart from detection latency:"]
    for name, timings in times.items():
        faults = [t for t in timings if t.kind == "fault"]
        gaps = len(timings) - len(faults)
        if faults:
            line = (
                f"- {name}: median {median(t.total_s for t in faults):.1f} s "
                f"(settle {median(t.settle_s for t in faults):.1f} s, "
                f"write-up {median(t.writeup_s for t in faults):.3f} s, "
                f"sink {median(t.sink_s for t in faults):.3f} s) "
                f"over {_count(len(faults), 'ticket')}"
            )
        else:
            line = f"- {name}: {NA} (no fault tickets)"
        out.append(line + (f"; {_count(gaps, 'monitoring-gap ticket')}" if gaps else ""))
    return "\n".join(out) + "\n"


def _rounded(value):
    if isinstance(value, bool) or value is None or isinstance(value, int | str):
        return value
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {k: _rounded(value[k]) for k in sorted(value)}
    if isinstance(value, list | tuple):
        return [_rounded(v) for v in value]
    raise TypeError(f"cannot put {type(value).__name__} into the JSON report")


def to_json(rows: dict[str, Summary]) -> dict:
    """Floats rounded to 4 dp (None stays None), tuples as lists, keys sorted."""
    return {name: _rounded(asdict(s)) for name, s in sorted(rows.items())}
