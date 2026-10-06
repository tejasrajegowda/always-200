# 0003. The seam: a time-ordered Sample stream; detectors never read the wall clock

**Status:** accepted

## Context

always-200 runs the same detectors in three settings: live against Prometheus, offline on the
bench from recorded files, and later from a stream. A score measured on the bench only says
something about the live product if the detector cannot tell the settings apart. The recordings
also hold the answer key (`fault_active`, `event_active`, `deploy_marker_total`, `session_phase`,
`session_seed`), which a detector must never see, and Prometheus returns NaN for a quantile at
zero traffic and no series at all for a counter that has never moved.

## Decision

| Decision | Content | Why |
|---|---|---|
| One seam | Every source yields an iterator of frozen `Sample(ts, service, signal, value)`; every detector has `update(sample) -> Anomaly \| None` (`always_200/core.py`, `typing.Protocol`). | Any source works with any detector; live, replay and a future stream are interchangeable. |
| Order | Samples are ordered by time, then service (`storefront, orders, payments`), then signal (`request_rate, latency_p95, error_ratio, telemetry_missing`), ruling P7. A replay file out of order, or with a duplicate, is refused. | The request gate needs the step's request rate before its latency; a silently re-sorted file would hide a broken recording. |
| Time | A detector reads `Sample.ts` only, never the wall clock (D5). | A replay at full speed gives exactly the live answer. |
| What may cross | Only the four detector signals, for the three known services, with finite values. `Sample` refuses everything else when it is built. | The answer key cannot leak into a detector (D11), and NaN cannot poison a baseline. |
| One assembler | `always_200/sources/assemble.py` turns per-step query results into samples for both the live source and the recorder, including `telemetry_missing` (ruling P2). | Live and recorded data can never disagree about what a missing scrape or an idle service means. |
| Files | JSON Lines, gzipped by suffix, written with a fixed gzip timestamp (`always_200/bench/files.py`). | Readable with any tool, streamable line by line, and byte-for-byte reproducible for a published dataset. |
| Answer key | Derived from the raw gauge points by edge detection (`always_200/bench/answer_key.py`): start = first scrape reading 1, end = first later scrape reading 0. | One clock (the scrape timestamps) for both the samples and the key. |

## Consequences

- A new source (for example a stream consumer) only has to yield `Sample`s in this order; no
  detector or scorer changes.
- Detectors cannot use time-of-day features from the wall clock; any such feature must come from
  `Sample.ts`.
- The recorder must store `up` and the answer-key gauges beside the samples, because the seam
  itself drops them.
