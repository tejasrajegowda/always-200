import gzip
import json

import pytest

from always_200.bench.files import read_jsonl, read_samples, write_jsonl, write_samples
from always_200.core import Sample
from always_200.sources.replay import ReplaySource

STREAM = [
    Sample(15.0, "storefront", "request_rate", 12.5),
    Sample(15.0, "storefront", "latency_p95", 0.0841),
    Sample(15.0, "orders", "error_ratio", 0.0),
    Sample(15.0, "orders", "telemetry_missing", 0.0),
    Sample(30.0, "payments", "telemetry_missing", 1.0),
]


def test_round_trip_through_gzip(tmp_path):
    path = tmp_path / "samples.jsonl.gz"
    assert write_samples(path, STREAM) == len(STREAM)
    with gzip.open(path, "rt", encoding="utf-8") as f:  # it really is gzip
        first = json.loads(f.readline())
    assert first == {"ts": 15.0, "service": "storefront", "signal": "request_rate", "value": 12.5}
    assert list(ReplaySource(path).samples()) == STREAM


def test_round_trip_through_plain_jsonl(tmp_path):
    path = tmp_path / "samples.jsonl"
    write_samples(path, STREAM)
    assert path.read_text(encoding="utf-8").count("\n") == len(STREAM)
    assert list(read_samples(path)) == STREAM


def test_gzip_output_is_byte_identical_across_writes(tmp_path):
    a, b = tmp_path / "a.jsonl.gz", tmp_path / "b.jsonl.gz"
    write_samples(a, STREAM)
    write_samples(b, STREAM)
    assert a.read_bytes() == b.read_bytes()


def test_out_of_order_file_is_refused(tmp_path):
    path = tmp_path / "samples.jsonl.gz"
    write_samples(path, [STREAM[1], STREAM[0]])  # latency before request rate in one step
    with pytest.raises(ValueError, match="out of order"):
        list(ReplaySource(path).samples())


def test_a_duplicate_sample_is_refused(tmp_path):
    path = tmp_path / "samples.jsonl"
    write_samples(path, [STREAM[0], STREAM[0]])
    with pytest.raises(ValueError, match="duplicate"):
        list(ReplaySource(path).samples())


def test_a_forbidden_signal_in_a_file_is_refused(tmp_path):
    path = tmp_path / "samples.jsonl"
    write_jsonl(path, [{"ts": 1.0, "service": "orders", "signal": "fault_active", "value": 1.0}])
    with pytest.raises(ValueError, match="not allowed past the seam"):
        list(ReplaySource(path).samples())


def test_a_missing_file_is_refused_at_once(tmp_path):
    with pytest.raises(FileNotFoundError):
        ReplaySource(tmp_path / "nope.jsonl.gz")


def test_jsonl_rows_round_trip_and_blank_lines_are_ignored(tmp_path):
    path = tmp_path / "deploy_markers.jsonl"
    rows = [{"ts": 100.0, "service": "orders"}, {"ts": 200.0, "service": "payments"}]
    write_jsonl(path, rows)
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert read_jsonl(path) == rows


def test_nan_is_never_written(tmp_path):
    with pytest.raises(ValueError):
        write_jsonl(tmp_path / "x.jsonl", [{"value": float("nan")}])
