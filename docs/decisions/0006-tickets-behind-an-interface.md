# 0006. Tickets go through one interface, one ticket per incident

**Status:** accepted

## Context

An incident has to reach a person as a ticket: opened with an assignee, updated when more
services start alerting, and resolved when it goes quiet. The ticket system differs by setting:
the bench and CI must never touch the network (D9, ruling P13), while the live demo files real
Jira tickets (step 24). Paging per alert would bury the person on call: a single fault makes a
detector alert on every 15 s step, and on the services upstream of the fault as well.

## Decision

| Decision | Content | Why |
|---|---|---|
| One interface | `TicketSink` (`always_200/tickets/base.py`) has three methods: `open(incident, writeup, assignee) -> key`, `comment(key, incident, text)` and `resolve(key, incident)`. `TicketFlow` (`always_200/tickets/flow.py`) talks only to it. | The Jira sink is added without changing the flow, the incident builder or the bench. |
| JSONL by default | `JsonlSink` (`always_200/tickets/jsonl.py`) appends one JSON line per action to a local file and never rewrites it. `config/default.yaml` names it (`tickets.sink: jsonl`); `sink_from` refuses an unknown sink name. | Nothing leaves the machine unless the config says so, and the file is a full, readable history of the tickets. |
| De-duplication by incident state | One incident is one ticket. The ticket opens when the culprit becomes final (the end of the 60 s settle window, ruling P14); every alert that arrived before that is in the opening body. A new service and signal after that is one comment; a repeat of a known pair is nothing. The close resolves the ticket, and a later alert opens a new incident and so a new ticket. | The incident builder already decides what one problem is (D7). Time buckets would split one long incident into several tickets, or merge two incidents into one. |
| The assignee comes from the culprit | `owners(incident.culprit)`, never the write-up's `suspected_service`. In the bench and CI the owner is `owner:<service>`; real accounts arrive with the owners file (step 24). | A model can make the note worse, never page the wrong team (D7, D12, ADR 0005). |
| Monitoring gaps get their own ticket | A `monitoring_gap` incident opens a "monitoring gap" ticket from the template's text and never asks the writer. | Missing telemetry is an alert (D16), but the evidence a model would need is exactly what is missing. |
| Sample time, not the wall clock | A `SampleClock`, moved by the flow to each event's timestamp, gives the time written on each line. | A replay writes the same ticket times as live (D5). |
| Time to ticket in three parts | Per ticket: the settle wait (sample seconds from the opening to the final culprit), the write-up's seconds and the sink's seconds. The bench prints their medians per detector, apart from detection latency (r2 §4.5). | Detection latency measures the detector; the time to ticket is what the person on call waits, and a slow writer or sink shows up in its own column. |
| The bench writes with the template | `bench --tickets` uses the template writer and never builds a model client, whatever the config names. | One model timeout per incident would turn a bench run of minutes into hours, and the bench must open no socket. |

## Consequences

- `bench --tickets [PATH]` writes `runs/bench/tickets.jsonl` by default, rewritten on each run: it
  is derived from the recordings. Each line also carries the detector and the session.
- An incident whose culprit is still settling when the stream ends gets no ticket in that run; a
  session ends with a cool-down, so this does not happen to a scored fault.
- The Jira sink must keep the same contract: a sink error never crashes the flow (step 24).
