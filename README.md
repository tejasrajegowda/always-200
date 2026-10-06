# always-200

**always-200 keeps your app at 200, and tells you the moment it isn't.**
(HTTP 200 means OK: the request worked.)

![ci](../../actions/workflows/ci.yml/badge.svg)

## What it is

- **A system under test.** A three-service demo shop (storefront, orders, payments) on Postgres,
  run with Docker Compose, with switches that inject latency and errors on demand.
- **A seeded traffic generator.** Open-loop load that follows a day curve, injects a known schedule
  of faults, and adds non-incident events (traffic surges, planned restarts) that a good detector
  must not page on.
- **Metrics.** Prometheus scrapes every service every 5 seconds: request rate, p95 latency and
  error ratio.
- **The detector.** always-200 learns a robust normal band per service and signal, raises an alert
  when readings stay outside it, groups alerts into incidents, picks the culprit service from the
  dependency graph, has a local LLM write the incident summary, and files a ticket for the
  service's owner.

Because the faults are injected on purpose, every recorded session has an answer key. Detection
rate, time to detect, false alarms per clean hour and culprit accuracy are scored against it, side
by side with two baseline detectors tuned on the same data. Sessions are recorded once and replayed
offline, so every result can be reproduced.

## Status

v1 under construction; no results yet.

## Licence

MIT for the code (see `LICENSE`). The dataset, once published, is CC BY 4.0.
