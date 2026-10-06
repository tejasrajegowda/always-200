"""Export against a fake Prometheus (httpx.MockTransport): no Docker, no network.

`FakeProm` holds raw scrape points (one every 5 s) for the gauges and `up`, answers raw range
queries from them, and answers the four `QUERIES` with steady values. It models one piece of
PromQL on purpose: the 5xx counter of a healthy service does not exist, so a plain
`5xx / all` query returns no series, while the "or * 0" form returns 0.

A quick session (seed "dev": warm-up 600 s, soak 300 s, 3 severe faults) keeps every test fast.
"""

import gzip
import json
import re

import httpx
import pytest
import yaml

from always_200.__main__ import bench, main
from always_200.bench.answer_key import AnswerKey, Event, Fault, read_key
from always_200.bench.export import (
    SessionHoled,
    SessionIncomplete,
    TrafficNotFinished,
    check_complete,
    export_run,
    marker_rows,
)
from always_200.bench.files import read_jsonl
from always_200.bench.score import SIZES, score_session
from always_200.config import graph_from, load_config
from always_200.core import SERVICES, SIGNALS
from always_200.detect.trivial import AlwaysDetector, NeverDetector
from always_200.incidents import IncidentBuilder
from always_200.pipeline import run_detector
from always_200.sources.prom_api import PromAPI, align
from always_200.sources.queries import QUERIES
from always_200.sources.replay import ReplaySource
from traffic.schedule import build_session

T0 = 1_759_740_000.0  # a multiple of 15 s
STEADY = {"request_rate": 10.0, "latency_p95": 0.05, "up": 1.0}


class FakeProm:
    def __init__(self) -> None:
        self.series: dict[tuple[str, frozenset], list[tuple[float, float]]] = {}
        self.snapshots = 0

    def add(self, name: str, labels: dict, points) -> None:
        self.series.setdefault((name, frozenset(labels.items())), []).extend(points)

    def drop(self, name: str, labels: dict, lo: float, hi: float) -> None:
        """Remove the scrapes strictly between lo and hi: a hole of hi - lo seconds."""
        key = (name, frozenset(labels.items()))
        self.series[key] = [(t, v) for t, v in self.series[key] if not lo < t < hi]

    def _raw(self, query: str, at: float) -> list[dict]:
        m = re.fullmatch(r"(\w+)(?:\{(.*)\})?\[(\d+)s\]", query)
        assert m, query
        name, matchers, range_s = m.group(1), m.group(2) or "", int(m.group(3))
        want = dict(re.findall(r'(\w+)="([^"]*)"', matchers))
        out = []
        for (n, labels), points in self.series.items():
            labels = dict(labels)
            if n != name or any(labels.get(k) != v for k, v in want.items()):
                continue
            inside = [[t, str(v)] for t, v in sorted(points) if at - range_s < t <= at]
            if inside:
                out.append({"metric": {"__name__": n, **labels}, "values": inside})
        return out

    def _range(self, expr: str, start: float, end: float, step: float) -> list[dict]:
        signal = {v: k for k, v in QUERIES.items()}.get(expr, "error_ratio")
        if signal == "error_ratio":
            if " or " not in expr:
                return []  # 5xx / all with no 5xx series: no result at all
            value = 0.0  # (5xx or all * 0) / all = 0
        else:
            value = STEADY[signal]
        steps = [start + i * step for i in range(int((end - start) // step) + 1)]
        return [
            {"metric": {"service": s}, "values": [[t, str(value)] for t in steps]} for s in SERVICES
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        p = request.url.params
        if request.url.path == "/api/v1/admin/tsdb/snapshot":
            self.snapshots += 1
            return httpx.Response(200, json={"status": "success", "data": {"name": "snap-1"}})
        if request.url.path == "/api/v1/query":
            result = self._raw(p["query"], float(p["time"]))
        else:
            result = self._range(p["query"], float(p["start"]), float(p["end"]), float(p["step"]))
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": result}}
        )

    def api(self) -> PromAPI:
        return PromAPI(transport=httpx.MockTransport(self.handler))


def scrapes(start: float, end: float) -> list[float]:
    return [start + 5 * i for i in range(int((end - start) // 5) + 1)]


def record_session(prom: FakeProm, seed: str, t0: float, quick: bool = True) -> float:
    """Adds one full session's raw points from t0; returns its last scrape time."""
    plan = build_session(seed, quick)
    ts = scrapes(t0, t0 + plan.total_s + 30)

    def phase(t: float) -> float:
        if t < t0 + plan.warmup_s:
            return 0.0
        if t < t0 + plan.warmup_s + plan.soak_s:
            return 1.0
        return 2.0 if t < t0 + plan.total_s else 3.0

    prom.add("session_seed", {"seed": seed}, [(t, 1.0) for t in ts])
    prom.add("session_phase", {"job": "traffic"}, [(t, phase(t)) for t in ts])
    prom.add("up", {"job": "traffic"}, [(t, 1.0) for t in ts])
    for s in SERVICES:
        prom.add("up", {"job": "patient", "service": s}, [(t, 1.0) for t in ts])
    markers: dict[str, list[float]] = {}
    for slot in plan.slots:
        if slot.kind == "fault":
            a, b = t0 + slot.start_s, t0 + slot.start_s + slot.duration_s
            labels = {"service": slot.target, "type": slot.fault_type, "size": slot.size}
            prom.add("fault_active", labels, [(t, 1.0 if a <= t < b else 0.0) for t in ts])
            if slot.deploy_marker_s is not None:
                markers.setdefault(slot.target, []).append(t0 + slot.deploy_marker_s)
        else:
            a, b = t0 + slot.start_s, t0 + slot.start_s + slot.duration_s
            prom.add(
                "event_active", {"kind": slot.event_kind}, [(t, float(a <= t < b)) for t in ts]
            )
    for service, times in markers.items():
        prom.add(
            "deploy_marker_total",
            {"service": service},
            [(t, float(sum(1 for m in times if m <= t))) for t in ts],
        )
    return ts[-1]


def copy_fake(name: str, dest) -> None:
    dest.mkdir()
    (dest / "meta.json").write_text(json.dumps({"snapshot": name}))


def run(prom: FakeProm, tmp_path, seeds=("dev",), quick=True, now=None, exit_code=0):
    return export_run(
        list(seeds),
        tmp_path / "recordings",
        prom.api(),
        "dev",
        quick,
        exit_code=lambda: exit_code,
        snapshot_to=copy_fake,
        now=now,
    )


@pytest.fixture
def prom():
    fake = FakeProm()
    fake.end = record_session(fake, "dev", T0)
    return fake


def test_export_writes_the_three_files_and_session_yaml(prom, tmp_path):
    folders = run(prom, tmp_path, now=prom.end + 10)
    out = tmp_path / "recordings" / "dev"
    assert folders == [out]
    names = {"samples.jsonl.gz", "answer_key.jsonl", "deploy_markers.jsonl", "session.yaml"}
    assert {p.name for p in out.iterdir()} == names | {"tsdb"}  # run name = seed name here

    plan = build_session("dev", quick=True)
    key = read_key(out / "answer_key.jsonl")
    assert key.scored_start == T0 + plan.warmup_s
    assert key.soak_end == T0 + plan.warmup_s + plan.soak_s
    assert [(f.target, f.type, f.size) for f in key.faults] == [
        (s.target, s.fault_type, s.size) for s in plan.slots
    ]
    assert all(f.closed for f in key.faults) and key.events == ()

    expected = sorted(
        (min(t for t in scrapes(T0, prom.end) if t >= T0 + s.deploy_marker_s), s.target)
        for s in plan.slots
        if s.deploy_marker_s is not None
    )
    got = [(r["ts"], r["service"]) for r in read_jsonl(out / "deploy_markers.jsonl")]
    assert got == expected

    meta = yaml.safe_load((out / "session.yaml").read_text(encoding="utf-8"))
    assert meta["seed"] == "dev" and meta["run_name"] == "dev" and meta["quick"] is True
    assert meta["timing"]["step_s"] == 15 and meta["graph"]["storefront"] == ["orders"]
    assert meta["window"] == {"start": T0, "end": prom.end}
    assert {"repo_commit", "prometheus_image", "frozen_yaml_commit"} <= set(meta)
    assert meta["counts"]["faults"] == 3

    assert prom.snapshots == 1
    assert json.loads((out / "tsdb" / "meta.json").read_text()) == {"snapshot": "snap-1"}


def test_samples_cover_every_step_of_the_window(prom, tmp_path):
    run(prom, tmp_path, now=prom.end + 10)
    samples = list(ReplaySource(tmp_path / "recordings" / "dev" / "samples.jsonl.gz").samples())
    steps = sorted({s.ts for s in samples})
    assert steps[0] == align(T0) and steps[-1] <= prom.end
    assert all(b - a == 15.0 for a, b in zip(steps, steps[1:], strict=False))
    assert len(samples) == len(steps) * len(SERVICES) * len(SIGNALS)


def test_export_never_writes_answer_key_series_into_samples(prom, tmp_path):
    # the mock also returns fault_active points; samples.jsonl.gz must contain only SIGNALS
    run(prom, tmp_path, now=prom.end + 10)
    path = tmp_path / "recordings" / "dev" / "samples.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    assert {r["signal"] for r in rows} == set(SIGNALS)
    text = json.dumps(rows)
    for name in ("fault_active", "event_active", "deploy_marker", "session_phase", "session_seed"):
        assert name not in text


def test_absent_5xx_numerator_still_gives_error_ratio_zero(prom, tmp_path, monkeypatch):
    run(prom, tmp_path, now=prom.end + 10)
    samples = list(ReplaySource(tmp_path / "recordings" / "dev" / "samples.jsonl.gz").samples())
    errors = [s for s in samples if s.signal == "error_ratio"]
    assert errors and all(s.value == 0.0 for s in errors)
    assert {s.service for s in errors} == set(SERVICES)

    # the naive form loses the healthy services' error signal entirely: why "or * 0" exists
    naive = "sum by (service) (rate(http_requests_total{code=~'5..'}[1m])) / sum by (service) (x)"
    monkeypatch.setitem(QUERIES, "error_ratio", naive)
    run(prom, tmp_path / "naive", now=prom.end + 10)
    path = tmp_path / "naive" / "recordings" / "dev" / "samples.jsonl.gz"
    assert not [s for s in ReplaySource(path).samples() if s.signal == "error_ratio"]


def test_export_refuses_a_session_with_a_45s_scrape_gap(prom, tmp_path):
    hole = T0 + 1500.0
    prom.drop("up", {"job": "patient", "service": "orders"}, hole, hole + 45)
    with pytest.raises(SessionHoled, match=r"patient/orders: no scrape .* \(45 s\)"):
        run(prom, tmp_path, now=prom.end + 10)
    assert not (tmp_path / "recordings").exists()  # nothing written


def test_a_30s_gap_is_still_allowed(prom, tmp_path):
    prom.drop("session_phase", {"job": "traffic"}, T0 + 1500.0, T0 + 1530.0)
    run(prom, tmp_path, now=prom.end + 10)


def test_two_runs_of_one_seed_on_one_volume_export_the_latest(tmp_path):
    # session_seed{seed="A1"} reads 1, then 0 for an hour, then 1 again: the window is the second
    # run only, and the hour between the runs is not treated as a scrape gap (ruling P12)
    prom = FakeProm()
    first = scrapes(T0, T0 + 600)  # an aborted first try: 10 minutes, then the stack stopped
    prom.add("session_seed", {"seed": "dev"}, [(t, 1.0) for t in first])
    prom.add("session_phase", {"job": "traffic"}, [(t, 0.0) for t in first])
    prom.add("up", {"job": "traffic"}, [(t, 1.0) for t in first])
    gap = scrapes(T0 + 605, T0 + 4200)
    prom.add("session_seed", {"seed": "dev"}, [(t, 0.0) for t in gap])  # no `up` in that hour
    t2 = T0 + 4205.0
    end = record_session(prom, "dev", t2)

    run(prom, tmp_path, now=end + 10)
    out = tmp_path / "recordings" / "dev"
    key = read_key(out / "answer_key.jsonl")
    assert key.scored_start == t2 + 600.0
    samples = list(ReplaySource(out / "samples.jsonl.gz").samples())
    assert samples[0].ts == align(t2) and min(s.ts for s in samples) >= t2


def test_export_refuses_a_session_missing_a_fault(prom, tmp_path):
    plan = build_session("dev", quick=True)
    lost = plan.slots[1]
    labels = {"service": lost.target, "type": lost.fault_type, "size": lost.size}
    prom.series.pop(("fault_active", frozenset(labels.items())))
    with pytest.raises(SessionIncomplete, match=f"missing fault {lost.fault_type}/severe x1"):
        run(prom, tmp_path, now=prom.end + 10)
    assert not (tmp_path / "recordings").exists()


def _key_from_plan(seed: str, drop: int | None = None, resize: int | None = None) -> AnswerKey:
    """The key a perfect recording of `seed` would give; optionally one fault dropped or resized."""
    plan = build_session(seed)
    faults = []
    for i, s in enumerate(f for f in plan.slots if f.kind == "fault"):
        if i == drop:
            continue
        size = SIZES[(SIZES.index(s.size) + 1) % 3] if i == resize else s.size
        end = s.start_s + s.duration_s
        faults.append(Fault(f"{seed}-f{i:02d}", s.fault_type, s.target, size, s.start_s, end, True))
    events = tuple(
        Event(s.event_kind, s.start_s, s.start_s + s.duration_s)
        for s in plan.slots
        if s.kind == "event"
    )
    return AnswerKey(seed, 600.0, 4200.0, plan.total_s, tuple(faults), events)


def test_a_full_session_with_35_closed_faults_is_incomplete():
    plan = build_session("A1")
    check_complete(_key_from_plan("A1"), plan)  # the full 36 + 12 passes
    with pytest.raises(SessionIncomplete, match="35 faults, the schedule has 36"):
        check_complete(_key_from_plan("A1", drop=7), plan)


def test_export_refuses_a_wrong_type_or_size_mix(prom, tmp_path):
    plan = build_session("A1")
    with pytest.raises(SessionIncomplete, match="unexpected fault"):
        check_complete(_key_from_plan("A1", resize=3), plan)  # 36 faults, one size wrong

    slot = build_session("dev", quick=True).slots[0]
    old = {"service": slot.target, "type": slot.fault_type, "size": "severe"}
    points = prom.series.pop(("fault_active", frozenset(old.items())))
    prom.add("fault_active", {**old, "size": "moderate"}, points)
    with pytest.raises(SessionIncomplete, match=f"unexpected fault {slot.fault_type}/moderate"):
        run(prom, tmp_path, now=prom.end + 10)


def test_a_fault_still_on_at_the_end_is_refused():
    key = _key_from_plan("A1")
    f = key.faults[0]
    still_on = Fault(f.id, f.type, f.target, f.size, f.start, f.end, closed=False)
    unclosed = AnswerKey(
        key.seed,
        key.scored_start,
        key.soak_end,
        key.scored_end,
        (still_on, *key.faults[1:]),
        key.events,
    )
    with pytest.raises(SessionIncomplete, match="never ended"):
        check_complete(unclosed, build_session("A1"))


def test_a_session_that_never_finished_is_refused(tmp_path):
    prom = FakeProm()
    end = record_session(prom, "dev", T0)
    key = ("session_phase", frozenset({"job": "traffic"}.items()))
    prom.series[key] = [(t, min(v, 2.0)) for t, v in prom.series[key]]  # phase never reaches 3
    with pytest.raises(SessionIncomplete, match="never reached the end"):
        run(prom, tmp_path, now=end + 10)


def test_export_waits_for_the_traffic_container(prom, tmp_path):
    with pytest.raises(TrafficNotFinished, match="still running"):
        run(prom, tmp_path, now=prom.end + 10, exit_code=None)
    with pytest.raises(TrafficNotFinished, match="code 1"):
        run(prom, tmp_path, now=prom.end + 10, exit_code=1)


def test_export_never_overwrites(prom, tmp_path):
    run(prom, tmp_path, now=prom.end + 10)
    before = (tmp_path / "recordings" / "dev" / "samples.jsonl.gz").read_bytes()
    with pytest.raises(FileExistsError):
        run(prom, tmp_path, now=prom.end + 10)
    assert (tmp_path / "recordings" / "dev" / "samples.jsonl.gz").read_bytes() == before


def test_unknown_seed_is_refused(prom, tmp_path):
    with pytest.raises(SessionIncomplete, match="never read 1"):
        run(prom, tmp_path, seeds=("B9",), now=prom.end + 10)


def test_deploy_marker_increments_and_counter_reset():
    points = [(0.0, 0.0), (5.0, 1.0), (10.0, 1.0), (15.0, 0.0), (20.0, 1.0), (25.0, 3.0)]
    series = [({"service": "orders"}, points)]
    rows = marker_rows(series, start=0.0, end=30.0)
    assert [r["ts"] for r in rows] == [5.0, 20.0, 25.0, 25.0]
    assert marker_rows(series, start=12.0, end=30.0)[0]["ts"] == 20.0  # before the window: no


def test_export_then_bench_prints_the_trivial_table(prom, tmp_path, capsys):
    """The 11.3 check, offline: never = 0% and 0 FA; always = 0%, 3 swallowed, 1 false alarm."""
    run(prom, tmp_path, now=prom.end + 10)
    out = tmp_path / "recordings" / "dev"
    cfg = load_config()
    graph = graph_from(cfg)
    key = read_key(out / "answer_key.jsonl")
    scores = {}
    for name, detector in (("never", NeverDetector()), ("always", AlwaysDetector())):
        builder = IncidentBuilder(graph)
        incidents = run_detector(
            ReplaySource(out / "samples.jsonl.gz").samples(), detector, builder, key.scored_start
        )
        scores[name] = score_session(incidents, key, graph)
    assert not scores["never"].detected and not scores["never"].false_alarms
    assert not scores["always"].detected
    assert scores["always"].info["swallowed"] == 3
    assert len(scores["always"].false_alarms) == 1

    table = bench([out], ["never", "always"])
    rows = {line.split()[0]: re.split(r"\s{2,}", line) for line in table.splitlines()[3:5]}
    assert rows["never"][1].startswith("0.0% (0.0-") and rows["never"][2] == "0"
    assert rows["never"][3] == "n/a / n/a" and rows["never"][-1] == "n/a"
    assert rows["always"][1].startswith("0.0% (0.0-") and rows["always"][2] == "3"

    assert main(["bench", "--recordings", str(out), "--detectors", "never,always"]) == 0
    assert capsys.readouterr().out == table
