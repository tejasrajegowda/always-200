"""The command line: `python -m always_200 <command>`. Commands are added step by step.

- `record`: start a detached recording run (`always_200.bench.record`);
- `export`: turn a finished run into recording files, or refuse (`always_200.bench.export`);
- `bench`: replay recordings through detectors, score them and print the results table; with
  `--tickets`, also file one ticket per incident to a JSONL log and print the time to ticket;
- `tune`: score the band and threshold grids on session A and apply the selection rule
  (`always_200.bench.tune`);
- `watch`: read live Prometheus, print one JSON line per incident event, and serve `/metrics`.
"""

import argparse
import json
import sys
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from always_200.bench.answer_key import read_key
from always_200.bench.files import read_jsonl
from always_200.bench.report import render_table, ticket_lines
from always_200.bench.score import score_session, summarise
from always_200.config import REPO_ROOT, graph_from, load_config
from always_200.core import ANOMALY_KINDS, Detector, Sample
from always_200.detect.band import BandConfig, BandDetector
from always_200.detect.trivial import AlwaysDetector, NeverDetector
from always_200.graph import Graph
from always_200.incidents import Incident, IncidentBuilder, IncidentEvent
from always_200.pipeline import run_detector
from always_200.selfmetrics import SelfMetrics
from always_200.sources.replay import ReplaySource
from always_200.tickets.base import SampleClock
from always_200.tickets.flow import TicketFlow, TicketTiming, placeholder_owner
from always_200.tickets.jsonl import JsonlSink
from always_200.writeup.context import build_context, load_runbooks
from always_200.writeup.writeup import WriteUp, load_schema, write

DETECTORS: dict[str, Callable[[], Detector]] = {
    "never": NeverDetector,
    "always": AlwaysDetector,
    "band": lambda: BandDetector(BandConfig.from_config(load_config())),
}
SAMPLES = "samples.jsonl.gz"
DEPLOYS = "deploy_markers.jsonl"
BENCH_TICKETS = Path("runs/bench/tickets.jsonl")
CONTEXT_KEEP_S = 1200.0  # samples kept for a write-up: t-11 min to the settled culprit, and room


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


def _remembered(samples: Iterable[Sample], recent: deque[Sample]) -> Iterator[Sample]:
    """Passes the samples on, keeping the last CONTEXT_KEEP_S seconds of them in `recent`."""
    for s in samples:
        recent.append(s)
        while recent[0].ts < s.ts - CONTEXT_KEEP_S:
            recent.popleft()
        yield s


def template_writer(
    recent: deque[Sample], deploys: list[dict], graph: Graph, runbooks: dict[str, str]
) -> Callable[[Incident], WriteUp]:
    """The bench's writer: always the template (no client), from the samples seen so far."""
    schema = load_schema()

    def writer(incident: Incident) -> WriteUp:
        context = build_context(incident, recent, deploys, graph, runbooks)
        return write(incident, context, None, schema)

    return writer


def bench(recordings: list[Path], detectors: list[str], tickets: Path | None = None) -> str:
    """Replay -> run_detector -> score_session -> summarise -> render_table.

    With `tickets`, every incident also goes through the ticket flow to a JSONL log (rewritten on
    each run: it is derived from the recordings). The write-up is always the template: the bench
    never builds a model client, whatever the config names, so it never opens a socket."""
    cfg = load_config()
    graph = graph_from(cfg)
    timing = cfg["timing"]
    sessions = session_dirs(recordings)
    runbooks = load_runbooks(REPO_ROOT / "config" / "runbooks")
    if tickets is not None:
        tickets.parent.mkdir(parents=True, exist_ok=True)
        tickets.write_text("", encoding="utf-8")
    rows = {}
    times: dict[str, list[TicketTiming]] = {}
    for name in detectors:
        if name not in DETECTORS:
            raise ValueError(f"unknown detector {name!r}; known: {', '.join(DETECTORS)}")
        scores = []
        times[name] = []
        for folder in sessions:
            key = read_key(folder / "answer_key.jsonl")
            builder = IncidentBuilder(
                graph, close_after_s=timing["incident_close_s"], settle_s=timing["settle_s"]
            )
            samples: Iterable[Sample] = ReplaySource(folder / SAMPLES).samples()
            flow = None
            if tickets is not None:
                recent: deque[Sample] = deque()
                samples = _remembered(samples, recent)
                deploys = read_jsonl(folder / DEPLOYS) if (folder / DEPLOYS).is_file() else []
                clock = SampleClock()
                flow = TicketFlow(
                    JsonlSink(tickets, clock, {"detector": name, "session": key.seed}),
                    template_writer(recent, deploys, graph, runbooks),
                    placeholder_owner,
                    clock,
                )
            incidents = run_detector(
                samples,
                DETECTORS[name](),
                builder,
                key.scored_start,
                on_events=flow.handle if flow is not None else None,
            )
            scores.append(score_session(incidents, key, graph, grace_s=timing["grace_s"]))
            if flow is not None:
                times[name] += flow.timings
        rows[name] = summarise(scores)
    if tickets is None:
        return render_table(rows)
    return f"{render_table(rows)}\n{ticket_lines(times)}Tickets written to {tickets}\n"


def event_payload(event: IncidentEvent) -> dict:
    """One incident event as a JSON object. The incident's alert list is not copied in."""
    incident = event.incident
    payload: dict = {
        "type": event.type,
        "ts": event.ts,
        "id": incident.id,
        "kind": incident.kind,
        "opened_at": incident.opened_at,
        "culprit": incident.culprit,
        "closed_at": incident.closed_at,
    }
    if event.alert is not None:
        alert = event.alert
        payload["alert"] = {
            "service": alert.service,
            "signal": alert.signal,
            "value": alert.value,
            "first": alert.first,
        }
    return payload


def _refresh_gauges(metrics: SelfMetrics, builder: IncidentBuilder) -> None:
    """`alerts_open` counts pairs still inside an open incident; `incidents_open` is 0 or 1."""
    open_incidents = [inc for inc in builder.incidents if inc.closed_at is None]
    pairs = {(alert.service, alert.signal) for inc in open_incidents for alert in inc.alerts}
    metrics.alerts_open.set(len(pairs))
    for kind in ANOMALY_KINDS:
        metrics.incidents_open.labels(kind=kind).set(
            sum(inc.kind == kind for inc in open_incidents)
        )


def watch(prometheus_url: str, metrics_port: int = 9200) -> None:
    """PrometheusSource, then the band, then the incident builder. One JSON line per event.

    Sinks are not wired here. The metrics server starts before the first query, so a slow
    Prometheus does not look like a dead watcher.
    """
    from always_200.sources.prom_api import PromAPI
    from always_200.sources.prometheus import PrometheusSource

    cfg = load_config()
    timing = cfg["timing"]
    graph = graph_from(cfg)
    metrics = SelfMetrics()
    metrics.serve(metrics_port)
    builder = IncidentBuilder(
        graph, close_after_s=timing["incident_close_s"], settle_s=timing["settle_s"]
    )
    _refresh_gauges(metrics, builder)
    source = PrometheusSource(
        PromAPI(prometheus_url), step_s=float(timing["step_s"]), metrics=metrics
    )

    def on_events(events: list[IncidentEvent]) -> None:
        _refresh_gauges(metrics, builder)
        for event in events:
            sys.stdout.write(json.dumps(event_payload(event)) + "\n")
            sys.stdout.flush()

    run_detector(
        source.samples(),
        BandDetector(BandConfig.from_config(cfg)),
        builder,
        on_events=on_events,
    )


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
    run_bench.add_argument(
        "--tickets",
        type=Path,
        nargs="?",
        const=BENCH_TICKETS,
        help=f"file one ticket per incident to this JSONL log (default {BENCH_TICKETS})",
    )

    run_tune = commands.add_parser("tune", help="score the tuning grids on session A")
    run_tune.add_argument("--session", type=Path, required=True, help="e.g. recordings/A1")
    run_tune.add_argument("--out", type=Path, default=Path("runs/tune"))

    run_watch = commands.add_parser("watch", help="watch live Prometheus and print incident events")
    run_watch.add_argument("--prometheus", default="http://127.0.0.1:9090")
    run_watch.add_argument("--metrics-port", type=int, default=9200)

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
    elif args.command == "watch":
        watch(args.prometheus, args.metrics_port)
    else:
        sys.stdout.write(bench(args.recordings, args.detectors, args.tickets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
