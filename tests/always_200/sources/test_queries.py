"""The signal expressions stay on the allow-list, including when a service has no errors yet."""

from always_200.sources.queries import ALLOWED_METRICS, BLOCKED_METRICS, QUERIES, metric_names


def test_queries_read_only_allowed_metrics():
    for name, expr in QUERIES.items():
        names = metric_names(expr)
        assert names <= ALLOWED_METRICS, (name, names)
        assert not names & BLOCKED_METRICS


def test_error_ratio_survives_zero_errors():
    assert " or " in QUERIES["error_ratio"] and "* 0" in QUERIES["error_ratio"]
