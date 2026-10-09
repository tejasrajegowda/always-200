import json

import pytest

from always_200.bench.report import ticket_lines
from always_200.config import load_config
from always_200.core import Anomaly
from always_200.incidents import Incident
from always_200.tickets import sink_from
from always_200.tickets.flow import TicketTiming
from always_200.tickets.jsonl import JsonlSink
from always_200.writeup.writeup import WriteUp

FIELDS = {"action", "incident", "ts", "culprit", "assignee", "title", "body", "writeup_source"}


def incident(t0=1000.0):
    alerts = [
        Anomaly(t0, "storefront", "latency_p95", "fault", 0.48, 0.12, 0.01, True),
        Anomaly(t0 + 15, "payments", "latency_p95", "fault", 0.412, 0.05, 0.01, True),
    ]
    return Incident(f"fault-{int(t0)}", "fault", t0, t0 + 15, alerts, "payments")


WRITEUP = WriteUp(
    "template",
    {
        "summary": "payments is the suspected source.",
        "suspected_service": "payments",
        "evidence": [{"service": "payments", "signal": "latency_p95", "ts": 0, "value": 412.0}],
        "first_check": "Check payments first.",
    },
    0.01,
    None,
)


def read(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_default_config_selects_jsonl_sink(tmp_path):
    """Ruling P13: unless the config names another sink, tickets go to a local log."""
    sink = sink_from(load_config(), root=tmp_path)
    assert isinstance(sink, JsonlSink)
    sink.open(incident(), WRITEUP, "owner:payments")
    assert (tmp_path / "runs" / "tickets.jsonl").is_file()


def test_unknown_sink_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown ticket sink 'jira'"):
        sink_from({"tickets": {"sink": "jira"}}, root=tmp_path)


def test_one_line_per_action_with_every_field(tmp_path):
    now = iter([1060.0, 1075.0, 1260.0])
    path = tmp_path / "t.jsonl"
    sink = JsonlSink(path, clock=lambda: next(now))
    inc = incident()
    key = sink.open(inc, WRITEUP, "owner:payments")
    sink.comment(key, inc, "Also alerting: orders latency_p95 at +75 s.")
    inc.closed_at = 1195.0
    sink.resolve(key, inc)
    rows = read(path)
    assert [r["action"] for r in rows] == ["open", "comment", "resolve"]
    assert all(set(r) == FIELDS for r in rows)
    assert [r["ts"] for r in rows] == [1060.0, 1075.0, 1260.0]
    assert {(r["incident"], r["culprit"], r["assignee"]) for r in rows} == {
        ("fault-1000", "payments", "owner:payments")
    }
    assert rows[0]["title"] == "[always-200] payments: latency p95 high"
    assert rows[2]["body"] == "Resolved: no alert since +15 s; closed at +195 s."


def test_opening_body_holds_writeup_alerts_and_evidence(tmp_path):
    path = tmp_path / "t.jsonl"
    JsonlSink(path, clock=lambda: 0.0).open(incident(), WRITEUP, "owner:payments")
    body = read(path)[0]["body"]
    assert body.startswith("payments is the suspected source.\n\nFirst check: Check payments")
    assert "- storefront latency_p95 at +0 s\n- payments latency_p95 at +15 s" in body
    assert "- payments latency_p95 at t+0: 412" in body
    assert body.endswith("Write-up: template")


def test_the_log_is_append_only(tmp_path):
    path = tmp_path / "t.jsonl"
    JsonlSink(path, clock=lambda: 1.0).open(incident(1000.0), WRITEUP, "a")
    first = path.read_text(encoding="utf-8")
    JsonlSink(path, clock=lambda: 2.0).open(incident(2000.0), WRITEUP, "b")
    assert path.read_text(encoding="utf-8").startswith(first)
    assert [r["incident"] for r in read(path)] == ["fault-1000", "fault-2000"]


def test_one_ticket_per_incident(tmp_path):
    sink = JsonlSink(tmp_path / "t.jsonl", clock=lambda: 0.0)
    sink.open(incident(), WRITEUP, "a")
    with pytest.raises(ValueError, match="already has a ticket"):
        sink.open(incident(), WRITEUP, "a")


def test_context_fields_are_added_to_every_line(tmp_path):
    path = tmp_path / "t.jsonl"
    sink = JsonlSink(path, clock=lambda: 0.0, context={"detector": "band", "session": "B1"})
    key = sink.open(incident(), WRITEUP, "a")
    sink.resolve(key, incident())
    assert all((r["detector"], r["session"]) == ("band", "B1") for r in read(path))


def test_time_to_ticket_lines():
    times = {
        "band": [
            TicketTiming("fault-1", "fault", 60.0, 0.002, 0.001),
            TicketTiming("fault-2", "fault", 75.0, 0.004, 0.001),
            TicketTiming("fault-3", "fault", 60.0, 0.003, 0.002),
            TicketTiming("monitoring_gap-4", "monitoring_gap", 60.0, 0.0, 0.001),
        ],
        "never": [],
    }
    text = ticket_lines(times)
    assert text.splitlines() == [
        "Time to ticket (settle + write-up + sink), apart from detection latency:",
        "- band: median 60.0 s (settle 60.0 s, write-up 0.003 s, sink 0.001 s) over 3 tickets;"
        " 1 monitoring-gap ticket",
        "- never: n/a (no fault tickets)",
    ]
