"""The service's own recent latency, so a fault can be sized against it (ruling P6)."""

from collections import deque


class RecentLatency:
    def __init__(self, size: int = 500) -> None:
        self._values: deque[float] = deque(maxlen=size)  # the oldest value drops out when full

    def add(self, seconds: float) -> None:
        self._values.append(seconds)

    def p95(self, default: float = 0.05) -> float:
        """The 95th percentile of the recent values; `default` until the first request."""
        if not self._values:
            return default
        ordered = sorted(self._values)
        return ordered[int(0.95 * (len(ordered) - 1))]
