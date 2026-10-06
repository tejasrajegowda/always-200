from dataclasses import dataclass, field

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from patient.app import create_app
from patient.store import MemoryStore


class FakeClock:
    """Monotonic time that moves only when a test (or the fake sleep) moves it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Chain:
    clock: FakeClock
    store: MemoryStore
    storefront: TestClient
    orders: TestClient
    payments: TestClient
    exits: list[int] = field(default_factory=list)

    @staticmethod
    def sample(client: TestClient, name: str, **labels: str) -> float:
        """One sample from a service's /metrics page, or 0.0 if it is not there."""
        for family in text_string_to_metric_families(client.get("/metrics").text):
            for s in family.samples:
                if s.name == name and s.labels == labels:
                    return s.value
        return 0.0

    def payments_duration_sum(self) -> float:
        return self.sample(
            self.payments,
            "http_request_duration_seconds_sum",
            service="payments",
            route="/payments",
        )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def chain(clock: FakeClock) -> Chain:
    store = MemoryStore()
    exits: list[int] = []

    async def fake_sleep(seconds: float) -> None:
        clock.advance(seconds)

    def build(service: str, downstream: httpx.AsyncClient | None, seed: int):
        return create_app(
            service,
            store if service != "storefront" else None,
            downstream,
            clock=clock,
            seed=seed,
            exit_fn=exits.append,
            sleep=fake_sleep,
        )

    def client_for(app) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    payments = build("payments", None, seed=3)
    orders = build("orders", client_for(payments), seed=2)
    storefront = build("storefront", client_for(orders), seed=1)
    return Chain(
        clock=clock,
        store=store,
        storefront=TestClient(storefront),
        orders=TestClient(orders),
        payments=TestClient(payments),
        exits=exits,
    )
