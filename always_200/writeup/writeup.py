"""One write-up per incident: the model's if it behaves, the template's otherwise (D7, D12).

The model writes; it never decides. `write` never touches the incident: the culprit, and so the
ticket's assignee, always come from the incident builder. The model's `suspected_service` is only
compared with the culprit afterwards (scores.culprit_agreement).

Every way the model can fail ends in the template, with the reason recorded:
- `timeout`: no answer within the client's timeout;
- `unreachable`: the server is down, refused the connection or answered with an error status;
- `invalid_json`: the reply is not JSON (or the server's reply had no message text);
- `schema`: well-formed JSON that breaks the schema, such as a service not in the graph, an extra
  field, or a summary longer than 600 characters.
With no client at all (CI, a clean machine without Ollama), the template is used and the error is
None, because nothing failed.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator

from always_200.incidents import Incident
from always_200.writeup.context import Context
from always_200.writeup.llm import LLMClient
from always_200.writeup.template import template_writeup

HERE = Path(__file__).parent


def load_schema() -> dict:
    return json.loads((HERE / "schema.json").read_text(encoding="utf-8"))


def load_prompt() -> str:
    return (HERE / "prompt.md").read_text(encoding="utf-8").strip()


def full_prompt(context: Context) -> str:
    """The fixed instruction (prompt.md), then the incident's context text."""
    return f"{load_prompt()}\n\n{context.text}"


@dataclass(frozen=True)
class WriteUp:
    source: str  # "llm" | "template"
    data: dict  # schema-shaped
    seconds: float
    error: str | None  # "timeout" | "unreachable" | "invalid_json" | "schema" | None


def _ask(client: LLMClient, context: Context, schema: dict) -> tuple[dict | None, str | None]:
    try:
        reply = client.generate(full_prompt(context), schema)
    except httpx.TimeoutException:
        return None, "timeout"
    except (httpx.HTTPError, OSError):
        return None, "unreachable"
    except ValueError:
        return None, "invalid_json"
    try:
        data = json.loads(reply)
    except (TypeError, ValueError):
        return None, "invalid_json"
    if not Draft202012Validator(schema).is_valid(data):
        return None, "schema"
    return data, None


def write(
    incident: Incident,
    context: Context,
    client: LLMClient | None,
    schema: dict,
    clock: Callable[[], float] = time.monotonic,
) -> WriteUp:
    start = clock()
    data, error = (None, None) if client is None else _ask(client, context, schema)
    if data is not None:
        return WriteUp("llm", data, clock() - start, None)
    return WriteUp("template", template_writeup(incident, context.samples), clock() - start, error)
