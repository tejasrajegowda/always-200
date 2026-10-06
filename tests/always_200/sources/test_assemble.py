from always_200.sources.assemble import assemble


def _healthy(ts: float, **override: dict) -> dict:
    """One step where all three services are up and busy; `override` replaces whole signals."""
    r = {
        "request_rate": {s: {ts: 10.0} for s in ("storefront", "orders", "payments")},
        "latency_p95": {s: {ts: 0.05} for s in ("storefront", "orders", "payments")},
        "error_ratio": {s: {ts: 0.01} for s in ("storefront", "orders", "payments")},
        "up": {s: {ts: 1.0} for s in ("storefront", "orders", "payments")},
    }
    r.update(override)
    return r


def _of(samples, service):
    return [(s.signal, s.value) for s in samples if s.service == service]


def test_step_order_puts_request_rate_first():
    r = {
        "request_rate": {"orders": {15.0: 10.0}},
        "latency_p95": {"orders": {15.0: 0.05}},
        "error_ratio": {"orders": {15.0: 0.0}},
        "up": {"orders": {15.0: 1.0}},
    }
    got = [(s.service, s.signal) for s in assemble(r, [15.0]) if s.service == "orders"]
    assert got == [
        ("orders", "request_rate"),
        ("orders", "latency_p95"),
        ("orders", "error_ratio"),
        ("orders", "telemetry_missing"),
    ]


def test_services_come_in_ruling_p7_order_within_a_step():
    got = [s.service for s in assemble(_healthy(15.0), [15.0]) if s.signal == "request_rate"]
    assert got == ["storefront", "orders", "payments"]


def test_steps_come_out_in_time_order_even_if_asked_out_of_order():
    r = _healthy(15.0)
    for signal in r:
        for service in r[signal]:
            r[signal][service][30.0] = r[signal][service][15.0]
    ts = [s.ts for s in assemble(r, [30.0, 15.0])]
    assert ts == sorted(ts) and ts[0] == 15.0 and ts[-1] == 30.0


def test_nan_p95_is_skipped_not_yielded():
    r = _healthy(15.0, latency_p95={"orders": {15.0: float("nan")}})
    assert _of(assemble(r, [15.0]), "orders") == [
        ("request_rate", 10.0),
        ("error_ratio", 0.01),
        ("telemetry_missing", 0.0),
    ]


def test_up_zero_means_telemetry_missing():
    r = _healthy(15.0)
    r["up"]["orders"][15.0] = 0.0
    assert _of(assemble(r, [15.0]), "orders") == [("telemetry_missing", 1.0)]


def test_missing_scrape_means_telemetry_missing():
    r = _healthy(15.0)
    del r["up"]["orders"]
    assert _of(assemble(r, [15.0]), "orders") == [("telemetry_missing", 1.0)]
    # the other services are untouched
    assert _of(assemble(r, [15.0]), "payments")[-1] == ("telemetry_missing", 0.0)


def test_no_up_series_at_all_means_every_service_is_missing():
    r = _healthy(15.0)
    del r["up"]
    got = list(assemble(r, [15.0]))
    assert [(s.service, s.signal, s.value) for s in got] == [
        ("storefront", "telemetry_missing", 1.0),
        ("orders", "telemetry_missing", 1.0),
        ("payments", "telemetry_missing", 1.0),
    ]


def test_idle_service_with_up_one_is_skipped_not_a_gap():  # ruling P2
    r = {"request_rate": {}, "latency_p95": {}, "error_ratio": {}, "up": {"orders": {15.0: 1.0}}}
    got = [(s.signal, s.value) for s in assemble(r, [15.0]) if s.service == "orders"]
    assert got == [("telemetry_missing", 0.0)]


def test_zero_error_ratio_is_a_sample_of_zero():  # Review Focus 2
    r = _healthy(15.0, error_ratio={"orders": {15.0: 0.0}})
    assert ("error_ratio", 0.0) in _of(assemble(r, [15.0]), "orders")


def test_zero_request_rate_is_still_a_sample():
    r = _healthy(15.0, request_rate={"orders": {15.0: 0.0}})
    assert _of(assemble(r, [15.0]), "orders")[0] == ("request_rate", 0.0)


def test_a_step_with_no_value_for_that_timestamp_is_missing_not_borrowed():
    r = _healthy(15.0)
    assert _of(assemble(r, [30.0]), "orders") == [("telemetry_missing", 1.0)]


def test_unknown_services_in_the_results_are_ignored():
    r = _healthy(15.0)
    r["up"]["postgres"] = {15.0: 1.0}
    assert {s.service for s in assemble(r, [15.0])} == {"storefront", "orders", "payments"}
