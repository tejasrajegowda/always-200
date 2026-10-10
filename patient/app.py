"""One patient service: a business route, its metrics, a sick switch and a restart button."""

import asyncio
import dataclasses
import os
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, field_validator

from patient.fault import SIZES, TYPES, Fault
from patient.metrics import PatientMetrics
from patient.recent import RecentLatency
from patient.store import PgStore, Store

ROUTES = {"storefront": "/buy", "orders": "/orders", "payments": "/payments"}
CALLS = {"storefront": "orders", "orders": "payments"}  # caller -> callee, as in the config graph
USES_STORE = ("orders", "payments")


class Purchase(BaseModel):
    customer_id: int
    amount_cents: int


class PaymentRequest(Purchase):
    order_id: int | None = None  # sent by orders; absent when payments is called on its own


class FaultRequest(BaseModel):
    type: str
    size: str
    duration_s: float = Field(gt=0, le=600)

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in TYPES:
            raise ValueError(f"type must be one of {TYPES}")
        return value

    @field_validator("size")
    @classmethod
    def _known_size(cls, value: str) -> str:
        if value not in SIZES:
            raise ValueError(f"size must be one of {tuple(SIZES)}")
        return value


class SickSwitch:
    """The one fault this service may have at a time, and the answer-key gauge that reports it."""

    def __init__(self, service: str, metrics: PatientMetrics, clock: Callable[[], float]) -> None:
        self._service = service
        self._gauge = metrics.fault_active
        self._clock = clock
        self._fault: Fault | None = None
        for type_ in TYPES:  # every series exists from the start, at 0
            for size in SIZES:
                self._gauge.labels(service, type_, size).set(0)

    def current(self) -> Fault | None:
        self.refresh()
        return self._fault

    def refresh(self) -> None:
        """End the fault if its time is up, so the gauge drops to 0 on its own."""
        if self._fault is not None and not self._fault.active(self._clock()):
            self.end()

    def start(self, fault: Fault) -> Fault:
        self._fault = fault
        self._gauge.labels(self._service, fault.type, fault.size).set(1)
        return fault

    def end(self) -> Fault | None:
        fault, self._fault = self._fault, None
        if fault is not None:
            self._gauge.labels(self._service, fault.type, fault.size).set(0)
        return fault


def _as_json(fault: Fault | None) -> dict | None:
    return None if fault is None else dataclasses.asdict(fault)


def create_app(
    service: str,
    store: Store | None,
    downstream: httpx.AsyncClient | None,
    clock: Callable[[], float] = time.monotonic,
    seed: int | None = None,
    exit_fn: Callable[[int], None] = os._exit,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> FastAPI:
    if service not in ROUTES:
        raise ValueError(f"unknown service {service!r}")
    if service in CALLS and downstream is None:
        raise ValueError(f"{service} needs a downstream client for {CALLS[service]}")
    if service in USES_STORE and store is None:
        raise ValueError(f"{service} needs a store")

    route = ROUTES[service]
    metrics = PatientMetrics()
    recent = RecentLatency()
    switch = SickSwitch(service, metrics, clock)
    rng = random.Random(seed)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def tick() -> None:
            while True:
                await asyncio.sleep(1)  # always the real sleep: a fake one would spin this loop
                switch.refresh()

        if isinstance(store, PgStore):
            await store.open()
        task = asyncio.create_task(tick())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if isinstance(store, PgStore):
                await store.close()
            if downstream is not None:
                await downstream.aclose()

    app = FastAPI(title=f"patient: {service}", lifespan=lifespan)
    app.state.metrics = metrics

    @app.middleware("http")
    async def measure_and_apply_fault(request: Request, call_next):
        if request.url.path != route:  # /metrics, /healthz and /admin/* are never measured
            return await call_next(request)
        start = clock()  # before the fault delay, so the delay is inside the measurement
        code = 500  # what is recorded if the handler raises
        try:
            fault = switch.current()
            if fault is not None and fault.fails(start, rng):
                response = JSONResponse({"detail": "injected fault"}, status_code=503)
            else:
                if fault is not None and (delay := fault.extra_delay(start)) > 0:
                    await sleep(delay)
                response = await call_next(request)
            code = response.status_code
            return response
        finally:
            elapsed = clock() - start
            metrics.requests.labels(service, route, str(code)).inc()
            metrics.duration.labels(service, route).observe(elapsed)
            recent.add(elapsed)

    async def call_downstream(body: dict) -> dict:
        """Call the next service; any failure there becomes a 502 here, so faults travel up."""
        target = CALLS[service]
        try:
            r = await downstream.post(ROUTES[target], json=body)
        except httpx.HTTPError as e:  # refused, reset or timed out, e.g. during a restart
            raise HTTPException(502, f"{target} unreachable: {type(e).__name__}") from e
        if r.status_code != 200:
            raise HTTPException(502, f"{target} answered {r.status_code}")
        return r.json()

    if service == "storefront":

        @app.post("/buy")
        async def buy(p: Purchase) -> dict:
            return await call_downstream(p.model_dump())

    elif service == "orders":

        @app.post("/orders")
        async def place_order(p: Purchase) -> dict:
            order_id = await store.create_order(p.customer_id, p.amount_cents)
            await store.recent_orders(p.customer_id)  # the read a real order page would do
            paid = await call_downstream({**p.model_dump(), "order_id": order_id})
            await store.mark_paid(order_id)
            return {"order_id": order_id, "payment_id": paid["payment_id"]}

    else:

        @app.post("/payments")
        async def take_payment(p: PaymentRequest) -> dict:
            return {"payment_id": await store.create_payment(p.order_id, p.amount_cents)}

    @app.post("/admin/fault")
    async def start_fault(req: FaultRequest) -> dict:
        if switch.current() is not None:
            raise HTTPException(409, "a fault is already active")
        fault = Fault(req.type, req.size, clock(), req.duration_s, base_p95_s=recent.p95())
        return _as_json(switch.start(fault))

    @app.get("/admin/fault")
    async def show_fault() -> dict | None:
        return _as_json(switch.current())

    @app.delete("/admin/fault")
    async def end_fault() -> dict | None:
        return _as_json(switch.end())

    @app.post("/admin/restart", status_code=202)
    async def restart(background: BackgroundTasks) -> dict:
        async def exit_soon() -> None:
            await sleep(0.2)  # long enough for the 202 to reach the caller
            exit_fn(0)  # the container's restart policy brings the service back

        background.add_task(exit_soon)
        return {"restarting": True}

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/metrics")
    async def metrics_page() -> Response:
        switch.refresh()  # the answer key is exact at every scrape, traffic or not
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return app
