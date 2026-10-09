"""Tickets: one per incident, filed through a sink (D14, rulings P13 and P14).

base.py is the `TicketSink` interface and the shared ticket text, jsonl.py the default sink (an
append-only log, no network), and flow.py the `TicketFlow` that turns incident events into open,
comment and resolve. `sink_from` picks the sink named in the config's `tickets` section; JSONL is
the default, so nothing is sent anywhere unless the config says so.
"""

import time
from collections.abc import Callable
from pathlib import Path

from always_200.tickets.base import TicketSink
from always_200.tickets.jsonl import JsonlSink

SINKS = ("jsonl",)


def sink_from(
    cfg: dict, root: Path, clock: Callable[[], float] = time.time, context: dict | None = None
) -> TicketSink:
    """The sink the config names (`tickets.sink`, default jsonl), its path relative to `root`."""
    tickets = cfg.get("tickets", {})
    name = tickets.get("sink", "jsonl")
    if name == "jsonl":
        return JsonlSink(root / tickets.get("path", "runs/tickets.jsonl"), clock, context)
    raise ValueError(f"unknown ticket sink {name!r}; known: {', '.join(SINKS)}")
