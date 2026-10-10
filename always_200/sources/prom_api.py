"""A small client for the Prometheus HTTP API: range queries, raw points and TSDB snapshots.

Three calls are all the recorder and the live source need:
- `query_range(expr, start, end, step)`: Prometheus evaluates `expr` at every step from start to
  end and returns one value per step per series. Here each series is one service, so the result
  is `service -> step timestamp -> value`. NaN and infinity are dropped (Review Focus 2).
- `raw(selector, at, range_s)`: the scraped points themselves, every 5 s, untouched. The answer
  key and the hole check need the real scrape times, not values smoothed onto a 15 s grid.
- `snapshot()`: asks Prometheus to write a consistent copy of its database (needs the admin API,
  `--web.enable-admin-api`) and returns the snapshot's folder name.

Prometheus refuses a range query with more than 11,000 points per series. At a 15 s step that is
about 45.8 h, and one 8.8 h session is 2,112 steps, so a session always fits in one query; a longer
span is refused here with a clear message instead of a vague error from the server.

`transport` lets tests answer with canned JSON (`httpx.MockTransport`), so nothing here needs a
running Prometheus.
"""

import math

import httpx

from always_200.sources.queries import ALLOWED_METRICS

MAX_POINTS = 11_000

Labels = dict[str, str]
RawSeries = list[tuple[Labels, list[tuple[float, float]]]]


class PrometheusError(Exception):
    """Prometheus answered with status "error" (a bad query, a timeout, a refused range)."""


def align(t: float, step: float = 15.0) -> float:
    """The first step boundary at or after t, so every query uses the same grid."""
    return math.ceil(t / step) * step


class PromAPI:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:9090",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._client = httpx.Client(base_url=base_url, transport=transport, timeout=timeout)

    def _data(self, response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            response.raise_for_status()
            raise PrometheusError(
                f"not a Prometheus answer (HTTP {response.status_code})"
            ) from None
        if body.get("status") != "success":
            raise PrometheusError(f"{body.get('errorType', 'error')}: {body.get('error', body)}")
        return body["data"]

    def _get(self, path: str, params: dict) -> dict:
        return self._data(self._client.get(path, params=params))

    def query_range(
        self, expr: str, start: float, end: float, step: float = 15.0
    ) -> dict[str, dict[float, float]]:
        """service -> ts -> value; drops NaN and any series whose `__name__` is outside the
        allow-list; raises on Prometheus 'error' status."""
        points = math.floor((end - start) / step) + 1
        if points > MAX_POINTS:
            raise ValueError(
                f"{points} points per series is over Prometheus's limit of {MAX_POINTS}; "
                "query a shorter span"
            )
        data = self._get(
            "/api/v1/query_range", {"query": expr, "start": start, "end": end, "step": step}
        )
        if data.get("resultType") != "matrix":
            raise PrometheusError(f"expected a matrix, got {data.get('resultType')!r}")
        out: dict[str, dict[float, float]] = {}
        for series in data["result"]:
            metric = series["metric"]
            # `by (service)` drops __name__. A series that still names a metric is raw, and only
            # the allow-list may pass (D11). Dropping it here, before the one-series-per-service
            # check, is what stops a planted answer-key series from replacing the real value.
            # `raw()` is unchanged, so the answer key can still read those gauges.
            name = metric.get("__name__")
            if name is not None and name not in ALLOWED_METRICS:
                continue
            service = metric.get("service")
            if service is None:
                raise PrometheusError(f"a result series has no service label: {metric}")
            if service in out:
                raise PrometheusError(f"two result series for service {service!r}")
            values = ((float(ts), float(v)) for ts, v in series["values"])
            out[service] = {ts: v for ts, v in values if math.isfinite(v)}
        return out

    def raw(self, selector: str, at: float, range_s: int) -> RawSeries:
        """Raw scraped points of a range vector `selector[range_s s]` evaluated at `at`."""
        data = self._get("/api/v1/query", {"query": f"{selector}[{int(range_s)}s]", "time": at})
        if data.get("resultType") != "matrix":
            raise PrometheusError(f"expected a matrix, got {data.get('resultType')!r}")
        return [
            (dict(series["metric"]), [(float(ts), float(v)) for ts, v in series["values"]])
            for series in data["result"]
        ]

    def snapshot(self) -> str:
        """POST /api/v1/admin/tsdb/snapshot -> the snapshot's folder name."""
        return self._data(self._client.post("/api/v1/admin/tsdb/snapshot"))["name"]
