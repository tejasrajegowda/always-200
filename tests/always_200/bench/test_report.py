"""The results table (step 10.3): the r2 §4.11 layout, n/a for empty denominators, stable JSON."""

import json

from always_200.bench.answer_key import AnswerKey, Event, Fault
from always_200.bench.report import CULPRIT_RULE, render_table, to_json
from always_200.bench.score import score_session, summarise
from always_200.bench.stats import bootstrap_median_ci, wilson
from always_200.core import Anomaly
from always_200.graph import Graph
from always_200.incidents import IncidentBuilder

G = Graph({"storefront": ("orders",), "orders": ("payments",), "payments": ()})
KEY = AnswerKey(
    "T1",
    0.0,
    300.0,
    3600.0,
    (
        Fault("T1-f01", "latency_step", "payments", "subtle", 600.0, 780.0, True),
        Fault("T1-f02", "errors", "orders", "moderate", 1500.0, 1680.0, True),
        Fault("T1-f03", "latency_ramp", "storefront", "severe", 2400.0, 2580.0, True),
    ),
    (Event("traffic_spike", 3000.0, 3150.0),),
)


def _incidents(*alerts):
    b = IncidentBuilder(G)
    for ts, svc, sig in alerts:
        b.feed(Anomaly(ts, svc, sig, "fault", 1.0, 0.5, 0.1, False))
    return b.finish(3600.0)


def rows():
    never = summarise([score_session([], KEY, G)])
    some = summarise(
        [
            score_session(
                _incidents(
                    (630.0, "payments", "latency_p95"),
                    (1545.0, "orders", "error_ratio"),
                    (3100.0, "storefront", "latency_p95"),
                ),
                KEY,
                G,
            )
        ]
    )
    return {"Some detector": some, "Never detector": never}


def _line(text, name):
    return [ln for ln in text.splitlines() if ln.startswith(name)]


def test_header_has_the_r2_columns_the_culprit_rule_and_no_chance_line():
    text = render_table(rows())
    for column in (
        "Detector",
        "Detection (95% CI)",
        "Swallowed",
        "Latency median / p90",
        "FA per clean h (95% CI)",
        "FA in events",
        "Culprit % (95% CI)",
    ):
        assert column in text
    assert CULPRIT_RULE in text
    assert "most downstream alerting service" in text  # the comparison row's heading
    assert "chance" not in text.lower()


def test_every_detector_has_main_comparison_size_and_type_rows():
    text = render_table(rows())
    assert len(_line(text, "Some detector")) == 4
    assert len(_line(text, "Never detector")) == 4


def test_zero_denominators_print_na():
    text = render_table(rows())  # must not raise
    main, comparison, _size, _type = _line(text, "Never detector")
    assert "n/a / n/a" in main  # latency median / p90
    assert main.rstrip().endswith("n/a")  # culprit %
    assert comparison.rstrip().endswith("n/a")  # comparison culprit
    assert "0.0% (0.0-56.1)" in main  # detection 0 of 3 is a number, not n/a


def test_by_size_and_by_type_sections_are_printed():
    text = render_table(rows())
    for word in ("subtle", "moderate", "severe", "latency_step", "latency_ramp", "errors"):
        assert word in text


def test_empty_denominators_are_none_not_errors():
    assert wilson(0, 0) is None
    assert bootstrap_median_ci([]) is None


def test_to_json_is_stable_rounded_and_serialisable():
    a, b = to_json(rows()), to_json(rows())
    assert a == b
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    never = a["Never detector"]
    assert never["culprit"] is None and never["latency_p90"] is None
    assert never["detection"] == [0.0, 0.0, 0.5615]  # rounded to 4 dp
    assert a["Some detector"]["n_faults"] == 3
