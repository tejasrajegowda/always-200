"""Where the shop keeps orders and payments. Tests use MemoryStore; PgStore arrives in step 4."""

from typing import Protocol


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
