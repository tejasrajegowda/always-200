# 0001. Repository structure

**Status:** accepted

## Context

always-200 is run in place from its own checkout: a demo shop, a traffic generator and the
detector run as containers under Docker Compose, and the bench replays recorded sessions offline.
It is not published as a library. The layout should follow the standard for that kind of project,
keep the system under test apart from the product, and keep generated data out of version control.

## Decision

| Decision | Content | Why | Source |
|---|---|---|---|
| Artifact type | An application run in place (`python -m always_200 …`), not a library. No build backend. | Nothing here is installed by others; uv's application projects have no build system. | [uv: creating projects](https://docs.astral.sh/uv/concepts/projects/init/) |
| Layout | Flat, no `src/`. | The src layout protects distributed packages; the flat layout runs without installation. | [src layout vs flat layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/) |
| Three top-level packages | `patient/` and `traffic/` are the system under test and its load generator; they live beside the product, not inside it. `always_200/` is the product, with one folder per capability (`sources`, `detect`, `incidents`, `writeup`, `tickets`, `bench`). | The product must not import the system it measures; the top level names what each part does. | [Screaming Architecture](https://blog.cleancoder.com/uncle-bob/2011/09/30/Screaming-Architecture.html) |
| One Docker image | One image for all programs; Compose picks the command. | One dependency set, one build, no drift between services. | [Compose: services](https://docs.docker.com/reference/compose-file/services/) |
| Tests | They mirror the packages under `tests/`. | A file's tests are found by its path. | [pytest: good integration practices](https://docs.pytest.org/en/stable/explanation/goodpractices.html) |
| Name | `always-200` for the repo; `always_200` is the import name. | Hyphens are not valid in Python import names. | [PEP 8: package and module names](https://peps.python.org/pep-0008/#package-and-module-names) |
| What is committed | The repo is code only. Generated data (`recordings/`, `runs/`, `dist/`) is never committed. | Recordings and datasets are large and are published as a release, not as source. | — |
| Commits | Guarded commits through `scripts/commit.py`. | Lint, format, tests and the privacy guard run before every commit; the hooks repeat the guard. | — |
| Environment | uv, with a committed lock. | `uv.lock` pins every version, so CI and every machine get the same environment. | [uv: locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/) |
| Public docs | `docs/decisions/`, `docs/build-log.md`, `docs/scoring-spec.md`. | Decisions, history and the grading rules are public; working notes are not. | — |

## Consequences

- Code runs directly from a checkout; there is no build or install step.
- New capabilities land in their own folder under `always_200/` when their work begins, not in
  advance.
- If the product is ever published as a library, the layout decision should be revisited in favor
  of the src layout.
