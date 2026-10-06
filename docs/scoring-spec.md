# Scoring spec

This document fixes how every detector is graded. It was committed **before any detector code
existed**, so no rule below can be chosen after seeing a result. One scorer serves every detector:
the always-200 band, the fixed threshold and the PromQL adaptive band.

A change to this document after detector code exists must be a separate commit that says why, and
any re-score of held-out data that it causes is logged in `docs/results-log.md`.

## Constants

Fixed in advance, never tuned (they live in `config/default.yaml` under `timing`):

| Constant | Value |
|---|---|
| scrape interval | 5 s |
| detector step | 15 s |
| rate window | 1 m |
| N to fire | 3 consecutive steps |
| steps to clear | 4 consecutive steps |
| grace | 90 s |
| warm-up | 10 min |
| clean soak | 60 min |
| cool-down | 6 min |
| incident close | 3 min with no alert |
| culprit settle window | 60 s |

Fault sizes (extra latency is defined in P6):

| Size | Extra latency | Error ratio |
|---|---|---|
| subtle | +30% | 5% |
| moderate | +100% | 10% |
| severe | +400% | 40% |

Dependency graph: `storefront → orders → payments`. Postgres is inside `orders` and `payments`
and is not a separate node.

## 1. Run layout (one recording session)

| Phase | Length | Scored |
|---|---|---|
| Warm-up | 10 min | No. Twice the 5-minute baseline window, so the rate windows and the band are full before scoring starts. The detector is fed warm-up samples; the incident builder is not (P1b). |
| Clean soak | 60 min, no faults, normal day-curve load | False alarms only |
| 48 slots | each one fault or one non-incident event, then a 6-min cool-down | Yes |

- **Slots:** 36 faults and 12 non-incident events per session, in seeded random order. Fault
  duration is uniform in 2–5 min; event duration is uniform in 2–4 min.
- **Cool-down = 6 min.** It exceeds the 5-minute baseline window plus N steps (45 s), so the band
  has re-learned normal before the next slot. It also exceeds grace (90 s) + the 4-step clear
  countdown (60 s) + incident close (180 s) = 330 s, so one slot's incident cannot merge into the
  next. Detectors emit alerts only on out-of-band steps (P1), so counting the countdown is the
  conservative sum.
- **No two faults overlap** in v1.
- **Session length** ≈ 10 + 60 + 48 × ~9.5 min ≈ 8.8 h.
- **Sessions:** A = 1 session (seed `A1`, 36 faults), used for tuning only. B = 3 sessions (seeds
  `B1`–`B3`, 108 faults), held out for scoring.

## 2. Time on one clock

- The answer key is derived from the recorded `fault_active` and `event_active` gauges.
- `fault_start` = the first scrape where the gauge reads 1. `fault_end` = the first scrape where it
  reads 0. Resolution is one scrape (5 s).
- For a ramped fault, `fault_start` is the start of the ramp, not the moment it becomes visible.
  This is the stricter choice for the detector.
- `scored_start` = the end of warm-up.

## 3. Alerts and incidents

- A detector emits **alerts** (`Anomaly` objects) per (service, signal). The shared incident builder
  groups them:
  - an alert while no incident is open **opens** an incident at the alert's timestamp;
  - alerts on any service while an incident is open **join** it;
  - an incident **closes** after 180 s of sample time with no alert (P1).
- **Culprit rule.** Take the services with alerts in the incident's first 60 s (the settle
  window). Pick the **most downstream** one in the dependency graph (greatest depth). Break ties by
  the earliest alert. The culprit at the end of the settle window is final and is the one scored.
- All counting below is **per incident**, not per alert, and identical for every detector.

## 4. Detection

- A fault is **detected** when an incident **opens** inside `[fault_start, fault_end + grace]` and
  contains an alert of the fault's signal kind (latency faults → `latency_p95`, error faults →
  `error_ratio`) on the target service or on a service upstream of it.
- **Grace = 90 s**: the 1-minute rate window plus 2 steps, the time a fault can stay visible in the
  signal after it ends. Fixed in advance.
- An incident already open before `fault_start` does not count. If it swallows the fault, the fault
  is a **miss** and is also counted as **swallowed** (P3).

## 5. Detection latency

- **Latency = incident open time − `fault_start`**, over detected faults only.
- Reported as the **median and the p90**, with a bootstrap 95% interval for the median (2,000
  resamples, fixed seed).
- Misses are reported as a detection rate and are never folded into a mean.
- **Stated floor.** For a large step fault a sample leaves the band after at most one scrape (5 s)
  plus one detector step (15 s), then needs N − 1 = 2 more steps (30 s). Latency is therefore about
  30–50 s at best; subtle and ramped faults take longer because the 1-minute rate window dilutes
  them.
- **Time-to-ticket** (incident open → ticket written) is reported separately. It includes the 60 s
  settle window and the write-up time (P14).

## 6. False alarms

- A **false alarm** is an incident that opens in clean time, or one that opens during
  `[fault_start, fault_end]` touching neither the target nor any service upstream of it (P3).
- **Clean time** = all scored time outside every `[fault_start, fault_end + grace]`: the soak, the
  cool-downs after grace, and the non-incident events.
- Metric: **false alarms per clean hour**, with an exact Poisson 95% interval.
- False alarms **during non-incident events** are also reported on their own (P4).
- Clean time per session ≈ 60 + 36 × 4.5 + 12 × ~9 min ≈ 5.6 h, so B gives ≈ 17 clean hours.

## 7. Recovery tail

- An incident that opens in `(fault_end, fault_end + grace]` and is not the detecting incident is
  **neither a hit nor a false alarm**. It is counted for information only.
- An incident that opens during the fault and stays open past `fault_end` is simply the detection.

## 8. Culprit accuracy

- **Culprit %** = detected faults whose final culprit equals the target service, computed
  identically for every detector through the shared incident builder.
- **No "chance = 33%" line.** The rule picks the most downstream alerting service, and a downstream
  fault is felt upstream because the caller waits. A high culprit % mostly means "the affected
  services alerted"; the table must not suggest more than that.
- The rule is printed in the table header: "culprit = most downstream service alerting in the first
  60 s, earliest on ties".
- **Comparison row, "most downstream alerting service":** the same incidents, with the culprit taken
  as the most downstream service that alerted at any time in the incident (no settle window). It
  shows what the 60 s settle window adds or costs.

## 9. Uncertainty

- Every proportion (detection rate, culprit %, LLM scores) is printed with a **Wilson 95%
  interval**, per size bin as well.
- A cell whose denominator is 0 prints **`n/a`**, never a number and never an error.

## 10. Tune on A, score on held-out B

- All tuning (band k, latency and error spread floors, fixed-threshold factor, PromQL k) uses
  **session A only**.
- **Selection rule, the same for every detector:** the operating point with the highest detection
  rate on A at **≤ 0.5 false alarms per clean hour**; tie-breaks and the infeasible case in P9.
- **How thin that is.** A has about 5.6 clean hours, so 0.5 per hour allows at most 2 false alarms
  (3 would be 0.54). **The frozen point is therefore chosen on about two false-alarm events on A**,
  and one extra false alarm can move it. The sweep on B is what keeps this honest, and the README's
  results section says so.
- The chosen values are written to `config/frozen.yaml` and committed before B is recorded.
  Scoring B reads only that file.
- Every re-score of B after a config change is logged in `docs/results-log.md` with its reason. A
  silent re-score is not allowed.

## 11. Results layout

```
Culprit rule: most downstream service alerting in the first 60 s, earliest on ties
Detector               Detection (95% CI)   Swallowed   Latency median / p90   FA per clean h (95% CI)   FA in events   Culprit % (95% CI)
always-200 band        __ (__–__)           __          __ s / __ s             __ (__–__)                __             __ (__–__)
PromQL adaptive band   ...
Fixed threshold        ...
Culprit comparison row: "most downstream alerting service" (whole incident, no settle window), per detector

By fault size (detection %):   subtle (+30% latency / 5% errors) | moderate (+100% / 10%) | severe (+400% / 40%)
By fault type:                 latency step | latency ramp | errors
Detection ceiling, beside the subtle and errors rows: per detector, the smallest error ratio it can
  fire on from a zero-error baseline, and the share of that row's faults sized at or above it
Sweep: 4 operating points per detector on B (detection vs FA per clean hour), frozen point marked
Under the table: the three differences between the Python band and the PromQL band (P16)
```

Any cell whose denominator is 0 prints `n/a`.

## Rulings fixed before detector code

The sections above leave the following points open or ambiguous. Each is fixed here, before any
detector exists. P6, P16 and P17 decide published numbers, so they cannot move after this commit
either.

**P1. Alerts during the clear countdown; when an incident is quiet.** While a series is in alert,
a detector returns an `Anomaly` only on out-of-band steps. The in-band steps of the 4-step clear
countdown return `None`; the series still counts as alerting, so its baseline stays frozen. The
flag `first=True` marks the firing step. An incident is quiet when no `Anomaly` has arrived for
180 s of **sample time**. The non-merge sum counts the countdown anyway: grace 90 s + countdown
60 s + close 180 s = 330 s < 360 s cool-down. *Why:* it makes "closes after 3 minutes with no
alert" measurable, and a countdown can never stretch one fault's incident into the next.

**P1b. Who is fed warm-up.** The detector is fed every sample, warm-up included. The incident
builder is fed only anomalies (and ticks) at or after `scored_start`, so no incident can open in
warm-up. *Why:* warm-up exists to fill the band; an incident opened there would swallow the session
unscored.

**P2. Missing telemetry.** `telemetry_missing` is 1 at a step only where `up == 0` or the scrape is
missing (no `up` sample). Where `up == 1` but the rate series is empty (an idle counter), the step
is **skipped** for that service: no signal samples, `telemetry_missing = 0`, baseline untouched. A
**monitoring-gap alert** fires after N = 3 consecutive missing steps and clears after 4. It goes to
a **separate incident stream** (`kind="monitoring_gap"`) that fault scoring never sees. *Why:*
missing data must alert (absent for more than 2 steps), a 2–5 s rolling-restart blip cannot page
anyone, and a quiet healthy service is never a monitoring gap.

**P3. Incident classification.** Each incident gets exactly one class, in open-time order:
- **detection:** it opens in `[fault_start, fault_end + grace]` with an alert of the fault's signal
  kind on the target or upstream, and it is the first such incident;
- **duplicate:** the same, but a later one;
- **wrong-signal:** it touches the target or upstream with the wrong signal and opens during the
  fault;
- **false alarm:** it opens in clean time, or opens during `[fault_start, fault_end]` touching
  neither the target nor upstream;
- **recovery tail:** it opens in `(fault_end, fault_end + grace]` and is not the detection.

Duplicate, wrong-signal and recovery-tail are counted for information only. Per fault, a miss
caused by an incident that opened before `fault_start` and was still open at `fault_start` is also
counted as **swallowed** (in `SessionScore.info` and as a table column). *Why:* no incident is left
unclassified, and a falling detection rate always says why.

**P4. False alarms during events** are false alarms that open in
`[event_start, event_end + grace]`. *Why:* the same 90 s lingering effect as faults.

**P5. Fixed-threshold value.** `threshold = max(factor × p99_night_A, p99_night_A + min_delta)`,
with the same request gate and the same N as the band. `p99_night_A` is the p99 over the
**low-load half** of A's clean soak: the soak steps where storefront's `request_rate` is at or
below its soak median. `min_delta` is 20 ms for latency and 1 percentage point for errors.
Thresholds exist only for `latency_p95` and `error_ratio`; `request_rate` never yields an
`Anomaly`. *Why:* the clean-soak p99 of the error ratio is usually 0, and a threshold of 0 would be
a straw man; fitting on the low-load half is the night-tuned case a fixed threshold is criticised
for, while a whole-soak p99 is already the midday value.

**P6. Fault size reference.** Fault size is measured against the target service's own p95 over its
last 500 requests, captured at fault start. The extra delay is `share × that p95` (subtle 0.3,
moderate 1.0, severe 4.0). *Why:* "+30% latency" needs a reference, and this one calibrates itself
under any load.

**P7. Sample order within a step:** by service (`storefront, orders, payments`), then by signal
(`request_rate, latency_p95, error_ratio, telemetry_missing`). *Why:* the request gate needs the
step's request rate before it evaluates the step's latency.

**P8. Build order.** The incident builder and the culprit rule are built before the scorer. *Why:*
the scorer counts per incident, so the builder must exist first. This changes the order, not the
design.

**P9. Selection tie-breaks:** the highest detection on A at ≤ 0.5 false alarms per clean hour; then
lower false alarms; then the first point in grid order. If no point is ≤ 0.5, pick the lowest-FA
point, mark it **infeasible**, and say so in the README. *Why:* section 10 alone does not cover
ties or the infeasible case.

**P10. Grids.**
- band: k ∈ {3, 4, 5, 6} × latency floor share ∈ {0.05, 0.10, 0.20} × error floor ∈
  {0.0025, 0.005, 0.0075} (36 points);
- fixed-threshold factor ∈ {1.0, 1.25, 1.5, 2.0};
- PromQL k ∈ {3, 4, 5, 6}.

The band and the PromQL band compare with `>=` (`value >= median + k × spread`). The subtle error
size (5%) is above `k × error_floor` for every grid point (max 6 × 0.0075 = 4.5%), so no subtle
error fault is undetectable by construction. The reported 4-point sweep for the band varies k at
the chosen floors. *Why:* 4 reported points per detector, with both floors tuned the same way.

**P11. CI fixtures.** The CI fixture is the opening of A1, about 2 h: contiguous from the real
warm-up start, through the whole clean soak, to the end (cool-down included) of the first run of 4
consecutive slots holding 3 faults and 1 event. `scored_start` is the real one. A second,
hand-built soak fixture (a synthetic day-curve soak, no faults) asserts zero false alarms for the
frozen band. *Why:* the fixture must contain the clean soak, or a detector that false-alarms as the
day curve rises would still pass CI. About 1 KB per 15 s step keeps 2 h well under 1 MB.

**P12. Recording runs.** One traffic run may chain seeds (`B1,B2,B3` back to back). Each recording
run gets its own Prometheus volume, named after the run (`always-200-prom-<RUN>`); old volumes are
kept, never wiped and never reused. Each run takes one TSDB snapshot. A seed's window is the
**latest contiguous run** of `session_seed{seed} == 1`. *Why:* no deletion, three sessions start
with one command, and a re-recording of a holed session can never be merged with the failed one.

**P13. Jira in CI.** CI builds `JiraSink` only with an injected `httpx.MockTransport`. A test
asserts that the default config selects `JsonlSink`. *Why:* CI never touches the network, and the
Jira code is still tested.

**P14. Ticket timing.** A ticket opens when the culprit becomes final (the end of the 60 s settle
window). Alerts that join earlier go into the opening body; alerts that join later become comments.
*Why:* the settle window is already part of time-to-ticket.

**P15. Dataset URL.** The dataset download URL is derived at runtime from
`git remote get-url origin`, with the `ALWAYS200_DATASET_URL` environment variable as an override.
*Why:* no account name is ever committed.

**P16. What the PromQL row computes.** Prometheus computes the median and the spread with
subqueries; spread = IQR / 1.349, because PromQL cannot express a MAD about the window's own
median. The `k` comparison and the N-in-a-row rule run in Python over those cached series. A
Docker-only test checks that the full PromQL boolean rule at the frozen k equals the Python
comparison. The three differences from the Python band (spread estimator, frozen baseline, window
mechanics) are printed under the results table. *Why:* the sweep stays cheap, the "pure PromQL"
claim stays honest, and a win or loss is never read as "Python versus PromQL" alone.

**P17. LLM evidence format and "grounded".**
- Evidence is given at 1-minute resolution.
- Latency in whole milliseconds, error ratio in % with 1 decimal, request rate in req/s with 1
  decimal.
- Times are relative minutes (`t-10` … `t+0`).
- A number in a write-up is **grounded** only if it equals, **as a number**, a value from the
  **metric-value column** of the evidence, within `max(0.05, 1%)` of that value. Minute labels,
  timestamps and runbook digits never ground a number.

*Why:* the grounded-numbers score becomes mechanical and unambiguous, and `t-10` cannot ground
"10 ms".
