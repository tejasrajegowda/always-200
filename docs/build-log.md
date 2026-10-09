# Build log

What was built, in order, and why.

## Step 1: repo setup

- **Layout.** A flat application layout with three top-level packages (`patient/`, `traffic/`,
  `always_200/`); see [ADR 0001](decisions/0001-repo-structure.md). Only `always_200/` exists so
  far, holding the version string.
- **Environment.** Python 3.12, managed by uv. `uv.lock` pins every version, and CI installs with
  `uv sync --locked`, so it fails if the lock and `pyproject.toml` disagree.
- **Checks.** ruff (lint rules E, F, I, UP, B; 100-character lines), `ruff format --check`, and
  pytest. Tests marked `docker` are skipped by default and never run in CI.
- **Commit checks.** Every commit goes through `scripts/commit.py`, which refuses an empty,
  multi-line or over-100-character message, runs the same three checks as CI, stages, and runs the
  guard (`scripts/guard.py`) before committing. The guard refuses local-only paths (built-in and
  local), absolute user-folder paths, forbidden words from a local list, secret-shaped strings,
  trailer lines in messages, files over 2 MB (`docs/demo.gif` may be up to 10 MB) and an
  unexpected author identity. It fails closed when its local configuration is missing or its word
  list is empty. The same guard runs from the git hooks in `.githooks/`, including before every
  push.
- **Line endings.** `.gitattributes` stores text with LF, so the shell hooks run on every platform.
- **CI.** GitHub Actions runs ruff and pytest on every push and pull request, with read-only
  repository permissions and no network access beyond installing the locked dependencies.
- **Licence.** MIT for the code. The dataset will be published separately under CC BY 4.0.

## Step 2: scoring spec, config, Makefile

- **Scoring spec.** [`scoring-spec.md`](scoring-spec.md) fixes how every detector is graded: run
  layout, the answer key's clock, incidents and the culprit rule, detection, latency, false alarms,
  recovery tail, culprit accuracy, uncertainty, tune-on-A / score-on-B, and the results layout. It
  also fixes rulings P1–P17 (P1b included), which close the gaps in those rules. It is committed
  before any detector code exists, so no rule can be chosen after seeing a result.
- **Config.** `config/default.yaml` holds the services, the dependency graph, the timing constants
  (never tuned), the default band settings and the selection limit. `always_200/config.py` loads it
  with PyYAML; `load_config(frozen=True)` overlays `config/frozen.yaml` key by key and raises
  `FileNotFoundError` while that file does not exist, so held-out scoring cannot run before the
  frozen point is committed.
- **Graph.** `always_200/graph.py` holds the caller → callees graph. `depth()` is the longest path
  from a root (used by the culprit rule) and `upstream()` is every service that can reach a service
  (used by the scorer). Unknown callees and cycles are refused when the graph is built.
- **Commands.** A `Makefile` with `lint` and `test`; each target is one `uv run …` line that also
  works without make. `env.example` lists the live-demo settings with empty values (copy it to `.env` and fill it in; it has no leading dot so it can never be mistaken for the real, untracked `.env`).
- **Check.** `make test` (or `uv run pytest -q`): 25 tests pass.

## Step 3: the patient service, in-process

- **One service, three roles.** `patient/app.py` builds one FastAPI app per service with
  `create_app(service, store, downstream, clock, seed, exit_fn, sleep)`. storefront `POST /buy`
  calls orders `POST /orders`, which writes an order, reads the customer's last 5, calls payments
  `POST /payments`, then marks the order paid. Any downstream failure (non-200 or a transport
  error) becomes a 502, so a fault travels up the chain as errors.
- **Metrics.** `patient/metrics.py` gives each app its own registry: `http_requests_total{service,
  route, code}`, `http_request_duration_seconds{service, route}` with 18 fixed buckets (5 ms to
  5 s), and `fault_active{service, type, size}`, the answer key. All nine `fault_active` series
  exist from start-up at 0. Only the business route is measured; `/metrics`, `/healthz` and
  `/admin/*` are not.
- **Sick switch.** `patient/fault.py` holds one fault: a latency step, a latency ramp (full size at
  half the duration) or errors, sized subtle / moderate / severe. Latency is a share of the
  service's own p95 over its last 500 requests, captured at fault start (`patient/recent.py`,
  ruling P6). `POST /admin/fault` starts one (409 if one is active, 422 on a bad body,
  `0 < duration_s <= 600`), `GET` shows it, `DELETE` ends it. The middleware starts its timer
  **before** the injected delay, so the delay is inside the histogram. The gauge drops to 0 when
  the fault ends, via a 1 s background ticker and a refresh before each scrape.
- **Restart.** `POST /admin/restart` answers 202 and calls `exit_fn(0)` 0.2 s later; under Compose
  the restart policy brings the service back.
- **Store.** `patient/store.py` defines the `Store` protocol and an in-memory `MemoryStore`; the
  Postgres store arrives in step 4. A payment's `order_id` is optional, so payments can be called
  on its own.
- **Tests.** The clock, the sleep and the exit function are injected, so the tests chain the three
  apps in-process (`httpx.ASGITransport`) on a fake clock and never wait for a fault delay.
- **Decision.** [ADR 0002](decisions/0002-metrics-only-v1.md): v1 is metrics only.
- **Check.** `make test` (or `uv run pytest -q`): 56 tests pass, without Docker.

## Step 6: the seeded session schedule and the day curve

Steps 4 (Postgres and Compose) and 5 (Prometheus and Grafana) are deferred; this step is pure code
and needs neither.

- **Schedule.** `traffic/schedule.py` turns a seed into a `SessionPlan`: 600 s warm-up, 3600 s
  clean soak, then 48 slots (36 faults, 12 non-incident events), each followed by a 360 s
  cool-down. One `random.Random("always-200:<seed>")` drives everything, so the same seed always
  gives the same plan. Sizes and fault types are exact thirds (12 each) and event kinds exact
  quarters (4 each of `traffic_spike`, `rolling_restart`, `deploy_only`), each shuffled
  independently, and the fault/event order is shuffled too. A fault lasts 120–300 s and hits a
  random service; with probability 0.5 it gets a deploy marker 30–120 s before it starts, which is
  always after the previous slot's 90 s grace. An event lasts 120–240 s. A full session is about
  8.6–9.0 h.
- **Quick mode.** `build_session(seed, quick=True)`: 600 s warm-up, 300 s soak (or `soak_s`), then
  three severe faults, one per service, in the order step, ramp, errors. For demos and smoke runs.
- **Load.** `traffic/load.py`: `day_curve(t)` is a cosine "day" compressed into one hour, 30% of
  peak at t = 0 and 100% at t = 30 min. `Load(seed).rate(t)` is `25 req/s × day_curve × (1 +
  noise)`, where the noise is a straight line between seeded knots every 60 s, uniform in ±5%; a
  traffic spike multiplies the rate by 2.5. Knots are drawn in order and kept, so a rate never
  depends on which times were asked first.
- **Consistency.** A test checks that the schedule's fault types and sizes are exactly the
  patient's sick-switch menu.
- **Check.** `make test` (or `uv run pytest -q`): 76 tests pass, without Docker.

## Step 8: the seam, the answer key and ReplaySource

Steps 4, 5, 7a and 7b need Docker or long unattended runs and are deferred; this step is pure
code and needs none of them.

- **Core types.** `always_200/core.py` defines `Sample`, `Anomaly`, the `Source` and `Detector`
  protocols, and the fixed orders `SERVICES` and `SIGNALS` (ruling P7). A `Sample` refuses any
  signal other than the four detector signals (so the answer-key gauges can never reach a
  detector, D11), an unknown service, and a non-finite value. An `Anomaly` refuses an unknown
  kind. `sample_order` is the stream's sort key. See [ADR 0003](decisions/0003-the-seam.md).
- **Assembly.** `always_200/sources/assemble.py` turns per-step query results
  (signal → service → timestamp → value, `up` included) into samples, step by step in P7 order.
  `up == 0` or a missing scrape gives only `telemetry_missing = 1`; `up == 1` with no request
  rate (an idle counter) gives only `telemetry_missing = 0` and skips the step (ruling P2); NaN
  values are skipped; a real 0 is kept.
- **Files.** `always_200/bench/files.py` reads and writes JSON Lines, gzipped when the name ends in
  `.gz`, with a zero gzip timestamp so equal content gives equal bytes. NaN is refused on write.
  The module docstring documents `samples.jsonl.gz`, `answer_key.jsonl` and `deploy_markers.jsonl`.
- **Replay.** `always_200/sources/replay.py`: `ReplaySource(path).samples()` streams a recorded
  file and raises `ValueError` on a sample out of order or a duplicate.
- **Answer key.** `always_200/bench/answer_key.py`: `on_intervals` (first 1 to first later 0,
  unclosed runs end at the last point), `latest_run` (ruling P12), `derive` (faults numbered
  `<seed>-fNN` by start time; `scored_start`, `soak_end` and `scored_end` from `session_phase`,
  refused if the session never finished), `scrape_gaps` (holes over 30 s, Review Focus 1), and
  `write_key` / `read_key`.
- **Check.** `make test` (or `uv run pytest -q`): 126 tests pass, without Docker.

## Step 9: the incident builder and the culprit rule

Pulled ahead of the detector and the scorer by ruling P8: the scorer counts per incident, so the
builder must exist first. Pure code, no Docker.

- **Incidents.** `always_200/incidents.py`: `IncidentBuilder(graph, close_after_s=180,
  settle_s=60)` groups `Anomaly`s into `Incident`s. There is one open incident per kind, so a
  `monitoring_gap` never mixes with a `fault` (ruling P2). An alert joins the open incident of its
  kind, or opens a new one with id `<kind>-<int(opened_at)>`. An incident closes when
  `now - last_alert_at >= 180 s`, and `closed_at` is `last_alert_at + 180` (r2 §4.3, ruling P1).
- **Culprit.** `choose_culprit` takes the services that alerted within 60 s of the opening and
  picks the most downstream one by `Graph.depth`, the earliest on a tie (D7). It is set on the
  first tick at or after `opened_at + 60` and never changes afterwards.
- **Events.** `feed` and `tick` return `IncidentEvent`s (`open`, `join`, `culprit`, `close`), so
  ticket code (step 23) can react without the builder knowing about it. `join` fires only for a
  `(service, signal)` pair new to the incident. In one tick the culprit event comes before the
  close event. `finish(now)` settles any pending culprit at the end of a stream.
- **Time.** Only alert timestamps and `tick(now)` move the clock; the wall clock is never read
  (D5).
- **Check.** `make test` (or `uv run pytest -q`): 145 tests pass, without Docker.

## Step 10: the scorer, statistics and report

Pure code, no Docker. One scorer grades every detector, exactly as the scoring spec says.

- **Intervals.** `always_200/bench/stats.py`: `wilson(k, n)` (95% Wilson interval for a share;
  None when n = 0), `poisson_rate_ci(count, hours)` (exact Garwood 95% interval for false alarms
  per clean hour; a lower bound of 0 for a count of 0), and `bootstrap_median_ci(values)` (seeded
  percentile bootstrap, 2,000 resamples, for the median latency; None for no values). Adds the
  `numpy` and `scipy` dependencies.
- **Pipeline.** `always_200/pipeline.py`: `run_detector(samples, detector, builder,
  scored_start)` feeds every sample to the detector, but ticks and anomalies reach the incident
  builder only at or after `scored_start` (ruling P1b).
- **Trivial detectors.** `always_200/detect/trivial.py`: `AlwaysDetector` (every latency and error
  sample is an anomaly) and `NeverDetector`, the scorer's first check (r2 §4).
- **Scorer.** `always_200/bench/score.py`: `score_session(incidents, key, graph)` classifies every
  fault incident once, in open-time order (ruling P3): unscored, false alarm (plus "in an event",
  P4), detection, duplicate, recovery tail, wrong signal; a miss inside an incident that opened
  before the fault is also counted as swallowed. Monitoring-gap incidents are never scored here.
  Clean hours = scored span minus the union of `[start, end + grace]` windows clipped to it.
  `SessionScore.downstream_any` holds the comparison culprit (most downstream alerting service over
  the whole incident). `summarise(scores)` pools sessions into a `Summary` with all intervals,
  by size and by type.
- **Report.** `always_200/bench/report.py`: `render_table` prints the culprit rule, the main row,
  the comparison row, detection by size and by type, with `n/a` for empty denominators and no
  "chance" line; `to_json` gives the same numbers rounded to 4 dp for CI comparison. The
  detection ceiling, the sweep and the PromQL notes are added with those detectors (steps 16-19).
- **Check.** `make test` (or `uv run pytest -q`): 185 tests pass, without Docker.

## Step 11: record and export

The code and its tests, against a fake Prometheus (`httpx.MockTransport`); no Docker. The live
45-minute check runs once the Compose stack (steps 4, 5 and 7a) exists.

- **Queries.** `always_200/sources/queries.py`: `QUERIES`, the PromQL of `up`, `request_rate`,
  `latency_p95` and `error_ratio`, each grouped by `service`. The error ratio uses
  `(5xx or all * 0) / all`, so a service with no 5xx series yet reads 0, not missing (Review
  Focus 2). `up` expects the scrape config to give each patient target a `service` label.
- **API client.** `always_200/sources/prom_api.py`: `PromAPI.query_range` (service -> step ->
  value, NaN and infinity dropped, refuses more than 11,000 points per series before asking),
  `raw` (the scraped points of `selector[Ns]`), `snapshot` (the admin API's TSDB snapshot), and
  `align(t, step)`. A Prometheus `error` answer raises `PrometheusError`.
- **Record.** `always_200/bench/record.py`: `start(seeds, run_name, quick)` refuses a run name
  whose volume `always-200-prom-<RUN>` exists (ruling P12), then runs `docker compose --profile
  traffic up -d --build` with `SEEDS`, `QUICK` and `RUN` as separate environment variables, and
  prints the expected end in local time.
- **Export.** `always_200/bench/export.py`: `export_run` needs the traffic container exited with
  code 0; per seed it finds the latest contiguous `session_seed == 1` run, refuses any scrape gap
  over 30 s on any `up` series or `session_phase` (`SessionHoled`), queries and assembles the
  samples, derives the answer key, refuses any mismatch with `build_session` in fault count,
  closed faults, `(type, size)` counts and event kinds (`SessionIncomplete`), turns
  `deploy_marker_total` increments into markers, and writes `samples.jsonl.gz`,
  `answer_key.jsonl`, `deploy_markers.jsonl` and `session.yaml` under `recordings/<seed>/`. Every
  seed is checked before anything is written, and existing output is never overwritten. One TSDB
  snapshot per run is copied to `recordings/<run>/tsdb/` with `docker compose cp`.
- **CLI.** `always_200/__main__.py`: `record`, `export` and `bench` (replay -> `run_detector` ->
  `score_session` -> `render_table`, for the `never` and `always` detectors).
- **Makefile.** `record`, `export` (`QUICK=1` adds `--quick`) and `bench-dev`.
- **Check.** `make test` (or `uv run pytest -q`): 220 tests pass, without Docker. An offline
  version of the quick-recording check exports a fake 3-fault session and benches it: `never`
  scores 0% with no false alarm, `always` scores 0% with 3 swallowed and 1 false alarm.

## Step 14: the normal band detector

Pure code, no Docker. Step 13 (the CI fixture cut from session A1) is deferred until A1 is
recorded, so the fixture's `band` row is added then.

- **Fire and clear.** `always_200/detect/firing.py`: `FireClear(n_fire=3, n_clear=4)`, shared by
  every detector. `step(out)` returns `(emit, first)`: it fires after 3 consecutive out steps
  (`first=True` on that step only), emits on every out step while alerting, and clears after 4
  consecutive in steps; any out step restarts the countdown. The countdown steps emit nothing but
  `alerting` stays True until the clear (ruling P1).
- **Band.** `always_200/detect/band.py`: `BandConfig` (defaults equal `config/default.yaml`;
  `from_config` reads the `band` section plus the timing N's and refuses unknown keys) and
  `BandDetector`. Per service and signal: `request_rate` only feeds the request gate (requests in
  the last minute = rate x 60); `latency_p95` and `error_ratio` are skipped below 30 requests per
  minute, learned silently until the 20-value window is full, then judged against
  `median + k x max(1.4826 x MAD, floor)` with `>=` (ruling P10) and a minimum distance above the
  median (20 ms, 1 pp). The floor is `max(0.10 x median, 5 ms)` for latency and 0.005 for errors.
  A value is learned only when it is in band and the series is not alerting, so the baseline is
  frozen through an alert and its countdown. `telemetry_missing >= 1` goes through its own
  `FireClear` per service and comes out as `kind="monitoring_gap"` (ruling P2). Only sample
  timestamps are read (D5).
- **Synthetic soak fixture.** `tests/fixtures/soak_synthetic/`: `make_soak.py` builds a seeded,
  fault-free 70-minute session (10 min warm-up, 60 min soak) with the day-curve load, latency that
  rises with load, log-normal noise, one-step blips and rare random errors, and writes
  `samples.jsonl` and `answer_key.jsonl`. A test rebuilds it and compares bytes. The band raises
  no incident on it (at every point of the P10 grid, checked by hand; the default point is under
  test), and the same soak with +100% latency on orders for 3 minutes at midday opens one
  incident within 45 s with orders as the culprit.
- **CLI.** `python -m always_200 bench --detectors band` now runs the band from the default
  config.
- **Check.** `make test` (or `uv run pytest -q`): 264 tests pass, without Docker.

## Step 16: the fixed-threshold baseline and the tuning machinery

Pure code, no Docker. Session A1 is not recorded yet, so `make tune` has not run on it; the
machinery is tested on synthetic sessions. Step 13's fixture (and its `threshold` row) is still
deferred.

- **Threshold.** `always_200/detect/threshold.py`: `night_half(soak)` keeps every sample of the
  soak steps where storefront's request rate is at or below its soak median (ruling P5; a soak with
  no storefront rate is kept whole). `fit_thresholds(soak, factor, ...)` takes the p99 of the
  gated `latency_p95` and `error_ratio` values of that half, per service, and returns
  `max(factor x p99, p99 + min_delta)` (20 ms, 1 pp); `request_rate` never gets a threshold, and a
  series with no gated value gets none. `ThresholdDetector` uses the band's request gate, the
  shared `FireClear` (N = 3 to fire, 4 to clear) and the same monitoring-gap handling, compares
  with `>=`, and refuses a `request_rate` threshold. `from_config` takes the N's from `timing` and
  the gate from `band`.
- **Config.** `config/default.yaml` gains a `threshold` section: `factor: 1.5` (a placeholder
  until step 18) and the two minimum deltas.
- **Tuning.** `always_200/bench/tune.py`: `BAND_GRID` (36 points) and `THRESHOLD_GRID` (4) as
  ruling P10 fixes them; `select` applies ruling P9 (highest detection at <= 0.5 FA per clean
  hour, then lower FA, then grid order; with no feasible point the lowest-FA point, flagged
  infeasible). `tune(session_dir, cfg, out_dir)` replays the session once per grid point through
  `run_detector` and `score_session`, fits the thresholds on the low-load half of
  `[scored_start, soak_end)`, prints both grids and the band's k sweep at the chosen floors, and
  writes `band.yaml` and `threshold.yaml` (candidates, selection, feasibility, and the selected
  thresholds). It refuses a session whose seed starts with B and never overwrites earlier output.
- **CLI.** `python -m always_200 tune --session recordings/A1 --out runs/tune`; `make tune`
  (`OUT=...` picks another output folder).
- **Check.** `make test` (or `uv run pytest -q`): 292 tests pass, without Docker.

## Step 20: the LLM write-up (code only, mocked)

Pure code, no GPU and no network. Steps 17 to 19 are still deferred (they need Docker or the
recorded sessions); nothing here depends on them. See
[ADR 0005](decisions/0005-llm-writes-never-decides.md).

- **Schema.** `always_200/writeup/schema.json` (draft 2020-12): `summary` (at most 600
  characters), `suspected_service` (enum of the three services), `evidence` (1 to 10 rows of
  service, signal, minute and value, no extra fields) and `first_check` (at most 400), all required,
  no extra fields. Evidence `ts` is the relative minute (-2 for t-2) and `value` the number as
  printed.
- **Context (ruling P17).** `always_200/writeup/context.py`: `build_context` prints the graph, one
  table of `request_rate`, `latency_p95` and `error_ratio` for all three services from t-10 to t+0
  (the last sample of each minute; latency in whole ms, errors in % with one decimal, request rate
  in req/s with one decimal; `-` for no sample), the minutes with `telemetry_missing`, the deploy
  markers of the last 15 minutes as relative minutes, and the runbooks. It never names the culprit.
  `Context.values` holds only the value-column numbers, parsed back from the printed text;
  `Context.samples` holds the sample behind each printed cell. `load_runbooks` reads
  `config/runbooks/<service>.md`.
- **Client.** `always_200/writeup/llm.py`: `LLMClient` (text in, text out) and `OllamaClient`, one
  `POST /api/chat` with `stream: false`, the schema as `format`, `options: {temperature: 0, seed:
  42}`, a 60 s timeout and `think: false` only when asked. It raises httpx errors as they are and a
  ValueError for a reply without `message.content`.
- **Write-up.** `always_200/writeup/writeup.py`: `write` sends `prompt.md` followed by the context
  text, parses and validates the reply, and falls back to the template on `timeout`,
  `unreachable` (transport error or HTTP error status), `invalid_json` or `schema`. With no client
  the template is used and `error` is None. The incident is never changed.
- **Template.** `always_200/writeup/template.py`: the builder's culprit, the culprit's alerting
  signal at its newest and oldest cell, printed with the table's own format, and evidence rows from
  the context's cells (the alerts only when there are no cells). Schema-valid and grounded by
  construction.
- **Scores.** `always_200/writeup/scores.py`: `grounded` (every number in `summary` and
  `first_check` within max(0.05, 1%) of a value-column number, compared as numbers) and
  `culprit_agreement`. A number is a decimal that starts a token: digits glued after a letter
  (`p95`) and minute labels (`t-10`, `t+0`) are not numbers; a unit glued after the digits
  (`412ms`) still counts.
- **Runbooks.** `config/runbooks/{storefront,orders,payments}.md`, five technical lines each.
- **Dependency.** `jsonschema`.
- **Check.** `make test` (or `uv run pytest -q`): 332 tests pass, with no network, Docker or GPU.
