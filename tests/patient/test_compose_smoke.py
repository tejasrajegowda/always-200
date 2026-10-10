"""The real three-service shop, through Docker, against real Postgres.

Skipped unless pytest is run with `-m docker` (`make smoke`). The default run,
and CI, select `-m 'not docker'` and never start this test.
"""

import time

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

pytestmark = pytest.mark.docker

STOREFRONT = "http://127.0.0.1:8001"
ORDERS = "http://127.0.0.1:8002"
PAYMENTS = "http://127.0.0.1:8003"
BUY = {"customer_id": 7, "amount_cents": 1299}


def _total(text: str, name: str, **labels: str) -> float:
    total = 0.0
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == name and all(
                sample.labels.get(key) == value for key, value in labels.items()
            ):
                total += sample.value
    return total


def test_buy_reaches_payments_and_metrics_count_it():
    deadline = time.monotonic() + 10
    with httpx.Client(timeout=10.0) as client:
        last: object = "no response"
        while True:
            try:
                first = client.post(f"{STOREFRONT}/buy", json=BUY)
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            else:
                if first.status_code == 200:
                    break
                last = first.status_code
            if time.monotonic() >= deadline:
                pytest.fail(f"the first buy did not return 200 within 10 s (last: {last})")
            time.sleep(0.5)

        for _ in range(10):
            response = client.post(f"{STOREFRONT}/buy", json=BUY)
            assert response.status_code == 200, response.text

        for base in (STOREFRONT, ORDERS, PAYMENTS):
            page = client.get(f"{base}/metrics")
            assert page.status_code == 200
            assert "http_request_duration_seconds_bucket" in page.text

        # code="200" on the payments service: one per successful buy above.
        paid = _total(client.get(f"{PAYMENTS}/metrics").text, "http_requests_total", code="200")
        assert paid >= 10

        # No order_id: the column is nullable, so a direct call still succeeds.
        alone = client.post(f"{PAYMENTS}/payments", json={"customer_id": 1, "amount_cents": 50})
        assert alone.status_code == 200, alone.text
