"""The declared dependency graph: which service calls which.

The culprit rule picks the most downstream alerting service (`depth`), and the scorer accepts an
alert on the target service or on anything upstream of it (`upstream`). Both come from here.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Graph:
    calls: dict[str, tuple[str, ...]]  # caller -> callees

    def __post_init__(self) -> None:
        for caller, callees in self.calls.items():
            for callee in callees:
                if callee not in self.calls:
                    raise ValueError(f"{caller} calls {callee}, which is not a service")
        for service in self.calls:
            self._check_no_cycle(service, ())

    def _check_no_cycle(self, service: str, path: tuple[str, ...]) -> None:
        if service in path:
            raise ValueError(f"the graph has a cycle: {' -> '.join((*path, service))}")
        for callee in self.calls[service]:
            self._check_no_cycle(callee, (*path, service))

    def _callers(self, service: str) -> list[str]:
        if service not in self.calls:
            raise KeyError(service)
        return [caller for caller, callees in self.calls.items() if service in callees]

    def depth(self, service: str) -> int:
        """Longest path from a root (a service nobody calls) to `service`."""
        callers = self._callers(service)
        return 1 + max(self.depth(c) for c in callers) if callers else 0

    def upstream(self, service: str) -> frozenset[str]:
        """Every service that can reach `service` through calls."""
        found: set[str] = set()
        todo = self._callers(service)
        while todo:
            caller = todo.pop()
            if caller not in found:
                found.add(caller)
                todo += self._callers(caller)
        return frozenset(found)
