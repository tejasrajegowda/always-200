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
