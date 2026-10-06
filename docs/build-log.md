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
