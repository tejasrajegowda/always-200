import dataclasses

from jsonschema import Draft202012Validator

from always_200.core import Anomaly
from always_200.writeup.context import build_context
from always_200.writeup.scores import culprit_agreement, grounded, numbers_in
from always_200.writeup.template import template_writeup

# The plan's tests (step 20.1).


def test_invented_number_is_not_grounded():
    assert not grounded({"summary": "p95 hit 999 ms", "first_check": ""}, [412.0])


def test_minute_labels_and_runbook_digits_never_ground_a_number(INC, SAMPLES, G, RUNBOOKS):
    ctx = build_context(INC, SAMPLES, [], G, RUNBOOKS)  # holds "t-10" and runbook digits
    assert "t-10" in ctx.text and "10 s" in ctx.text and 10.0 not in ctx.values
    assert not grounded({"summary": "p95 hit 10 ms", "first_check": ""}, ctx.values)
    assert not grounded({"summary": "the pool holds 20", "first_check": ""}, ctx.values)


def test_numbers_are_compared_as_numbers():
    assert grounded({"summary": "p95 rose to 412.0 ms", "first_check": ""}, [412.0])
    assert grounded({"summary": "errors at 4.1%", "first_check": ""}, [4.1])


def test_template_is_always_grounded(INC, SAMPLES, G, RUNBOOKS, SCHEMA):
    gap = Anomaly(INC.opened_at, "orders", "telemetry_missing", "monitoring_gap", 1, 0, 0, True)
    variants = [
        INC,
        dataclasses.replace(INC, culprit="orders"),
        dataclasses.replace(INC, culprit=None),  # not settled yet: the first alerting service
        dataclasses.replace(INC, kind="monitoring_gap", alerts=[gap], culprit="orders"),
    ]
    for inc in variants:
        for samples in (SAMPLES, SAMPLES[: len(SAMPLES) // 3]):
            ctx = build_context(inc, samples, [], G, RUNBOOKS)
            data = template_writeup(inc, ctx.samples)
            assert grounded(data, ctx.values), data
            assert Draft202012Validator(SCHEMA).is_valid(data), data
            assert numbers_in(data["summary"]) or inc.kind == "monitoring_gap"


# Further tests.


def test_the_template_says_the_builders_culprit_with_real_numbers(CTX, INC):
    data = template_writeup(INC, CTX.samples)
    assert data["suspected_service"] == "payments" == INC.culprit
    assert numbers_in(data["summary"]) == [412.0, 50.0]
    assert {"service": "payments", "signal": "latency_p95", "ts": 0, "value": 412.0} in data[
        "evidence"
    ]


def test_with_no_context_cells_the_alerts_are_the_evidence(INC, G, SCHEMA):
    ctx = build_context(INC, [], [], G, {})
    data = template_writeup(INC, ctx.samples)
    assert Draft202012Validator(SCHEMA).is_valid(data) and grounded(data, ctx.values)
    assert [r["service"] for r in data["evidence"]] == ["storefront", "payments"]


def test_tolerance_is_the_larger_of_0_05_and_one_percent():
    assert grounded({"summary": "412 ms", "first_check": ""}, [415.0])  # 1% of 415 = 4.15
    assert not grounded({"summary": "410 ms", "first_check": ""}, [415.0])
    assert grounded({"summary": "0.05%", "first_check": ""}, [0.0])  # the 0.05 floor
    assert not grounded({"summary": "0.1%", "first_check": ""}, [0.0])


def test_first_check_numbers_count_too():
    assert not grounded({"summary": "412 ms", "first_check": "restart within 30 s"}, [412.0])


def test_what_counts_as_a_number():
    text = "p95 at t-10 and t+0 is 412ms, 5xx at 4.1%; range 400-450, delta -3, http2 ok."
    assert numbers_in(text) == [412.0, 5.0, 4.1, 400.0, 450.0, -3.0]


def test_no_numbers_is_grounded():
    assert grounded({"summary": "payments looks slow", "first_check": "check its deploy"}, [])


def test_culprit_agreement():
    assert culprit_agreement({"suspected_service": "payments"}, "payments")
    assert not culprit_agreement({"suspected_service": "orders"}, "payments")
