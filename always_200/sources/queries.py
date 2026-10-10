"""The PromQL behind each detector signal (r2 §3), plus Prometheus's own `up`.

The recorder (step 11) and the live source (step 15) run exactly these queries, so a recording and
a live run can never disagree about what a signal means. Every query keeps one label, `service`,
so a result row maps to one service.

The error ratio uses the "or * 0" form. The 5xx counter of a healthy service does not exist until
its first error, and a plain `5xx / all` then returns no series at all: the error signal of a
healthy service would never be evaluated (Review Focus 2). `(5xx or all * 0) / all` falls back to
"all requests times 0" wherever the 5xx series is absent, so a healthy service reads 0, not
missing. At zero traffic it is 0 / 0 = NaN, which the API client drops.

`up` assumes the scrape config gives every patient target a `service` label (step 5).
"""

import re

_RATE = "rate(http_requests_total[1m])"
_ERRORS = 'rate(http_requests_total{code=~"5.."}[1m])'

QUERIES: dict[str, str] = {
    "up": 'max by (service) (up{service!=""})',
    "request_rate": f"sum by (service) ({_RATE})",
    "latency_p95": (
        "histogram_quantile(0.95, sum by (le, service) "
        "(rate(http_request_duration_seconds_bucket[1m])))"
    ),
    "error_ratio": (
        f"(sum by (service) ({_ERRORS}) or sum by (service) ({_RATE}) * 0)"
        f" / sum by (service) ({_RATE})"
    ),
}

# A detector query may read only these metrics. The answer-key series are blocked so the
# detector can never see the fault it is supposed to find (D11).
ALLOWED_METRICS = frozenset({"http_requests_total", "http_request_duration_seconds_bucket", "up"})
BLOCKED_METRICS = frozenset(
    {
        "fault_active",
        "event_active",
        "deploy_marker_total",
        "session_phase",
        "session_seed",
    }
)


def metric_names(expr: str) -> set[str]:
    """Metric identifiers directly followed by '{' or '['."""
    return set(re.findall(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\s*[{\[]", expr))
