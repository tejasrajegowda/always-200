"""The default sink: an append-only JSON Lines log of ticket actions (D14).

Every action adds one line and nothing is ever rewritten, so the file is the full history of what
the tickets did, readable with any text tool:

    {"action": "open", "incident": "fault-36000", "ts": 36060.0, "culprit": "payments",
     "assignee": "owner:payments", "title": "[always-200] payments: latency p95 high",
     "body": "...", "writeup_source": "template"}

`comment` and `resolve` lines carry the same fields, with the ticket's culprit, assignee, title
and write-up source repeated, so every line can be read on its own. `context` adds fixed fields to
every line (the bench adds the detector and the session). The key of a ticket is its incident id;
one sink files at most one ticket per incident.

It never touches the network, so CI and the bench use it (ruling P13).
"""

import json
import time
from collections.abc import Callable
from pathlib import Path

from always_200.incidents import Incident
from always_200.tickets.base import opening_body, resolve_text, title
from always_200.writeup.writeup import WriteUp


class JsonlSink:
    def __init__(
        self,
        path: Path,
        clock: Callable[[], float] = time.time,
        context: dict | None = None,
    ) -> None:
        self._path = path
        self._clock = clock
        self._context = dict(context or {})
        self._tickets: dict[str, dict] = {}  # key -> the fields repeated on every line

    def _append(self, action: str, key: str, body: str) -> None:
        row = {"action": action, "incident": key, "ts": self._clock(), **self._tickets[key]}
        row["body"] = body
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({**row, **self._context}, allow_nan=False) + "\n")

    def open(self, incident: Incident, writeup: WriteUp, assignee: str) -> str:
        key = incident.id
        if key in self._tickets:
            raise ValueError(f"incident {key} already has a ticket")
        self._tickets[key] = {
            "culprit": incident.culprit,
            "assignee": assignee,
            "title": title(incident),
            "writeup_source": writeup.source,
        }
        self._append("open", key, opening_body(incident, writeup))
        return key

    def comment(self, key: str, incident: Incident, text: str) -> None:
        self._append("comment", key, text)

    def resolve(self, key: str, incident: Incident) -> None:
        self._append("resolve", key, resolve_text(incident))
