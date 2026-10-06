"""The recording file formats: JSON Lines, gzipped when the name ends in `.gz`.

JSON Lines means one JSON object per line. A file can be read one line at a time, appended to, and
inspected with any text tool. Every file is UTF-8 with `\\n` line endings. NaN and infinity are
refused when writing, because they are not valid JSON and must never reach a detector.

A gzipped file is written with its timestamp set to 0 and no file name inside, so the same
content always gives the same bytes (a published dataset can be checked by hash).

`samples.jsonl.gz`, one sample per line, in stream order (time, service, signal):
    {"ts": 1759740000.0, "service": "orders", "signal": "latency_p95", "value": 0.0841}

`answer_key.jsonl`, three line kinds (written and read by `always_200.bench.answer_key`):
    {"kind": "session", "seed": "A1", "scored_start": ..., "soak_end": ..., "scored_end": ...}
    {"kind": "fault", "id": "A1-f07", "target": "orders", "type": "latency_ramp",
     "size": "subtle", "start": ..., "end": ..., "closed": true}
    {"kind": "event", "event": "traffic_spike", "start": ..., "end": ...}

`deploy_markers.jsonl`, one deploy marker per line:
    {"ts": ..., "service": "orders"}

All times are Unix seconds, taken from the scrape timestamps (one clock, scoring spec section 2).
"""

import gzip
import io
import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from always_200.core import Sample


@contextmanager
def _open_write(path: Path) -> Iterator[TextIO]:
    if path.suffix == ".gz":
        with (
            open(path, "wb") as raw,
            gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz,
            io.TextIOWrapper(gz, encoding="utf-8", newline="\n") as text,
        ):
            yield text
    else:
        with open(path, "w", encoding="utf-8", newline="\n") as text:
            yield text


@contextmanager
def _open_read(path: Path) -> Iterator[TextIO]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as text:
            yield text
    else:
        with open(path, encoding="utf-8") as text:
            yield text


def _dump(row: dict) -> str:
    return json.dumps(row, allow_nan=False) + "\n"


def _rows(path: Path) -> Iterator[dict]:
    with _open_read(path) as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def write_samples(path: Path, samples: Iterable[Sample]) -> int:
    """Writes the samples in the order given and returns how many were written."""
    n = 0
    with _open_write(path) as f:
        for s in samples:
            f.write(_dump({"ts": s.ts, "service": s.service, "signal": s.signal, "value": s.value}))
            n += 1
    return n


def read_samples(path: Path) -> Iterator[Sample]:
    """Yields the file's samples lazily; each one is checked by `Sample` as it is built."""
    for row in _rows(path):
        yield Sample(float(row["ts"]), row["service"], row["signal"], float(row["value"]))


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with _open_write(path) as f:
        for row in rows:
            f.write(_dump(row))


def read_jsonl(path: Path) -> list[dict]:
    return list(_rows(path))
