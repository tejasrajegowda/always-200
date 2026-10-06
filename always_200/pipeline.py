"""The one path every run takes: samples -> detector -> incident builder -> incidents.

Live watching, replay and the bench all go through `run_detector`, so a detector is graded on
exactly the code that would page someone.
"""

from collections.abc import Iterable

from always_200.core import Detector, Sample
from always_200.incidents import Incident, IncidentBuilder


def run_detector(
    samples: Iterable[Sample],
    detector: Detector,
    builder: IncidentBuilder,
    scored_start: float = float("-inf"),
) -> list[Incident]:
    """Ruling P1b: the detector sees every sample (warm-up included, so its band fills);
    the incident builder sees only ticks and anomalies at or after scored_start."""
    last = None
    for s in samples:
        a = detector.update(s)
        if s.ts >= scored_start:
            builder.tick(s.ts)
            if a is not None:
                builder.feed(a)
        last = s.ts
    return builder.finish(last if last is not None else 0.0)
