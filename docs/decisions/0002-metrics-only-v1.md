# 0002. Metrics only in v1

**Status:** accepted (the design's recommended default; if traces are chosen for v1 instead, this
record is superseded by `0002-traces-in-v1.md` and a traces step is added after Prometheus and
Grafana)

## Context

The patient services could report three kinds of telemetry: metrics (counters, gauges and
histograms scraped by Prometheus), traces (one record per request as it crosses services, via
OpenTelemetry) and logs. The v1 goal is one measured, reproducible detection score: a seeded
faulty shop, a detector, fair baselines and a scorer, all replayable offline. Every detector in
v1 reads the same three signals per service: request rate, p95 latency and error ratio. All three
come from metrics.

## Decision

| Decision | Content | Why |
|---|---|---|
| Telemetry in v1 | Prometheus metrics only: `http_requests_total`, `http_request_duration_seconds` and the answer-key gauge `fault_active`. No traces, no logs. | Every v1 signal is a metric; traces would add a collector, a trace store and a second recording format before the first number exists. |
| Dependency graph | Declared in `config/default.yaml`, not discovered. | Three services with a fixed call chain; a declared graph is exact and testable. |
| Traces | Arrive in v1.x with OpenTelemetry instrumentation, a collector and a trace backend. | They are needed for the second, larger patient, where the graph is too big to declare by hand. |

## Consequences

- The record and replay formats in v1 hold metric samples only.
- The culprit rule uses the declared graph; when traces arrive, the graph can be derived from them
  and checked against the declared one.
- The v1 stack does not include OpenTelemetry.
