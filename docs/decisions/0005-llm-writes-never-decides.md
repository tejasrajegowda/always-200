# 0005. The LLM writes the incident note; it never decides anything

**Status:** accepted

## Context

An incident needs a short note for the person on call: what changed, where, and what to check
first. A local language model writes that kind of prose well. It is also unreliable in ways that
matter here: it can name a service that does not exist, ignore the requested shape, write far too
much, take longer than a page can wait, or state numbers it was never shown. The ticket's assignee
and the scored culprit must not depend on any of that, and CI must never need the model (D9, D12).

## Decision

| Decision | Content | Why |
|---|---|---|
| Decisions stay in code | The culprit, and so the assignee, come only from the incident builder (D7). `write` never changes the incident. The model's `suspected_service` is only compared with the culprit afterwards (culprit agreement). | A model error can make a note worse, never page the wrong team. |
| Not told the culprit | The context (`always_200/writeup/context.py`) shows the graph, a t-10 .. t+0 table of every signal for all three services, missing telemetry, recent deploys and the runbooks, but never which service the builder blamed. | Agreement is then evidence the model read the data, not an echo. |
| Structured output | The reply must be JSON matching `always_200/writeup/schema.json` (draft 2020-12): four required fields, no extra fields, the service from the graph's enum, at most 600 characters of summary and 1 to 10 evidence rows. The schema is also sent as Ollama's `format`. | Constrained output is cheap to check and safe to put in a ticket. |
| Deterministic settings | Temperature 0, seed 42, a 60 s timeout, no streaming (`always_200/writeup/llm.py`); `think: false` only when asked, for thinking models. | Re-running a bench gives the same note; a stuck model cannot hold an incident. |
| Template fallback | On a timeout, an unreachable server, a reply that is not JSON, or a schema break, the template (`always_200/writeup/template.py`) writes the note from the builder's culprit and the context's own cells, and the reason is recorded in `WriteUp.error`. With no client at all (CI, a clean machine) the template is used with no error. | Every incident gets a valid note, and failures are counted, not hidden. |
| Grounded numbers | A note is grounded when every number in its prose equals, as a number, a value from the table's value column within max(0.05, 1%) (ruling P17, `always_200/writeup/scores.py`). Minute labels, digits inside names (`p95`) and runbook digits never ground a number. | Invented numbers are the most damaging model error in an incident note, and this makes them measurable. |

## Consequences

- The LLM's quality is reported as two rates (culprit agreement, grounded numbers) plus the
  fallback count by reason; it never changes the detection or culprit scores.
- The template is grounded and schema-valid by construction, so a run without a model is a fair
  floor to compare a model against.
- A number written with thousands separators ("1,200") reads as two numbers and is ungrounded;
  the evidence table never prints one, so a model copying the table is never penalised.
- CI tests the client only through `httpx.MockTransport`; real models are tried in a later step.
