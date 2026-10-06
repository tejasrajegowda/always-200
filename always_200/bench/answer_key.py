"""The answer key: which faults and events happened when, derived from the recorded gauges.

The patient sets `fault_active{service, type, size}` to 1 while a fault runs; the traffic machine
sets `event_active{kind}` while an event runs and `session_phase` to 0 (warm-up), 1 (clean soak),
2 (slots) and 3 (finished). These series are recorded beside the samples but never become samples
(D11). This module reads their raw scrape points, `(timestamp, value)` pairs, and turns them into
an `AnswerKey`.

Edge rules (scoring spec section 2): a run starts at the first point reading 1 and ends at the
first later point reading 0, so resolution is one scrape. A run still at 1 on the last point ends
there and is marked `closed=False`. A gauge reads 1 when its value is at least 0.5.

The file format is documented in `always_200.bench.files`.
"""

from dataclasses import dataclass
from pathlib import Path

from always_200.bench.files import read_jsonl, write_jsonl

Points = list[tuple[float, float]]  # (timestamp, value), one per scrape


@dataclass(frozen=True)
class Fault:
    id: str
    type: str
    target: str
    size: str
    start: float
    end: float
    closed: bool


@dataclass(frozen=True)
class Event:
    kind: str
    start: float
    end: float


@dataclass(frozen=True)
class AnswerKey:
    seed: str
    scored_start: float
    soak_end: float
    scored_end: float
    faults: tuple[Fault, ...]
    events: tuple[Event, ...]


def on_intervals(points: Points) -> list[tuple[float, float, bool]]:
    """Runs of value 1: start = first point reading 1, end = first later point reading 0.
    A run still at 1 on the last point ends there with closed=False."""
    runs: list[tuple[float, float, bool]] = []
    start: float | None = None
    ordered = sorted(points)
    for ts, value in ordered:
        on = value >= 0.5
        if on and start is None:
            start = ts
        elif not on and start is not None:
            runs.append((start, ts, True))
            start = None
    if start is not None:
        runs.append((start, ordered[-1][0], False))
    return runs


def latest_run(points: Points) -> tuple[float, float, bool] | None:
    """The last of on_intervals(points): a seed's session window is the LATEST contiguous run of
    session_seed == 1 (ruling P12), never the span from the first 1 to the last 1."""
    runs = on_intervals(points)
    return runs[-1] if runs else None


def _first_ts(points: Points, test, name: str) -> float:
    for ts, value in sorted(points):
        if test(value):
            return ts
    raise ValueError(f"session_phase never reached {name}; the session is not complete")


def derive(
    seed: str,
    fault_series: dict[tuple[str, str, str], Points],
    event_series: dict[str, Points],
    phase_points: Points,
) -> AnswerKey:
    """Builds the key from one session's gauge points.

    `fault_series` is keyed by (service, type, size), the labels of `fault_active`;
    `event_series` by event kind. The caller passes only points inside the seed's session window.
    Faults are numbered `<seed>-f01`, `-f02`, ... by start time.
    """
    scored_start = _first_ts(phase_points, lambda v: v >= 1, "the clean soak (1)")
    soak_end = _first_ts(phase_points, lambda v: v >= 2, "the slots (2)")
    scored_end = _first_ts(phase_points, lambda v: v == 3, "the end (3)")

    found = [
        (start, end, closed, service, ftype, size)
        for (service, ftype, size), points in fault_series.items()
        for start, end, closed in on_intervals(points)
    ]
    faults = tuple(
        Fault(f"{seed}-f{n:02d}", ftype, service, size, start, end, closed)
        for n, (start, end, closed, service, ftype, size) in enumerate(sorted(found), start=1)
    )
    events = tuple(
        Event(kind, start, end)
        for start, end, kind in sorted(
            (start, end, kind)
            for kind, points in event_series.items()
            for start, end, _closed in on_intervals(points)
        )
    )
    return AnswerKey(seed, scored_start, soak_end, scored_end, faults, events)


def scrape_gaps(points: Points, max_gap_s: float = 30.0) -> list[tuple[float, float]]:
    """Every pair of neighbouring scrapes more than `max_gap_s` apart (Review Focus 1).

    A recording with any such hole inside its session window must not be scored."""
    ts = sorted(t for t, _ in points)
    return [(a, b) for a, b in zip(ts, ts[1:], strict=False) if b - a > max_gap_s]


def write_key(path: Path, key: AnswerKey) -> None:
    rows: list[dict] = [
        {
            "kind": "session",
            "seed": key.seed,
            "scored_start": key.scored_start,
            "soak_end": key.soak_end,
            "scored_end": key.scored_end,
        }
    ]
    rows += [
        {
            "kind": "fault",
            "id": f.id,
            "target": f.target,
            "type": f.type,
            "size": f.size,
            "start": f.start,
            "end": f.end,
            "closed": f.closed,
        }
        for f in key.faults
    ]
    rows += [{"kind": "event", "event": e.kind, "start": e.start, "end": e.end} for e in key.events]
    write_jsonl(path, rows)


def read_key(path: Path) -> AnswerKey:
    sessions: list[dict] = []
    faults: list[Fault] = []
    events: list[Event] = []
    for row in read_jsonl(path):
        kind = row.get("kind")
        if kind == "session":
            sessions.append(row)
        elif kind == "fault":
            faults.append(
                Fault(
                    row["id"],
                    row["type"],
                    row["target"],
                    row["size"],
                    float(row["start"]),
                    float(row["end"]),
                    bool(row["closed"]),
                )
            )
        elif kind == "event":
            events.append(Event(row["event"], float(row["start"]), float(row["end"])))
        else:
            raise ValueError(f"{path.name}: unknown line kind {kind!r}")
    if len(sessions) != 1:
        raise ValueError(f"{path.name}: needs exactly one session line, found {len(sessions)}")
    s = sessions[0]
    return AnswerKey(
        s["seed"],
        float(s["scored_start"]),
        float(s["soak_end"]),
        float(s["scored_end"]),
        tuple(faults),
        tuple(events),
    )
