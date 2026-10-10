"""The dashboard panels and the detector read the same PromQL."""

import json
from pathlib import Path

from always_200.sources.queries import QUERIES

DASHBOARD = (
    Path(__file__).resolve().parents[2] / "deploy" / "grafana" / "dashboards" / "always-200.json"
)

PANELS = {
    "Request rate": "request_rate",
    "p95 latency": "latency_p95",
    "Error ratio": "error_ratio",
}


def test_signal_panels_copy_the_shared_queries():
    panels = {
        panel["title"]: panel
        for panel in json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"]
    }
    for title, key in PANELS.items():
        assert panels[title]["targets"][0]["expr"] == QUERIES[key]
