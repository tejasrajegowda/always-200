"""Tuning on session A (scoring spec section 10, rulings P9 and P10).

A detector has knobs (the band's k and spread floors, the threshold's factor). Each setting of the
knobs is one **operating point**: replayed over session A it detects some share of the faults and
raises some number of false alarms per clean hour. Turning a knob trades one for the other.

`tune` tries every point of a fixed grid (a **grid search**), scores each one on A with the same
scorer the results table uses, and picks one with the **selection rule written before any result
existed**: the highest detection at <= 0.5 false alarms per clean hour; then lower false alarms;
then the first point in grid order. When no point qualifies, it takes the lowest-false-alarm point
and says plainly that the choice is **infeasible** (Review Focus 3).

Only session A may be tuned on; a session B is refused, because B is the held-out exam. The
outputs are candidates, nothing is frozen here: `config/frozen.yaml` is written once, by hand, at
step 18.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from always_200.bench.answer_key import AnswerKey, read_key
from always_200.bench.files import read_samples
from always_200.bench.report import _grid
from always_200.bench.score import score_session
from always_200.config import graph_from
from always_200.core import Detector, Sample
from always_200.detect.band import BandConfig, BandDetector
from always_200.detect.threshold import ThresholdDetector, fit_thresholds
from always_200.incidents import IncidentBuilder
from always_200.pipeline import run_detector


@dataclass(frozen=True)
class OperatingPoint:
    detector: str
    params: dict
    detection: float
    fa_per_h: float


def select(points: list[OperatingPoint], max_fa_per_h: float = 0.5) -> tuple[OperatingPoint, bool]:
    """Rule P9: highest detection with fa <= max; then lower fa; then grid order. If none qualify,
    return the lowest-fa point and False (infeasible)."""
    if not points:
        raise ValueError("there is no operating point to select from")
    ranked = list(enumerate(points))
    feasible = [(i, p) for i, p in ranked if p.fa_per_h <= max_fa_per_h]
    if feasible:
        _, best = min(feasible, key=lambda ip: (-ip[1].detection, ip[1].fa_per_h, ip[0]))
        return best, True
    # Infeasible: the lowest false-alarm rate; equal rates fall back to detection, then grid order.
    _, best = min(ranked, key=lambda ip: (ip[1].fa_per_h, -ip[1].detection, ip[0]))
    return best, False


BAND_GRID = [
    {"k": k, "latency_floor_frac": f, "error_floor": e}
    for e in (0.0025, 0.005, 0.0075)
    for f in (0.05, 0.10, 0.20)
    for k in (3, 4, 5, 6)
]  # 36 points, P10
THRESHOLD_GRID = [{"factor": x} for x in (1.0, 1.25, 1.5, 2.0)]
SAMPLE_FILES = ("samples.jsonl.gz", "samples.jsonl")


def load_session(session_dir: Path) -> tuple[list[Sample], AnswerKey]:
    """One session's samples (in memory, so the grid replays them without re-reading) and key."""
    key = read_key(session_dir / "answer_key.jsonl")
    path = next((session_dir / n for n in SAMPLE_FILES if (session_dir / n).is_file()), None)
    if path is None:
        raise FileNotFoundError(f"no samples file in {session_dir}")
    return list(read_samples(path)), key


def score_point(samples: list[Sample], key: AnswerKey, detector: Detector, cfg: dict) -> dict:
    """One replay through the shared path; the counts and the two rates of one operating point."""
    graph = graph_from(cfg)
    timing = cfg["timing"]
    builder = IncidentBuilder(
        graph, close_after_s=timing["incident_close_s"], settle_s=timing["settle_s"]
    )
    incidents = run_detector(samples, detector, builder, key.scored_start)
    score = score_session(incidents, key, graph, grace_s=timing["grace_s"])
    if score.clean_hours <= 0:
        raise ValueError(f"session {key.seed} has no clean time to count false alarms in")
    return {
        "detected": len(score.detected),
        "faults": len(key.faults),
        "false_alarms": len(score.false_alarms),
        "clean_hours": score.clean_hours,
        "detection": len(score.detected) / len(key.faults) if key.faults else 0.0,
        "fa_per_h": len(score.false_alarms) / score.clean_hours,
    }


def band_detector(cfg: dict, params: dict) -> BandDetector:
    band = {**cfg["band"], **params}
    return BandDetector(BandConfig.from_config({"band": band, "timing": cfg["timing"]}))


def soak_of(samples: list[Sample], key: AnswerKey) -> list[Sample]:
    """The clean soak, [scored_start, soak_end): what the threshold is fitted on (P5)."""
    return [s for s in samples if key.scored_start <= s.ts < key.soak_end]


def threshold_fit(samples: list[Sample], key: AnswerKey, cfg: dict, factor: float) -> dict:
    th = cfg["threshold"]
    return fit_thresholds(
        soak_of(samples, key),
        factor,
        latency_min_delta_s=float(th["latency_min_delta_s"]),
        error_min_delta=float(th["error_min_delta"]),
        min_requests_1m=float(cfg["band"]["min_requests_1m"]),
    )


def _point(name: str, params: dict, scored: dict) -> OperatingPoint:
    return OperatingPoint(name, dict(params), scored["detection"], scored["fa_per_h"])


def _row(point: OperatingPoint, scored: dict) -> dict:
    return {
        "params": dict(point.params),
        "detection": round(point.detection, 4),
        "fa_per_h": round(point.fa_per_h, 4),
        "detected": scored["detected"],
        "false_alarms": scored["false_alarms"],
    }


def _write(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _table(name: str, points: list[OperatingPoint], chosen: OperatingPoint, ok: bool) -> list[str]:
    knobs = list(points[0].params)
    rows = [
        [
            *(str(p.params[k]) for k in knobs),
            _pct(p.detection),
            f"{p.fa_per_h:.2f}",
            "<- selected" if p is chosen else "",
        ]
        for p in points
    ]
    out = [f"{name} ({len(points)} points)"]
    out += _grid([*knobs, "Detection", "FA per clean h", ""], rows)
    picked = ", ".join(f"{k}={v}" for k, v in chosen.params.items())
    verdict = (
        "Selected"
        if ok
        else "Selected (INFEASIBLE: no point is at or under the FA limit; lowest-FA point, P9)"
    )
    out.append(
        f"{verdict}: {picked}; detection {_pct(chosen.detection)}, "
        f"{chosen.fa_per_h:.2f} FA per clean h"
    )
    return out


def tune(session_dir: Path, cfg: dict, out_dir: Path) -> dict[str, tuple[OperatingPoint, bool]]:
    """Scores every grid point on A only, writes out_dir/<detector>.yaml (candidates + selection)
    and prints the table."""
    samples, key = load_session(Path(session_dir))
    if key.seed.upper().startswith("B"):
        raise ValueError(f"session {key.seed} is held out: tuning uses session A only")
    out_dir = Path(out_dir)
    targets = [out_dir / "band.yaml", out_dir / "threshold.yaml"]
    taken = [str(p) for p in targets if p.exists()]
    if taken:
        raise FileExistsError(f"tune output already exists, choose a new --out: {', '.join(taken)}")
    max_fa = float(cfg["selection"]["max_fa_per_clean_hour"])

    band_scored = [score_point(samples, key, band_detector(cfg, p), cfg) for p in BAND_GRID]
    band_points = [_point("band", p, s) for p, s in zip(BAND_GRID, band_scored, strict=True)]
    fits = [threshold_fit(samples, key, cfg, p["factor"]) for p in THRESHOLD_GRID]
    th_scored = [
        score_point(samples, key, ThresholdDetector.from_config(fit, cfg), cfg) for fit in fits
    ]
    th_points = [_point("threshold", p, s) for p, s in zip(THRESHOLD_GRID, th_scored, strict=True)]

    results: dict[str, tuple[OperatingPoint, bool]] = {
        "band": select(band_points, max_fa),
        "threshold": select(th_points, max_fa),
    }
    first = band_scored[0]
    header = (
        f"Tuning on session {key.seed}: {first['faults']} faults, "
        f"{first['clean_hours']:.2f} clean hours. Rule (P9): highest detection at <= {max_fa} "
        "FA per clean hour, then lower FA, then grid order."
    )
    lines = [header, ""]

    band_pick, band_ok = results["band"]
    sweep = [
        p
        for p in band_points
        if all(p.params[k] == band_pick.params[k] for k in ("latency_floor_frac", "error_floor"))
    ]
    lines += _table("band", band_points, band_pick, band_ok)
    lines += ["", "Band sweep: k at the chosen floors (P10)"]
    lines += [f"  k={p.params['k']}: {_pct(p.detection)}, {p.fa_per_h:.2f} FA/h" for p in sweep]
    lines.append("")
    th_pick, th_ok = results["threshold"]
    lines += _table("threshold", th_points, th_pick, th_ok)
    print("\n".join(lines))

    out_dir.mkdir(parents=True, exist_ok=True)
    common = {"session": key.seed, "max_fa_per_clean_hour": max_fa}
    _write(
        targets[0],
        {
            "detector": "band",
            **common,
            "selected": {
                **_row(band_pick, band_scored[band_points.index(band_pick)]),
                "feasible": band_ok,
            },
            "sweep": [_row(p, band_scored[band_points.index(p)]) for p in sweep],
            "candidates": [_row(p, s) for p, s in zip(band_points, band_scored, strict=True)],
        },
    )
    th_index = th_points.index(th_pick)
    _write(
        targets[1],
        {
            "detector": "threshold",
            **common,
            "selected": {
                **_row(th_pick, th_scored[th_index]),
                "feasible": th_ok,
                "thresholds": {
                    f"{service}/{signal}": round(value, 6)
                    for (service, signal), value in fits[th_index].items()
                },
            },
            "candidates": [_row(p, s) for p, s in zip(th_points, th_scored, strict=True)],
        },
    )
    return results
