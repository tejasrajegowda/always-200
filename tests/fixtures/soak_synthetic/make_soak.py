"""Builds the hand-built synthetic soak fixture (ruling P11): a clean, fault-free session.

It is the second CI fixture. The first (the opening of a real recorded session) shows the band
on real data; this one is made up on purpose, so it can run anywhere and never changes:

- 10 min warm-up, then a 60 min clean soak, one step every 15 s, all three services;
- the load follows the compressed day curve (`traffic.load`): night, midday and night again;
- latency p95 rises with load (busier means slower, the D1 realism), with seeded log-normal
  noise and occasional one-step blips of up to +60%;
- errors are rare and random: each request fails with probability 0.1%.

A detector that false-alarms just because the day curve rises or one step is noisy fails CI on
this file. Same code, same bytes: `test_soak_fixture.py` rebuilds it and compares.

Rebuild (only when this generator changes on purpose):
    uv run python -m tests.fixtures.soak_synthetic.make_soak
"""

import math
import random
from collections.abc import Iterator
from pathlib import Path

from always_200.bench.answer_key import AnswerKey, write_key
from always_200.bench.files import write_samples
from always_200.core import SERVICES, Sample
from traffic.load import PEAK_RPS, Load

HERE = Path(__file__).resolve().parent
T0 = 1_800_000_000.0  # an arbitrary, round Unix time for the session start
STEP_S = 15
WARMUP_S = 600
SOAK_S = 3600
BASE_P95_S = {"storefront": 0.060, "orders": 0.040, "payments": 0.020}  # p95 with no load
LOAD_SLOPE = 0.6  # p95 is 60% higher at peak load than with no load
NOISE_SIGMA = 0.06  # log-normal noise on every p95
BLIP_CHANCE = 0.02  # share of steps with a one-step latency blip
BLIP_MAX = 0.6  # a blip adds up to +60%
ERROR_CHANCE = 0.001  # each request fails with this probability


def build() -> Iterator[Sample]:
    load = Load("soak-synthetic")
    rng = random.Random("always-200-soak-synthetic")
    for i in range((WARMUP_S + SOAK_S) // STEP_S + 1):
        t = i * STEP_S
        rps = load.rate(t)
        requests_1m = round(rps * 60)
        for service in SERVICES:
            p95 = BASE_P95_S[service] * (1 + LOAD_SLOPE * rps / PEAK_RPS)
            p95 *= math.exp(rng.gauss(0.0, NOISE_SIGMA))
            if rng.random() < BLIP_CHANCE:
                p95 *= 1 + rng.uniform(0.0, BLIP_MAX)
            errors = sum(rng.random() < ERROR_CHANCE for _ in range(requests_1m))
            ts = T0 + t
            yield Sample(ts, service, "request_rate", round(rps, 4))
            yield Sample(ts, service, "latency_p95", round(p95, 6))
            yield Sample(ts, service, "error_ratio", round(errors / requests_1m, 6))
            yield Sample(ts, service, "telemetry_missing", 0.0)


def key() -> AnswerKey:
    end = T0 + WARMUP_S + SOAK_S
    return AnswerKey("soak", T0 + WARMUP_S, end, end, (), ())


def write(folder: Path = HERE) -> None:
    write_samples(folder / "samples.jsonl", build())
    write_key(folder / "answer_key.jsonl", key())


if __name__ == "__main__":
    write()
