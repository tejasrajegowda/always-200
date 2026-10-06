"""ReplaySource: a recorded `samples.jsonl(.gz)` file played back as the `Sample` stream.

It is the offline twin of the live source. A detector cannot tell them apart, which is what makes
the bench's numbers mean something about the live product. The file must already be in stream
order (time, service, signal; ruling P7): a file out of order, or with the same sample twice, is
refused rather than silently sorted, because either means the recording is broken.
"""

from collections.abc import Iterator
from pathlib import Path

from always_200.bench.files import read_samples
from always_200.core import Sample, sample_order


class ReplaySource:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(self.path)

    def samples(self) -> Iterator[Sample]:
        prev: tuple[float, int, int] | None = None
        for line_no, s in enumerate(read_samples(self.path), start=1):
            key = sample_order(s)
            if prev is not None:
                if key == prev:
                    raise ValueError(f"{self.path.name} line {line_no}: duplicate sample")
                if key < prev:
                    raise ValueError(f"{self.path.name} line {line_no}: samples out of order")
            prev = key
            yield s
