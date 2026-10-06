import time

import pytest
from fastapi.testclient import TestClient

from patient.app import create_app
from patient.store import MemoryStore

BUY = {"customer_id": 7, "amount_cents": 1299}


def test_buy_flows_through_all_three_services(chain):
    r = chain.storefront.post("/buy", json=BUY)
    assert r.status_code == 200
    assert chain.store.payments_count() == 1


def test_a_finished_order_is_marked_paid(chain):
    order_id = chain.storefront.post("/buy", json=BUY).json()["order_id"]
    assert chain.store.is_paid(order_id)


def test_error_fault_on_payments_surfaces_upstream_as_5xx(chain):
    chain.payments.post("/admin/fault", json={"type": "errors", "size": "severe", "duration_s": 60})
    codes = [
        chain.storefront.post("/buy", json={"customer_id": 1, "amount_cents": 1}).status_code
        for _ in range(200)
    ]
    assert 40 <= sum(c >= 500 for c in codes) <= 120  # ~40%
    assert set(codes) <= {200, 502}  # payments says 503; each caller above says 502


def test_fault_gauge_is_one_during_and_zero_after(chain):
    chain.orders.post(
        "/admin/fault", json={"type": "latency_step", "size": "subtle", "duration_s": 30}
    )
    line = 'fault_active{service="orders",size="subtle",type="latency_step"}'
    assert f"{line} 1.0" in chain.orders.get("/metrics").text
    chain.clock.advance(31)
    assert f"{line} 0.0" in chain.orders.get("/metrics").text
    assert chain.orders.get("/admin/fault").json() is None


def test_every_fault_series_exists_at_zero_before_any_fault(chain):
    text = chain.payments.get("/metrics").text
    assert text.count('fault_active{service="payments"') == 9  # 3 types x 3 sizes
    assert 'fault_active{service="payments",size="severe",type="errors"} 0.0' in text


def test_latency_fault_delay_is_inside_the_histogram(chain):
    # severe step on payments; base p95 is the 0.05 s default, so the extra delay is 0.2 s.
    # The fake sleep advances the fake clock, so the delay is "real" in the app's own time.
    before = chain.payments_duration_sum()
    chain.payments.post(
        "/admin/fault", json={"type": "latency_step", "size": "severe", "duration_s": 60}
    )
    for _ in range(10):
        chain.payments.post("/payments", json={"customer_id": 1, "amount_cents": 1})
    assert chain.payments_duration_sum() - before == pytest.approx(10 * 0.2, rel=0.05)


def test_fault_size_is_relative_to_the_recent_p95(chain):
    for _ in range(20):  # no fault yet: every request takes 0 s on the fake clock
        chain.payments.post("/payments", json={"customer_id": 1, "amount_cents": 1})
    body = chain.payments.post(
        "/admin/fault", json={"type": "latency_step", "size": "severe", "duration_s": 60}
    ).json()
    assert body["base_p95_s"] == 0.0


def test_second_fault_while_active_is_refused(chain):
    fault = {"type": "errors", "size": "subtle", "duration_s": 60}
    assert chain.payments.post("/admin/fault", json=fault).status_code == 200
    assert chain.payments.post("/admin/fault", json=fault).status_code == 409


def test_a_new_fault_is_allowed_once_the_last_one_ended(chain):
    fault = {"type": "errors", "size": "subtle", "duration_s": 60}
    chain.payments.post("/admin/fault", json=fault)
    chain.clock.advance(61)
    assert chain.payments.post("/admin/fault", json=fault).status_code == 200


def test_delete_ends_the_fault_now(chain):
    chain.orders.post("/admin/fault", json={"type": "errors", "size": "severe", "duration_s": 60})
    assert chain.orders.get("/admin/fault").json()["type"] == "errors"
    assert chain.orders.delete("/admin/fault").status_code == 200
    assert chain.orders.get("/admin/fault").json() is None
    assert (
        'fault_active{service="orders",size="severe",type="errors"} 0.0'
        in chain.orders.get("/metrics").text
    )


@pytest.mark.parametrize(
    "body",
    [
        {"type": "latency_spike", "size": "severe", "duration_s": 60},
        {"type": "errors", "size": "huge", "duration_s": 60},
        {"type": "errors", "size": "severe", "duration_s": 0},
        {"type": "errors", "size": "severe", "duration_s": 601},
    ],
)
def test_bad_fault_type_is_rejected(chain, body):
    assert chain.payments.post("/admin/fault", json=body).status_code == 422
    assert chain.payments.get("/admin/fault").json() is None


def test_admin_and_metrics_routes_are_not_measured(chain):
    chain.storefront.post("/buy", json=BUY)
    chain.storefront.get("/healthz")
    chain.storefront.get("/admin/fault")
    chain.storefront.get("/metrics")
    text = chain.storefront.get("/metrics").text
    routes = {
        line.split('route="')[1].split('"')[0]
        for line in text.splitlines()
        if line.startswith("http_") and 'route="' in line
    }
    assert routes == {"/buy"}


def test_requests_are_counted_by_status_code(chain):
    chain.storefront.post("/buy", json=BUY)
    chain.storefront.post("/buy", json={"customer_id": "not a number"})
    labels = {"service": "storefront", "route": "/buy"}
    assert chain.sample(chain.storefront, "http_requests_total", code="200", **labels) == 1
    assert chain.sample(chain.storefront, "http_requests_total", code="422", **labels) == 1


def test_histogram_has_the_declared_buckets(chain):
    chain.payments.post("/payments", json={"customer_id": 1, "amount_cents": 1})
    assert 'le="0.015"' in chain.payments.get("/metrics").text


def test_restart_calls_exit_after_responding(chain):
    r = chain.orders.post("/admin/restart")
    assert r.status_code == 202
    assert chain.exits == [0]


def test_healthz(chain):
    assert chain.payments.get("/healthz").json() == {"ok": True}


def test_a_service_without_what_it_needs_is_refused():
    with pytest.raises(ValueError, match="downstream"):
        create_app("storefront", None, None)
    with pytest.raises(ValueError, match="store"):
        create_app("payments", None, None)
    with pytest.raises(ValueError, match="unknown service"):
        create_app("billing", MemoryStore(), None)


def test_the_ticker_ends_a_fault_with_no_traffic(clock):
    # No request and no scrape after the fault ends: only the 1 s background ticker can
    # bring the gauge back to 0. Read the registry directly, because /metrics refreshes itself.
    app = create_app("payments", MemoryStore(), None, clock=clock)
    with TestClient(app) as client:  # "with" runs the lifespan, which starts the ticker
        client.post("/admin/fault", json={"type": "errors", "size": "subtle", "duration_s": 5})
        clock.advance(6)
        labels = {"service": "payments", "type": "errors", "size": "subtle"}
        registry = app.state.metrics.registry
        deadline = time.monotonic() + 3.0
        while registry.get_sample_value("fault_active", labels) != 0.0:
            assert time.monotonic() < deadline, "the ticker never reset the gauge"
            time.sleep(0.05)
