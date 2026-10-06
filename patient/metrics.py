"""The patient's Prometheus metrics. One registry per app, so tests can build many apps."""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

BUCKETS = (0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.075, 0.1, 0.15,
           0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.5, 5.0)  # fmt: skip


class PatientMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "http_requests_total",
            "Business requests handled, by status code.",
            ["service", "route", "code"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "http_request_duration_seconds",
            "Business request duration, injected fault delay included.",
            ["service", "route"],
            buckets=BUCKETS,
            registry=self.registry,
        )
        self.fault_active = Gauge(
            "fault_active",
            "1 while this fault is active: the answer key, never for the detector.",
            ["service", "type", "size"],
            registry=self.registry,
        )
