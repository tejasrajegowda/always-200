"""Two deliberately useless detectors, the scorer's first check (r2 §4).

`AlwaysDetector` alerts on every latency and error sample; `NeverDetector` never alerts. Their
scores are known before running them (the first opens one incident that swallows every fault, the
second detects nothing and never false-alarms), so if the scorer gives anything else, the scorer
is wrong.
"""

from always_200.core import Anomaly, Sample

ALERTING_SIGNALS = ("latency_p95", "error_ratio")


class AlwaysDetector:
    def __init__(self) -> None:
        self._fired: set[tuple[str, str]] = set()

    def update(self, sample: Sample) -> Anomaly | None:
        if sample.signal not in ALERTING_SIGNALS:
            return None
        series = (sample.service, sample.signal)
        first = series not in self._fired
        self._fired.add(series)
        return Anomaly(
            sample.ts, sample.service, sample.signal, "fault", sample.value, 0.0, 0.0, first
        )


class NeverDetector:
    def update(self, sample: Sample) -> Anomaly | None:
        return None
