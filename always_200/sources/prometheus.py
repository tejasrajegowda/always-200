"""The live source: ask Prometheus for one settled step at a time.

The wall clock decides only when it is time to ask. Every `Sample.ts` is the timestamp on the
value Prometheus returned for that step (D5), so a detector cannot tell a live run from a replay.

Each step runs the four committed queries (`QUERIES`). A series whose metric name is outside the
allow-list is dropped by the client before it can become a number (D11). This module never reads
`fault_active` or the other answer-key gauges.
"""

import math
import sys
import time
from collections.abc import Callable, Iterator

import httpx

from always_200.core import Sample
from always_200.selfmetrics import SelfMetrics
from always_200.sources.assemble import StepResults, assemble
from always_200.sources.prom_api import PromAPI
from always_200.sources.queries import QUERIES


def due_step(now: float, step_s: float, settle_s: float) -> float:
    """The newest step boundary that has had `settle_s` seconds to be scraped.

    Prometheus evaluates a range on boundaries of `step_s` measured from the epoch. Rounding
    down, after leaving `settle_s` for the last scrape to land, is that boundary. It is not
    `now` itself.
    """
    return math.floor((now - settle_s) / step_s) * step_s


class PrometheusSource:
    def __init__(
        self,
        api: PromAPI,
        step_s: float = 15.0,
        settle_s: float = 10.0,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        metrics: SelfMetrics | None = None,
    ) -> None:
        self._api = api
        self._step_s = step_s
        self._settle_s = settle_s
        self._now = now
        self._sleep = sleep
        self._metrics = metrics

    def _wait_until(self, ready_at: float) -> None:
        """Sleep until `ready_at`. A wake that does not move the clock waits again."""
        while self._now() < ready_at:
            self._sleep(ready_at - self._now())

    def _once(self, asked: float) -> tuple[float, StepResults]:
        """Query every signal for exactly `asked`. The returned timestamp is Prometheus's."""
        raw: StepResults = {}
        stamps: set[float] = set()
        for signal, expr in QUERIES.items():
            got = self._api.query_range(expr, asked, asked, self._step_s)
            raw[signal] = got
            for series in got.values():
                stamps.update(series)
        ts = next(iter(stamps)) if len(stamps) == 1 else asked
        results: StepResults = {}
        for signal, by_service in raw.items():
            results[signal] = {}
            for service, series in by_service.items():
                if not series:
                    continue
                value = series[ts] if ts in series else next(iter(series.values()))
                results[signal][service] = {ts: value}
        return ts, results

    def _fetch(self, asked: float) -> tuple[float, StepResults]:
        """Same step again after a transport failure. A bad query is raised, not retried."""
        while True:
            try:
                return self._once(asked)
            except httpx.HTTPError as exc:
                print(
                    f"prometheus query at step {asked:.0f} failed ({exc.__class__.__name__}); "
                    "retrying the same step",
                    file=sys.stderr,
                )
                self._sleep(min(5.0, self._step_s))

    def samples(self) -> Iterator[Sample]:
        """Forever, one step at a time, each step yielded once.

        Starts at the newest step that has already settled, then walks forward by `step_s`.
        A step that is already due is not waited for, so a slow query catches up instead of
        skipping. The lag gauge is `now() - ts`: how late this step's own timestamp is.
        """
        asked = due_step(self._now(), self._step_s, self._settle_s)
        while True:
            self._wait_until(asked + self._settle_s)
            ts, results = self._fetch(asked)
            batch = list(assemble(results, [ts]))
            if self._metrics is not None:
                self._metrics.samples_total.inc(len(batch))
                self._metrics.sample_lag_seconds.set(self._now() - ts)
            yield from batch
            asked += self._step_s
