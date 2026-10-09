"""The two write-up scores (r2 section 9, ruling P17).

- `culprit_agreement`: did the model suspect the same service the incident builder chose? The
  model was never told that choice (context.py), so agreement is earned, not copied.
- `grounded`: is every number in the prose one the model was actually shown? A number is grounded
  only if it equals, as a number, a value from the evidence table's value column, within
  max(0.05, 1%) of that value. "412", "412.0" and "412.00" are the same number; "999" is not.

What counts as a number in the prose: a decimal that starts a token. Digits glued after a letter
are part of a name, not a quantity ("p95", "http2"), and a minute label ("t-10", "t+0") is a time
reference, never a measurement. Digits followed by a unit still count ("412ms" is 412), so an
invented number cannot hide by dropping a space.
"""

import re
from collections.abc import Sequence

NUMBER = re.compile(r"(?<![\w.])(?<![A-Za-z][+-])[-+]?\d+(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    return [float(m) for m in NUMBER.findall(text)]


def _matches(n: float, v: float) -> bool:
    return abs(n - v) <= max(0.05, 0.01 * abs(v))


def grounded(data: dict, values: Sequence[float]) -> bool:
    """Every number in summary and first_check equals, as a number, some metric value v in `values`
    within max(0.05, 0.01 * |v|) (ruling P17). Matching is numeric, never by string token."""
    prose = f"{data.get('summary', '')}\n{data.get('first_check', '')}"
    return all(any(_matches(n, v) for v in values) for n in numbers_in(prose))


def culprit_agreement(data: dict, culprit: str) -> bool:
    return data.get("suspected_service") == culprit
