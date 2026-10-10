"""The Prometheus API client against canned JSON (httpx.MockTransport): no Prometheus needed."""

import math

import httpx
import pytest

from always_200.sources.prom_api import MAX_POINTS, PromAPI, PrometheusError, align


def api_answering(body: dict, status: int = 200, seen: list | None = None) -> PromAPI:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body)

    return PromAPI(transport=httpx.MockTransport(handler))


def matrix(*series: dict) -> dict:
    return {"status": "success", "data": {"resultType": "matrix", "result": list(series)}}


def test_query_range_parses_matrix_and_drops_nan():
    seen: list[httpx.Request] = []
    api = api_answering(
        matrix(
            {"metric": {"service": "orders"}, "values": [[15, "0.08"], [30, "NaN"], [45, "0.09"]]},
            {"metric": {"service": "payments"}, "values": [[15, "+Inf"], [30, "0.02"]]},
        ),
        seen=seen,
    )
    got = api.query_range("some_expr", 15.0, 45.0)
    assert got == {"orders": {15.0: 0.08, 45.0: 0.09}, "payments": {30.0: 0.02}}
    assert all(math.isfinite(v) for values in got.values() for v in values.values())
    params = seen[0].url.params
    assert seen[0].url.path == "/api/v1/query_range"
    assert (params["query"], params["start"], params["end"], params["step"]) == (
        "some_expr",
        "15.0",
        "45.0",
        "15.0",
    )


def test_a_blocked_metric_name_in_a_range_result_is_dropped():
    api = api_answering(
        matrix(
            {"metric": {"service": "orders"}, "values": [[15, "0.02"]]},
            {
                "metric": {"__name__": "fault_active", "service": "orders"},
                "values": [[15, "12345"]],
            },
            {
                "metric": {"__name__": "event_active", "service": "storefront"},
                "values": [[15, "1"]],
            },
        )
    )
    assert api.query_range("latency", 15.0, 15.0) == {"orders": {15.0: 0.02}}


def test_query_range_keeps_a_real_zero():
    api = api_answering(matrix({"metric": {"service": "orders"}, "values": [[15, "0"]]}))
    assert api.query_range("x", 15.0, 15.0) == {"orders": {15.0: 0.0}}


def test_prometheus_error_status_raises():
    bad = {"status": "error", "errorType": "bad_data", "error": "parse error at char 3"}
    with pytest.raises(PrometheusError, match="bad_data: parse error"):
        api_answering(bad, status=400).query_range("rate(", 0.0, 15.0)
    with pytest.raises(PrometheusError, match="bad_data"):
        api_answering(bad, status=200).raw("up", 100.0, 60)


def test_a_non_json_answer_raises():
    api = PromAPI(transport=httpx.MockTransport(lambda r: httpx.Response(502, text="bad gateway")))
    with pytest.raises(httpx.HTTPStatusError):
        api.query_range("up", 0.0, 15.0)


def test_a_series_without_a_service_label_is_refused():
    api = api_answering(matrix({"metric": {"job": "x"}, "values": [[15, "1"]]}))
    with pytest.raises(PrometheusError, match="no service label"):
        api.query_range("up", 15.0, 15.0)


def test_more_than_11000_points_is_refused_before_asking():
    seen: list[httpx.Request] = []
    api = api_answering(matrix(), seen=seen)
    api.query_range("up", 0.0, (MAX_POINTS - 1) * 15.0)  # exactly 11,000 points: allowed
    with pytest.raises(ValueError, match="11000"):
        api.query_range("up", 0.0, MAX_POINTS * 15.0)
    assert len(seen) == 1
    assert 8.8 * 3600 / 15 + 1 < MAX_POINTS  # one full session fits in one query


def test_align_rounds_up_to_step():
    assert align(16.0) == 30.0 and align(30.0) == 30.0
    assert align(0.0) == 0.0 and align(1.0, step=5.0) == 5.0


def test_raw_returns_labels_and_scraped_points():
    seen: list[httpx.Request] = []
    api = api_answering(
        matrix({"metric": {"__name__": "up", "job": "traffic"}, "values": [[5, "1"], [10, "0"]]}),
        seen=seen,
    )
    assert api.raw('session_seed{seed="A1"}', 100.0, 3600) == [
        ({"__name__": "up", "job": "traffic"}, [(5.0, 1.0), (10.0, 0.0)])
    ]
    assert seen[0].url.path == "/api/v1/query"
    assert seen[0].url.params["query"] == 'session_seed{seed="A1"}[3600s]'
    assert seen[0].url.params["time"] == "100.0"


def test_snapshot_posts_and_returns_the_folder_name():
    seen: list[httpx.Request] = []
    api = api_answering(
        {"status": "success", "data": {"name": "20261007T120000Z-1a2b3c"}}, seen=seen
    )
    assert api.snapshot() == "20261007T120000Z-1a2b3c"
    assert (seen[0].method, seen[0].url.path) == ("POST", "/api/v1/admin/tsdb/snapshot")
