import json
import socket

import pytest

import always_200.__main__ as cli
from always_200.bench.answer_key import AnswerKey, Fault, write_key
from always_200.bench.files import write_samples
from always_200.bench.report import CULPRIT_RULE
from always_200.config import load_config
from always_200.core import SERVICES, SIGNALS, Anomaly, Sample
from always_200.graph import Graph
from always_200.incidents import IncidentBuilder
from always_200.tickets.base import SampleClock
from always_200.tickets.flow import TicketFlow, placeholder_owner, title
from always_200.tickets.jsonl import JsonlSink
from always_200.writeup import llm
from always_200.writeup.template import template_writeup
from always_200.writeup.writeup import WriteUp

G = Graph({"storefront": ("orders",), "orders": ("payments",), "payments": ()})


def A(ts, svc, sig="latency_p95", kind="fault"):
    return Anomaly(ts, svc, sig, kind, 0.412, 0.05, 0.01, False)


def template(inc):
    return WriteUp("template", template_writeup(inc, []), 0.25, None)


class Rig:
    """A builder, a JSONL sink on a sample clock and the flow, fed one alert or tick at a time."""

    def __init__(self, tmp_path, writer=template, timer=None):
        self.path = tmp_path / "tickets.jsonl"
        self.builder = IncidentBuilder(G)
        clock = SampleClock()
        extra = {} if timer is None else {"timer": timer}
        self.sink = JsonlSink(self.path, clock)
        self.flow = TicketFlow(self.sink, writer, placeholder_owner, clock, **extra)

    def feed(self, *alerts):
        for a in alerts:
            self.flow.handle(self.builder.feed(a))
        return self

    def tick(self, now):
        self.flow.handle(self.builder.tick(now))
        return self

    def lines(self):
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines()]

    def actions(self):
        return [r["action"] for r in self.lines()]


# The plan's tests (step 23.1).


def test_one_incident_one_ticket_joins_become_comments(tmp_path):
    rig = Rig(tmp_path).feed(A(0, "payments")).tick(60)
    rig.feed(A(75, "orders"), A(90, "orders"), A(105, "storefront", "error_ratio"))
    rig.feed(A(120, "payments"))  # a repeat of a pair already in the ticket: nothing new
    assert rig.actions() == ["open", "comment", "comment"]
    lines = rig.lines()
    assert {r["incident"] for r in lines} == {"fault-0"}
    assert lines[1]["body"] == "Also alerting: orders latency_p95 at +75 s."
    assert lines[2]["body"] == "Also alerting: storefront error_ratio at +105 s."


def test_join_before_settle_is_in_the_body_not_a_comment(tmp_path):
    rig = Rig(tmp_path).feed(A(0, "storefront"), A(30, "payments")).tick(60)
    assert rig.actions() == ["open"]
    body = rig.lines()[0]["body"]
    assert "- storefront latency_p95 at +0 s" in body
    assert "- payments latency_p95 at +30 s" in body
    assert rig.lines()[0]["culprit"] == "payments"


def test_resolve_after_three_quiet_minutes(tmp_path):
    rig = Rig(tmp_path).feed(A(0, "payments")).tick(60).tick(179)
    assert rig.actions() == ["open"]
    rig.tick(180)
    assert rig.actions() == ["open", "resolve"]
    resolve = rig.lines()[1]
    assert resolve["ts"] == 180.0  # sample time, from the clock the flow moves
    assert resolve["body"] == "Resolved: no alert since +0 s; closed at +180 s."


def test_later_alert_on_same_culprit_opens_a_new_ticket(tmp_path):
    rig = Rig(tmp_path).feed(A(0, "payments")).tick(60).tick(180)
    rig.feed(A(200, "payments")).tick(260)
    lines = rig.lines()
    assert [r["action"] for r in lines] == ["open", "resolve", "open"]
    assert lines[0]["incident"] != lines[2]["incident"]
    assert lines[2]["culprit"] == "payments"


def test_assignee_comes_from_incident_culprit_never_from_llm(tmp_path):
    """Review Focus 4: the model suspects storefront; the builder's culprit is payments."""

    def model(inc):
        data = {**template_writeup(inc, []), "suspected_service": "storefront"}
        return WriteUp("llm", data, 1.5, None)

    rig = Rig(tmp_path, writer=model).feed(A(0, "storefront"), A(15, "payments")).tick(60)
    opened = rig.lines()[0]
    assert opened["culprit"] == "payments"
    assert opened["assignee"] == "owner:payments"
    assert opened["title"] == "[always-200] payments: latency p95 high"
    assert opened["writeup_source"] == "llm"
    assert rig.builder.incidents[0].culprit == "payments"  # the flow never changed it


def test_monitoring_gap_gets_its_own_ticket_kind(tmp_path):
    def writer(inc):
        assert inc.kind == "fault", "a monitoring gap never asks the writer"
        return template(inc)

    rig = Rig(tmp_path, writer=writer)
    rig.feed(A(0, "orders", "telemetry_missing", "monitoring_gap"), A(15, "payments")).tick(75)
    lines = rig.lines()
    assert [r["action"] for r in lines] == ["open", "open"]
    gap, fault = lines
    assert gap["title"] == "[always-200] orders: monitoring gap"
    assert gap["assignee"] == "owner:orders" and gap["writeup_source"] == "template"
    assert "Telemetry from orders went missing" in gap["body"]
    assert fault["title"] == "[always-200] payments: latency p95 high"


def test_bench_opens_no_socket(monkeypatch, tmp_path, capsys):
    """With recordings present and a model named in the config, the bench touches no socket and
    builds no model client: it prints the table, files template write-ups and the time to ticket.
    (The LLM rows from a saved outputs.jsonl arrive with the LLM run, a later step.)"""
    folder = _session(tmp_path / "rec")
    with_model = {**load_config(), "llm": {"model": "any-model", "url": "http://127.0.0.1:11434"}}
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: with_model)
    monkeypatch.setitem(cli.DETECTORS, "spike", PaymentsSpike)
    monkeypatch.setattr(
        llm.OllamaClient, "__init__", lambda *a, **k: (_ for _ in ()).throw(AssertionError("llm"))
    )
    monkeypatch.setattr(
        socket, "socket", lambda *a, **k: (_ for _ in ()).throw(AssertionError("socket"))
    )
    out = tmp_path / "runs" / "tickets.jsonl"
    argv = ["bench", "--recordings", str(folder), "--detectors", "never,spike", "--tickets"]
    assert cli.main([*argv, str(out)]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith(CULPRIT_RULE)
    assert "Detection (95% CI)" in printed
    assert "- never: n/a (no fault tickets)" in printed
    assert "- spike: median 60.0 s (settle 60.0 s" in printed
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert [r["action"] for r in lines] == ["open", "resolve"]
    assert {r["writeup_source"] for r in lines} == {"template"}
    assert {(r["detector"], r["session"]) for r in lines} == {("spike", "A1")}
    assert lines[0]["ts"] == 465.0  # alert from 405 s, plus the 60 s settle
    assert lines[0]["assignee"] == "owner:payments"


# More checks of the flow.


def test_alert_at_the_settle_step_is_a_comment_not_in_the_body(tmp_path):
    rig = Rig(tmp_path).feed(A(0, "payments"))
    rig.feed(A(60, "orders"))  # feed ticks to 60 first: the culprit is final before this alert
    assert rig.actions() == ["open", "comment"]
    assert "orders" not in rig.lines()[0]["body"].split("Alerts before")[1].split("Evidence")[0]


def test_time_to_ticket_has_three_parts(tmp_path):
    clock = iter([10.0, 10.004])
    rig = Rig(tmp_path, timer=lambda: next(clock)).feed(A(0, "payments")).tick(60)
    (t,) = rig.flow.timings
    assert (t.incident, t.kind, t.settle_s, t.writeup_s) == ("fault-0", "fault", 60.0, 0.25)
    assert t.sink_s == pytest.approx(0.004)
    assert t.total_s == pytest.approx(60.254)


def test_title_names_the_culprits_signal():
    inc = IncidentBuilder(G)
    inc.feed(A(0, "storefront", "latency_p95"))
    inc.feed(A(15, "payments", "error_ratio"))
    inc.tick(60)
    assert title(inc.incidents[0]) == "[always-200] payments: error ratio high"


def test_no_ticket_events_no_lines(tmp_path):
    rig = Rig(tmp_path).feed(A(0, "payments")).tick(45)  # culprit not final yet
    assert rig.actions() == []


# A tiny recorded session for the bench test.


class PaymentsSpike:
    """Alerts on payments latency from 400 s to 500 s (steps 405 to 495), then never again."""

    def update(self, s):
        if s.service == "payments" and s.signal == "latency_p95" and 400 <= s.ts <= 500:
            return Anomaly(s.ts, s.service, s.signal, "fault", 0.4, 0.05, 0.01, s.ts == 400)
        return None


def _session(folder):
    folder.mkdir(parents=True)
    samples = [
        Sample(float(ts), svc, sig, 0.0 if sig == "telemetry_missing" else 0.05)
        for ts in range(0, 901, 15)
        for svc in SERVICES
        for sig in SIGNALS
    ]
    write_samples(folder / "samples.jsonl.gz", samples)
    fault = Fault("A1-f01", "latency_step", "payments", "severe", 390.0, 480.0, True)
    write_key(folder / "answer_key.jsonl", AnswerKey("A1", 60.0, 300.0, 900.0, (fault,), ()))
    return folder
