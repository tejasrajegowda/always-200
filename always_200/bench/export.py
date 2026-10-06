"""`make export`: turn a finished recording run into the r2 §6 files, or refuse.

For each seed of the run it writes `<out_root>/<seed>/`:
- `samples.jsonl.gz`: the detector's input, every step of the session (warm-up included);
- `answer_key.jsonl`: the faults and events, derived from the recorded gauges;
- `deploy_markers.jsonl`: every deploy marker the traffic machine set;
- `session.yaml`: what is needed to trust and repeat the recording (seed, timing, graph, commits,
  Prometheus image, run name).
Then it takes one TSDB snapshot for the whole run and copies it to `<out_root>/<run_name>/tsdb/`,
the raw data the PromQL baseline is computed from later (ruling P12).

It refuses, and writes nothing, when:
- the traffic container has not exited with code 0 (the run is not finished, or it crashed);
- a seed's session has a scrape gap over 30 s inside its window (`SessionHoled`, Review Focus 1):
  the laptop slept or Docker restarted, and a holed session must never be scored silently;
- a seed's session does not hold exactly what its schedule promised (`SessionIncomplete`): a failed
  sick-switch call would otherwise shrink the answer key and make detection look easier;
- a seed's output folder already holds files: nothing is ever overwritten.

Only the four detector signals ever reach `samples.jsonl.gz`: the samples come from `QUERIES`
through `assemble()`, and `Sample` itself refuses any other signal (D11).
"""

import json
import math
import re
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

import always_200
from always_200.bench.answer_key import (
    AnswerKey,
    Points,
    derive,
    latest_run,
    scrape_gaps,
    write_key,
)
from always_200.bench.files import write_jsonl, write_samples
from always_200.bench.record import check_names
from always_200.config import REPO_ROOT, load_config
from always_200.core import Sample
from always_200.sources.assemble import assemble
from always_200.sources.prom_api import PromAPI, RawSeries, align
from always_200.sources.queries import QUERIES
from traffic.schedule import SessionPlan, build_session

STEP_S = 15.0
MAX_GAP_S = 30.0
LOOKBACK_S = 7 * 24 * 3600  # how far back a seed's session_seed points are searched
PAD_S = 60  # extra raw history read before a window, so a counter's previous value is known


class SessionHoled(Exception): ...


class SessionIncomplete(Exception): ...


class TrafficNotFinished(Exception): ...


@dataclass
class _Export:
    seed: str
    window: tuple[float, float]
    samples: list[Sample]
    key: AnswerKey
    markers: list[dict]


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))


def _merged(series: RawSeries, lo: float, hi: float) -> Points:
    """All points of all series inside [lo, hi], one per timestamp, oldest first."""
    found: dict[float, float] = {}
    for _labels, points in series:
        for ts, value in points:
            if lo <= ts <= hi:
                found[ts] = value
    return sorted(found.items())


def _grouped(series: RawSeries, keys: tuple[str, ...], lo: float, hi: float) -> dict:
    """Points inside [lo, hi] grouped by the given labels (several series of one group merge)."""
    groups: dict[tuple[str, ...], RawSeries] = {}
    for labels, points in series:
        group = tuple(labels.get(k, "") for k in keys)
        groups.setdefault(group, []).append((labels, points))
    return {group: _merged(members, lo, hi) for group, members in groups.items()}


def find_window(api: PromAPI, seed: str, now: float) -> tuple[float, float]:
    """The LATEST contiguous run of session_seed{seed} == 1 (ruling P12)."""
    points = _merged(api.raw(f'session_seed{{seed="{seed}"}}', now, LOOKBACK_S), -math.inf, now)
    run = latest_run(points)
    if run is None:
        raise SessionIncomplete(f"seed {seed}: session_seed never read 1; nothing was recorded")
    return run[0], run[1]


def check_holes(api: PromAPI, seed: str, start: float, end: float) -> None:
    """Any scrape gap over 30 s on any `up` series or on session_phase -> SessionHoled."""
    span = math.ceil(end - start) + PAD_S
    holes: list[str] = []
    ups = api.raw("up", end, span)
    if not ups:
        raise SessionHoled(f"seed {seed}: no `up` series at all inside the session window")
    for labels, points in ups + api.raw("session_phase", end, span):
        inside = [(ts, v) for ts, v in points if start <= ts <= end]
        name = labels.get("job") or labels.get("__name__", "?")
        if labels.get("service"):
            name += f"/{labels['service']}"
        holes += [
            f"{name}: no scrape from {_utc(a)} to {_utc(b)} ({b - a:.0f} s)"
            for a, b in scrape_gaps(inside, MAX_GAP_S)
        ]
    if holes:
        raise SessionHoled(
            f"seed {seed}: the recording has holes, so it cannot be scored. Record it again "
            "under a new run name.\n  " + "\n  ".join(holes)
        )


def check_complete(key: AnswerKey, plan: SessionPlan) -> None:
    """The key must hold exactly the plan's faults and events, by count, (type, size) and kind."""
    want_faults = Counter((s.fault_type, s.size) for s in plan.slots if s.kind == "fault")
    want_events = Counter(s.event_kind for s in plan.slots if s.kind == "event")
    got_faults = Counter((f.type, f.size) for f in key.faults)
    got_events = Counter(e.kind for e in key.events)
    n_faults, n_events = sum(want_faults.values()), sum(want_events.values())
    problems = []
    if len(key.faults) != n_faults:
        problems.append(f"{len(key.faults)} faults, the schedule has {n_faults}")
    problems += [f"missing fault {t}/{z} x{n}" for (t, z), n in (want_faults - got_faults).items()]
    problems += [
        f"unexpected fault {t}/{z} x{n}" for (t, z), n in (got_faults - want_faults).items()
    ]
    problems += [f"fault {f.id} never ended" for f in key.faults if not f.closed]
    if len(key.events) != n_events:
        problems.append(f"{len(key.events)} events, the schedule has {n_events}")
    problems += [f"missing event {k} x{n}" for k, n in (want_events - got_events).items()]
    problems += [f"unexpected event {k} x{n}" for k, n in (got_events - want_events).items()]
    if problems:
        raise SessionIncomplete(
            f"seed {key.seed}: the answer key does not match the schedule, so it cannot be "
            "scored.\n  " + "\n  ".join(problems)
        )


def marker_rows(series: RawSeries, start: float, end: float) -> list[dict]:
    """One row per increment of deploy_marker_total inside [start, end], by service."""
    rows = []
    for (service,), points in _grouped(series, ("service",), -math.inf, end).items():
        prev: float | None = None
        for ts, value in points:
            if prev is None:
                rise = value if ts > start else 0.0  # a series born inside the window
            else:
                rise = value - prev if value >= prev else value  # a drop is a counter reset
            if start <= ts <= end:
                rows += [{"ts": ts, "service": service}] * round(rise)
            prev = value
    return sorted(rows, key=lambda r: (r["ts"], r["service"]))


def _samples(api: PromAPI, start: float, end: float) -> list[Sample]:
    first = align(start, STEP_S)
    results = {
        signal: api.query_range(expr, first, end, STEP_S) for signal, expr in QUERIES.items()
    }
    steps = [first + i * STEP_S for i in range(math.floor((end - first) / STEP_S) + 1)]
    return list(assemble(results, steps))


def export_seed(api: PromAPI, seed: str, quick: bool, now: float) -> _Export:
    """Every check and every query for one seed, in memory; nothing is written here."""
    start, end = find_window(api, seed, now)
    check_holes(api, seed, start, end)
    samples = _samples(api, start, end)
    span = math.ceil(end - start) + PAD_S
    faults = _grouped(api.raw("fault_active", end, span), ("service", "type", "size"), start, end)
    events = _grouped(api.raw("event_active", end, span), ("kind",), start, end)
    phase = _merged(api.raw("session_phase", end, span), start, end)
    try:
        key = derive(seed, faults, {kind: pts for (kind,), pts in events.items()}, phase)
    except ValueError as err:
        raise SessionIncomplete(f"seed {seed}: {err}") from err
    check_complete(key, build_session(seed, quick))
    markers = marker_rows(api.raw("deploy_marker_total", end, span), start, end)
    return _Export(seed, (start, end), samples, key, markers)


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, cwd=REPO_ROOT)
    except OSError:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _prometheus_image() -> str:
    compose = REPO_ROOT / "compose.yaml"
    if compose.exists():
        found = re.search(
            r"image:\s*[\"']?(\S*prometheus\S*?)[\"']?\s*$", compose.read_text(), re.M
        )
        if found:
            return found.group(1)
    return "unknown"


def session_meta(ex: _Export, run_name: str, quick: bool) -> dict:
    cfg = load_config()
    plan = build_session(ex.seed, quick)
    frozen = REPO_ROOT / "config" / "frozen.yaml"
    return {
        "seed": ex.seed,
        "quick": quick,
        "run_name": run_name,
        "window": {"start": ex.window[0], "end": ex.window[1]},
        "scored_start": ex.key.scored_start,
        "soak_end": ex.key.soak_end,
        "scored_end": ex.key.scored_end,
        "timing": cfg["timing"],
        "plan": {
            "warmup_s": plan.warmup_s,
            "soak_s": plan.soak_s,
            "cooldown_s": plan.cooldown_s,
            "total_s": plan.total_s,
        },
        "graph": cfg["graph"],
        "counts": {
            "samples": len(ex.samples),
            "faults": len(ex.key.faults),
            "events": len(ex.key.events),
            "deploy_markers": len(ex.markers),
        },
        "always_200_version": always_200.__version__,
        "repo_commit": _git("rev-parse", "--short", "HEAD") or "unknown",
        "frozen_yaml_commit": (
            _git("log", "-1", "--format=%h", "--", "config/frozen.yaml")
            if frozen.exists()
            else None
        ),
        "prometheus_image": _prometheus_image(),
    }


def traffic_exit_code() -> int | None:
    """The traffic container's exit code, or None while it runs (or if it never ran)."""
    out = subprocess.run(
        ["docker", "compose", "--profile", "traffic", "ps", "--all", "--format", "json", "traffic"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    rows = json.loads(out) if out.startswith("[") else [json.loads(x) for x in out.splitlines()]
    if not rows or rows[0].get("State") != "exited":
        return None
    return int(rows[0].get("ExitCode", -1))


def copy_snapshot(name: str, dest: Path) -> None:
    """`docker compose cp` the snapshot out of the Prometheus container."""
    subprocess.run(
        ["docker", "compose", "cp", f"prometheus:/prometheus/snapshots/{name}", str(dest)],
        check=True,
    )


def export_run(
    seeds: list[str],
    out_root: Path,
    api: PromAPI,
    run_name: str,
    quick: bool = False,
    *,
    exit_code: Callable[[], int | None] = traffic_exit_code,
    snapshot_to: Callable[[str, Path], None] = copy_snapshot,
    now: float | None = None,
) -> list[Path]:
    """Requires the traffic container to have exited with code 0. For each seed:
    1. its window = latest_run(raw session_seed{seed} points): the LATEST contiguous run of 1s,
       never the span from the first 1 to the last 1 (ruling P12);
    2. scrape_gaps() on every `up` series and on session_phase inside the window -> any gap = refuse
       (raise SessionHoled with the gap times);
    3. QUERIES via query_range over [align(start), end] -> assemble() -> samples.jsonl.gz;
    4. raw fault_active, event_active and session_phase -> derive() -> answer_key.jsonl;
    5. completeness against build_session(seed, quick): the same number of faults (36 full, 3
       quick), all closed; the same number of events (12 full, 0 quick); the same counts per
       (type, size) and per event kind. Any difference = refuse (raise SessionIncomplete naming
       the missing items), so a failed sick-switch call can never shrink the answer key;
    6. raw deploy_marker_total increments -> deploy_markers.jsonl;
    7. session.yaml: seed, timing, graph, repo commit (git rev-parse --short HEAD),
       Prometheus image tag, frozen.yaml commit if present, run_name.
    Then one TSDB snapshot for the run, copied to out_root/<run_name>/tsdb/ (ruling P12).

    Every seed is checked before anything is written, so a refusal leaves no partial files."""
    check_names(seeds, run_name)
    code = exit_code()
    if code != 0:
        state = "is still running (or never ran)" if code is None else f"exited with code {code}"
        raise TrafficNotFinished(f"the traffic container {state}; export needs a finished run")
    out_root = Path(out_root)
    targets = [out_root / seed for seed in seeds]
    tsdb = out_root / run_name / "tsdb"
    taken = [f for f in targets if f.exists() and any(f.iterdir())]
    if tsdb.exists():
        taken.append(tsdb)
    if taken:
        raise FileExistsError(f"{taken[0]} already exists; nothing is ever overwritten")

    moment = time.time() if now is None else now
    exports = [export_seed(api, seed, quick, moment) for seed in seeds]

    for ex, folder in zip(exports, targets, strict=True):
        folder.mkdir(parents=True, exist_ok=True)
        write_samples(folder / "samples.jsonl.gz", ex.samples)
        write_key(folder / "answer_key.jsonl", ex.key)
        write_jsonl(folder / "deploy_markers.jsonl", ex.markers)
        meta = session_meta(ex, run_name, quick)
        (folder / "session.yaml").write_text(
            yaml.safe_dump(meta, sort_keys=False), encoding="utf-8", newline="\n"
        )
    tsdb.parent.mkdir(parents=True, exist_ok=True)
    snapshot_to(api.snapshot(), tsdb)
    return targets
