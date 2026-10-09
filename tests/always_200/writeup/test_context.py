from pathlib import Path

from always_200.core import Sample
from always_200.writeup.context import build_context, fmt_value, load_runbooks

REPO = Path(__file__).resolve().parents[3]
T0 = 36000.0  # the fixtures' incident opening time (conftest.py)


def rows(text: str) -> list[list[str]]:
    """The evidence table's data rows, split into cells."""
    out = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if line.startswith("| t") and len(cells) == 5:
            out.append(cells)
    return out


# The plan's tests (step 20.1).


def test_context_never_names_the_culprit_as_such(CTX):
    assert "culprit" not in CTX.text.lower()
    for svc in ("storefront", "orders", "payments"):
        assert svc in CTX.text


def test_context_formats_numbers_as_rule_p17(CTX):
    table = {(m, svc, sig): (value, unit) for m, svc, sig, value, unit in rows(CTX.text)}
    assert len(table) == 3 * 3 * 11  # 3 services x 3 signals x minutes t-10 .. t+0
    assert table[("t+0", "payments", "latency_p95")] == ("412", "ms")  # whole milliseconds
    assert table[("t-10", "payments", "latency_p95")] == ("50", "ms")
    assert table[("t+0", "payments", "error_ratio")] == ("4.1", "%")  # %, one decimal
    assert table[("t-5", "orders", "error_ratio")] == ("0.0", "%")  # zero errors print as 0.0
    assert table[("t-1", "storefront", "request_rate")] == ("25.3", "req/s")  # one decimal
    assert sorted(CTX.values) == [0.0, 4.1, 25.3, 50.0, 80.0, 120.0, 412.0, 445.0, 480.0]


# Further tests.


def test_minute_labels_run_from_t_minus_10_to_t_plus_0(CTX):
    minutes = list(dict.fromkeys(r[0] for r in rows(CTX.text)))
    assert minutes == [f"t-{m}" for m in range(10, 0, -1)] + ["t+0"]


def test_each_cell_is_the_last_sample_of_its_minute(INC, G):
    s = [
        Sample(T0 - 59, "orders", "latency_p95", 0.200),
        Sample(T0 - 15, "orders", "latency_p95", 0.300),
        Sample(T0, "orders", "latency_p95", 0.333),
        Sample(T0 + 15, "orders", "latency_p95", 0.999),  # after t+0: never shown
    ]
    ctx = build_context(INC, s, [], G, {})
    table = {(r[0], r[1], r[2]): r[3] for r in rows(ctx.text)}
    assert table[("t+0", "orders", "latency_p95")] == "333"
    assert table[("t-1", "orders", "latency_p95")] == "-"  # no sample in (t-2, t-1]
    assert table[("t+0", "payments", "latency_p95")] == "-"
    assert ctx.values == (333.0,) and [c.value for c in ctx.samples] == [0.333]


def test_missing_telemetry_minutes_are_listed(INC, G):
    s = [Sample(T0 - 60 * m, "payments", "telemetry_missing", 1.0) for m in (0, 1)]
    s += [Sample(T0 - 120, "payments", "telemetry_missing", 0.0)]
    ctx = build_context(INC, s, [], G, {})
    assert "- payments at t-1, t+0" in ctx.text
    assert ctx.values == ()


def test_deploys_of_the_last_15_minutes_as_relative_minutes(INC, SAMPLES, G):
    deploys = [
        {"ts": T0 - 2000, "service": "orders"},  # too old
        {"ts": T0 - 245, "service": "payments"},
        {"ts": T0 + 30, "service": "storefront"},  # after the incident opened
    ]
    text = build_context(INC, SAMPLES, deploys, G, {}).text
    deploy_part = text.split("Deploys in the last fifteen minutes:")[1].split("Runbooks:")[0]
    assert deploy_part.strip() == "- payments at t-4"
    assert "- none" in build_context(INC, SAMPLES, [], G, {}).text.split("Deploys")[1]


def test_graph_and_runbooks_are_in_the_text(CTX, RUNBOOKS):
    assert "- storefront calls orders" in CTX.text
    assert "- payments calls nothing" in CTX.text
    for svc, book in RUNBOOKS.items():
        assert f"## {svc}\n{book}" in CTX.text


def test_fmt_value_rounds_as_p17():
    assert fmt_value("latency_p95", 0.4116) == "412"
    assert fmt_value("error_ratio", 0.0412) == "4.1"
    assert fmt_value("request_rate", 12.345) == "12.3"


def test_the_committed_runbooks_load_with_five_lines_each():
    books = load_runbooks(REPO / "config" / "runbooks")
    assert sorted(books) == ["orders", "payments", "storefront"]
    for svc, book in books.items():
        assert len(book.splitlines()) == 5 and svc in book
        assert "culprit" not in book.lower()
