import json

import httpx
import pytest
from jsonschema import Draft202012Validator

from always_200.writeup.llm import OllamaClient
from always_200.writeup.scores import culprit_agreement
from always_200.writeup.writeup import full_prompt, load_prompt, write


class Fake:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc = reply, exc
        self.prompts = []

    def generate(self, prompt, schema):
        self.prompts.append(prompt)
        if self.exc:
            raise self.exc
        return self.reply


# The plan's tests (step 20.1).


def test_valid_output_is_used(INC, CTX, VALID, SCHEMA):
    w = write(INC, CTX, Fake(json.dumps(VALID)), SCHEMA)
    assert w.source == "llm" and w.error is None and w.data == VALID


def test_invalid_json_falls_back(INC, CTX, SCHEMA):
    w = write(INC, CTX, Fake("Sure! Here is the JSON: {"), SCHEMA)
    assert w.source == "template" and w.error == "invalid_json"


def test_service_outside_the_graph_falls_back(INC, CTX, VALID, SCHEMA):  # Review Focus 4
    w = write(INC, CTX, Fake(json.dumps({**VALID, "suspected_service": "billing"})), SCHEMA)
    assert w.source == "template" and w.error == "schema"


def test_extra_fields_fall_back(INC, CTX, VALID, SCHEMA):  # additionalProperties false
    top = {**VALID, "confidence": 0.9}
    nested = {**VALID, "evidence": [{**VALID["evidence"][0], "note": "spike"}]}
    for bad in (top, nested):
        w = write(INC, CTX, Fake(json.dumps(bad)), SCHEMA)
        assert w.source == "template" and w.error == "schema"


def test_overlong_summary_falls_back(INC, CTX, VALID, SCHEMA):  # 5,000 characters
    w = write(INC, CTX, Fake(json.dumps({**VALID, "summary": "x" * 5000})), SCHEMA)
    assert w.source == "template" and w.error == "schema"


def test_timeout_and_unreachable_fall_back(INC, CTX, SCHEMA):
    assert write(INC, CTX, Fake(exc=httpx.ReadTimeout("t")), SCHEMA).error == "timeout"
    assert write(INC, CTX, Fake(exc=httpx.ConnectError("c")), SCHEMA).error == "unreachable"


def test_no_client_means_template(INC, CTX, SCHEMA):  # CI and clean-machine path
    w = write(INC, CTX, None, SCHEMA)
    assert w.source == "template" and w.error is None
    assert Draft202012Validator(SCHEMA).is_valid(w.data)
    assert w.data["suspected_service"] == "payments"


def test_ollama_request_has_temperature_0_seed_42_and_schema(SCHEMA):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "{}"}})

    client = OllamaClient(
        "http://ollama.test:11434/", "some-model", transport=httpx.MockTransport(handler)
    )
    assert client.generate("the prompt", SCHEMA) == "{}"
    (req,) = seen
    assert req.method == "POST" and str(req.url) == "http://ollama.test:11434/api/chat"
    body = json.loads(req.content)
    assert body == {
        "model": "some-model",
        "messages": [{"role": "user", "content": "the prompt"}],
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0, "seed": 42},
    }
    assert req.extensions["timeout"]["read"] == 60.0


# Further tests: more ways to fail, and the rules the plan states in prose.


def test_schema_is_a_valid_draft_2020_12_schema(SCHEMA):
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$schema"].endswith("/draft/2020-12/schema")
    assert set(SCHEMA["required"]) == {"summary", "suspected_service", "evidence", "first_check"}


@pytest.mark.parametrize(
    "change",
    [
        {"evidence": []},
        {"evidence": [{"service": "payments", "signal": "latency_p95", "ts": 0, "value": 1}] * 11},
        {"evidence": [{"service": "payments", "signal": "telemetry_missing", "ts": 0, "value": 1}]},
        {"evidence": [{"service": "payments", "signal": "latency_p95", "ts": 0, "value": "412"}]},
        {"first_check": "y" * 401},
    ],
)
def test_other_schema_breaks_fall_back(INC, CTX, VALID, SCHEMA, change):
    w = write(INC, CTX, Fake(json.dumps({**VALID, **change})), SCHEMA)
    assert w.source == "template" and w.error == "schema"


def test_missing_field_and_non_object_fall_back(INC, CTX, VALID, SCHEMA):
    no_check = {k: v for k, v in VALID.items() if k != "first_check"}
    for reply in (json.dumps(no_check), json.dumps([VALID]), "42"):
        assert write(INC, CTX, Fake(reply), SCHEMA).error == "schema"
    assert write(INC, CTX, Fake(None), SCHEMA).error == "invalid_json"


def test_llm_never_changes_the_culprit(INC, CTX, VALID, SCHEMA):  # Review Focus 4: the assignee
    reply = {**VALID, "suspected_service": "storefront"}
    w = write(INC, CTX, Fake(json.dumps(reply)), SCHEMA)
    assert w.source == "llm" and w.data["suspected_service"] == "storefront"
    assert INC.culprit == "payments"
    assert not culprit_agreement(w.data, INC.culprit)


def test_seconds_come_from_the_injected_clock(INC, CTX, VALID, SCHEMA):
    times = iter([100.0, 102.5])
    w = write(INC, CTX, Fake(json.dumps(VALID)), SCHEMA, clock=lambda: next(times))
    assert w.seconds == 2.5


def test_the_model_gets_the_instruction_then_the_context(INC, CTX, VALID, SCHEMA):
    fake = Fake(json.dumps(VALID))
    write(INC, CTX, fake, SCHEMA)
    assert fake.prompts == [full_prompt(CTX)]
    assert fake.prompts[0].startswith(load_prompt()) and fake.prompts[0].endswith(CTX.text)
    assert "culprit" not in load_prompt().lower()


def test_think_false_is_sent_only_when_asked(SCHEMA):
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "{}"}})

    for think in (None, True, False):
        OllamaClient("http://o", "m", think=think, transport=httpx.MockTransport(handler)).generate(
            "p", SCHEMA
        )
    assert ["think" in b for b in bodies] == [False, False, True]
    assert bodies[2]["think"] is False


def test_ollama_failures_reach_write_as_fallbacks(INC, CTX, SCHEMA):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    cases = [
        (refuse, "unreachable"),
        (slow, "timeout"),
        (lambda r: httpx.Response(500, text="model crashed"), "unreachable"),
        (lambda r: httpx.Response(404, json={"error": "model not found"}), "unreachable"),
        (lambda r: httpx.Response(200, json={"done": True}), "invalid_json"),
        (lambda r: httpx.Response(200, text="not json"), "invalid_json"),
        (lambda r: httpx.Response(200, json={"message": {"content": "{oops"}}), "invalid_json"),
    ]
    for handler, error in cases:
        client = OllamaClient("http://o", "m", transport=httpx.MockTransport(handler))
        w = write(INC, CTX, client, SCHEMA)
        assert (w.source, w.error) == ("template", error), error
