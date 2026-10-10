"""Process entry for one patient service.

SERVICE picks the role: storefront, orders or payments.
DATABASE_URL is read for orders and payments only.
DOWNSTREAM_URL is the next service: storefront calls orders, orders calls payments.

One process, one worker. Several workers would each keep their own metrics, so a
scrape would see only one worker's share of the traffic:

    uvicorn patient.main:app --host 0.0.0.0 --port 8000 --workers 1
"""

import os

import httpx

from patient.app import CALLS, USES_STORE, create_app
from patient.store import PgStore


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


_service = _env("SERVICE")
_store = PgStore(_env("DATABASE_URL")) if _service in USES_STORE else None
# 60s, longer than httpx's 5s default. Waiting for a free database connection
# has to show up as latency. A short timeout would record that wait as an error.
_downstream = (
    httpx.AsyncClient(base_url=_env("DOWNSTREAM_URL"), timeout=60.0) if _service in CALLS else None
)

app = create_app(_service, _store, _downstream)
