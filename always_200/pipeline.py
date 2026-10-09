"""The one path every run takes: samples -> detector -> incident builder -> incidents.

Live watching, replay and the bench all go through `run_detector`, so a detector is graded on
exactly the code that would page someone.
"""

from collections.abc import Callable, Iterable

from always_200.core import Detector, Sample
from always_200.incidents import Incident, IncidentBuilder, IncidentEvent


def run_detector(
    samples: Iterable[Sample],
    detector: Detector,
    builder: IncidentBuilder,
    scored_start: float = float("-inf"),
    on_events: Callable[[list[IncidentEvent]], None] | None = None,
) -> list[Incident]:
    """Ruling P1b: the detector sees every sample (warm-up included, so its band fills);
    the incident builder sees only ticks and anomalies at or after scored_start.
    `on_events` (the ticket flow, step 23) gets the builder's events after each sample."""
    last = None
    for s in samples:
        a = detector.update(s)
        if s.ts >= scored_start:
            events = builder.tick(s.ts)
            if a is not None:
                events += builder.feed(a)
            if on_events is not None and events:
                on_events(events)
        last = s.ts
    return builder.finish(last if last is not None else 0.0)
