"""The command line: `python -m always_200 <command>`. Commands are added step by step.

- `record`: start a detached recording run (`always_200.bench.record`);
- `export`: turn a finished run into recording files, or refuse (`always_200.bench.export`);
- `bench`: replay recordings through detectors, score them and print the results table;
- `tune`: score the band and threshold grids on session A and apply the selection rule
  (`always_200.bench.tune`).
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from always_200.bench.answer_key import read_key
from always_200.bench.report import render_table
from always_200.bench.score import score_session, summarise
from always_200.config import graph_from, load_config
from always_200.core import Detector
from always_200.detect.band import BandConfig, BandDetector
from always_200.detect.trivial import AlwaysDetector, NeverDetector
from always_200.incidents import IncidentBuilder
from always_200.pipeline import run_detector
from always_200.sources.replay import ReplaySource

DETECTORS: dict[str, Callable[[], Detector]] = {
    "never": NeverDetector,
    "always": AlwaysDetector,
    "band": lambda: BandDetector(BandConfig.from_config(load_config())),
}
SAMPLES = "samples.jsonl.gz"


def _seeds(text: str) -> list[str]:
    return [s.strip() for s in text.split(",") if s.strip()]


def session_dirs(paths: list[Path]) -> list[Path]:
    """A path holding samples.jsonl.gz is one session; otherwise each such subfolder is."""
    found: list[Path] = []
    for path in paths:
        if (path / SAMPLES).is_file():
            found.append(path)
        else:
            found += sorted(p.parent for p in path.glob(f"*/{SAMPLES}"))
    if not found:
        raise FileNotFoundError(f"no {SAMPLES} in {', '.join(map(str, paths))}")
    return found


def bench(recordings: list[Path], detectors: list[str]) -> str:
    """Replay -> run_detector -> score_session -> summarise -> render_table."""
    cfg = load_config()
    graph = graph_from(cfg)
    timing = cfg["timing"]
    sessions = session_dirs(recordings)
    rows = {}
    for name in detectors:
        if name not in DETECTORS:
            raise ValueError(f"unknown detector {name!r}; known: {', '.join(DETECTORS)}")
        scores = []
        for folder in sessions:
            key = read_key(folder / "answer_key.jsonl")
            builder = IncidentBuilder(
                graph, close_after_s=timing["incident_close_s"], settle_s=timing["settle_s"]
            )
            incidents = run_detector(
                ReplaySource(folder / SAMPLES).samples(),
                DETECTORS[name](),
                builder,
                key.scored_start,
            )
            scores.append(score_session(incidents, key, graph, grace_s=timing["grace_s"]))
        rows[name] = summarise(scores)
    return render_table(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="always_200")
    commands = parser.add_subparsers(dest="command", required=True)

    record = commands.add_parser("record", help="start a detached recording run")
    export = commands.add_parser("export", help="export a finished run to recording files")
    for sub in (record, export):
        sub.add_argument("--seeds", type=_seeds, required=True, help="comma-separated, e.g. A1")
        sub.add_argument("--run", required=True, help="the run name (names its Prometheus volume)")
        sub.add_argument("--quick", action="store_true", help="the short 3-fault plan")
    export.add_argument("--out", type=Path, default=Path("recordings"))
    export.add_argument("--prometheus", default="http://127.0.0.1:9090")

    run_bench = commands.add_parser("bench", help="replay recordings and print the results table")
    run_bench.add_argument("--recordings", type=Path, nargs="+", required=True)
    run_bench.add_argument("--detectors", type=_seeds, default=["never", "always"])

    run_tune = commands.add_parser("tune", help="score the tuning grids on session A")
    run_tune.add_argument("--session", type=Path, required=True, help="e.g. recordings/A1")
    run_tune.add_argument("--out", type=Path, default=Path("runs/tune"))

    args = parser.parse_args(argv)
    if args.command == "record":
        from always_200.bench.record import start

        start(args.seeds, args.run, args.quick)
    elif args.command == "export":
        from always_200.bench.export import export_run
        from always_200.sources.prom_api import PromAPI

        folders = export_run(args.seeds, args.out, PromAPI(args.prometheus), args.run, args.quick)
        print("Exported: " + ", ".join(str(f) for f in folders))
    elif args.command == "tune":
        from always_200.bench.tune import tune

        tune(args.session, load_config(), args.out)
    else:
        sys.stdout.write(bench(args.recordings, args.detectors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
