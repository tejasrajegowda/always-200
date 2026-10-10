# 0004. No Alertmanager in v1

**Status:** accepted

## Context

Alertmanager groups, silences and routes alerts, and Prometheus users already know it. The design
leaves it out of v1 core. The bench has to replay a long session in a few seconds and come out
with the same incidents every time, and the page a person receives has to name one service chosen
from the dependency graph.

## Decision

| Decision | Content | Why |
|---|---|---|
| Not in v1 | No Alertmanager process, and no webhook from one, in the v1 path. | Three reasons below. |
| Grouping | The incident builder groups alerts on sample time (`always_200/incidents.py`). | The same code runs live and on the bench. |
| Who watches the watcher | Prometheus evaluates one rule, `Always200Down` (`deploy/prometheus/rules/self.yml`): `up{job="always-200"} == 0` for 1 minute. It is visible on Prometheus's Alerts page. Nothing forwards it. | The watcher's own `/metrics` is scraped like any other target. A human can see that the watcher has stopped. Routing that alert onward can wait. |

The three reasons:

1. **Replay.** Alertmanager's grouping runs on wall-clock timers (`group_wait`, `group_interval`,
   `repeat_interval`). The bench replays many hours of samples in seconds. A service that sleeps
   on the wall clock cannot do that, and the detector is required to ignore the wall clock.
2. **The culprit rule.** Alertmanager can group by labels. It cannot choose the most-downstream
   anomalous service from a dependency graph. That choice would still live in always-200, so
   Alertmanager would not remove the code it is often brought in to replace.
3. **The order of work.** It is one more tool to learn and wire up before the first measured
   number exists.

## Consequences

- Live and replay share one grouping path. A score from the bench is a score of the live product.
- `Always200Down` does not page anyone by itself. In v1 it is a light on the Alerts page.
- A later version can add an optional Alertmanager webhook into the live demo, so the fixed rule,
  the PromQL band and this band share one downstream path. That path must not sit on the offline
  bench.
