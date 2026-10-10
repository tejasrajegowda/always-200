"""The watcher's own metrics: who watches the watcher.

Prometheus scrapes this process and evaluates `Always200Down` when the scrape fails. The
numbers are about always-200 itself, never about the shop, so they are not on the detector's
allow-list.

`samples_total` and `sample_lag_seconds` move on every step the live source yields. The watch
command moves `alerts_open` and `incidents_open`. The write-up and the ticket sink (later steps)
move the last three; they exist now so those steps share one registry.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

from always_200.core import ANOMALY_KINDS


class SelfMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.samples_total = Counter(
            "always200_samples_total",
            "Samples yielded by the live source.",
            registry=self.registry,
        )
        self.sample_lag_seconds = Gauge(
            "always200_sample_lag_seconds",
            "Seconds between a step's own timestamp and the moment it was read.",
            registry=self.registry,
        )
        self.alerts_open = Gauge(
            "always200_alerts_open",
            "Service and signal pairs inside incidents that are still open.",
            registry=self.registry,
        )
        self.incidents_open = Gauge(
            "always200_incidents_open",
            "Open incidents of this kind. One kind has at most one open incident.",
            ["kind"],
            registry=self.registry,
        )
        for kind in ANOMALY_KINDS:
            self.incidents_open.labels(kind=kind).set(0)
        self.llm_seconds = Histogram(
            "always200_llm_seconds",
            "Seconds spent waiting for one write-up from the model.",
            registry=self.registry,
        )
        self.writeup_fallbacks_total = Counter(
            "always200_writeup_fallbacks_total",
            "Write-ups that fell back to the template.",
            registry=self.registry,
        )
        self.sink_errors_total = Counter(
            "always200_sink_errors_total",
            "Ticket-sink attempts that failed and were written to the fallback log.",
            registry=self.registry,
        )

    def serve(self, port: int = 9200) -> None:
        """Serve `/metrics` on every interface, so a Prometheus container can scrape it."""
        start_http_server(port, registry=self.registry)
