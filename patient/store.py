"""Where the shop keeps orders and payments.

Tests use MemoryStore. The live shop uses PgStore: a Postgres pool of five
connections. Past that, a request waits for a free connection, so a busy shop
gets slower. A large pool would hide that queue.
"""

from typing import Protocol

from psycopg_pool import AsyncConnectionPool


class Store(Protocol):
    async def create_order(self, customer_id: int, amount_cents: int) -> int: ...
    async def recent_orders(self, customer_id: int, limit: int = 5) -> list[int]: ...
    async def mark_paid(self, order_id: int) -> None: ...
    async def create_payment(self, order_id: int | None, amount_cents: int) -> int: ...


class MemoryStore:
    """A Store held in plain Python lists, for tests. It forgets everything when it goes away."""

    def __init__(self) -> None:
        self._orders: list[dict] = []  # an order's id is its position + 1
        self._payments: list[dict] = []

    async def create_order(self, customer_id: int, amount_cents: int) -> int:
        self._orders.append(
            {"customer_id": customer_id, "amount_cents": amount_cents, "paid": False}
        )
        return len(self._orders)

    async def recent_orders(self, customer_id: int, limit: int = 5) -> list[int]:
        ids = [i + 1 for i, o in enumerate(self._orders) if o["customer_id"] == customer_id]
        return ids[::-1][:limit]  # newest first

    async def mark_paid(self, order_id: int) -> None:
        self._orders[order_id - 1]["paid"] = True

    async def create_payment(self, order_id: int | None, amount_cents: int) -> int:
        self._payments.append({"order_id": order_id, "amount_cents": amount_cents})
        return len(self._payments)

    def payments_count(self) -> int:
        return len(self._payments)

    def is_paid(self, order_id: int) -> bool:
        return self._orders[order_id - 1]["paid"]


class PgStore:
    """Orders and payments in Postgres. Call open() from the app lifespan, not here."""

    def __init__(self, conninfo: str) -> None:
        # open=False: opening an async pool in the constructor is deprecated, and
        # there is no event loop yet at import time. max_size=5 is the queue.
        self.pool = AsyncConnectionPool(conninfo, min_size=1, max_size=5, open=False)

    async def open(self) -> None:
        # Wait until one connection is real, so the process is not listening
        # (and therefore not "healthy") before it can write.
        await self.pool.open(wait=True, timeout=30)

    async def close(self) -> None:
        await self.pool.close()

    async def create_order(self, customer_id: int, amount_cents: int) -> int:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "INSERT INTO orders (customer_id, amount_cents) VALUES (%s, %s) RETURNING id",
                    (customer_id, amount_cents),
                )
            ).fetchone()
        if row is None:
            raise RuntimeError("insert returned no order id")
        return int(row[0])

    async def recent_orders(self, customer_id: int, limit: int = 5) -> list[int]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id FROM orders
                WHERE customer_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (customer_id, limit),
            )
            rows = await cursor.fetchall()
        return [int(row[0]) for row in rows]

    async def mark_paid(self, order_id: int) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE orders SET status = 'paid' WHERE id = %s", (order_id,))

    async def create_payment(self, order_id: int | None, amount_cents: int) -> int:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "INSERT INTO payments (order_id, amount_cents) VALUES (%s, %s) RETURNING id",
                    (order_id, amount_cents),
                )
            ).fetchone()
        if row is None:
            raise RuntimeError("insert returned no payment id")
        return int(row[0])
