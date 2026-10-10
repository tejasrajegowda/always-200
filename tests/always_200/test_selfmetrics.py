"""The watcher's own registry, and the Prometheus rule that scrapes it."""

from pathlib import Path

import yaml
from prometheus_client import generate_latest

from always_200.selfmetrics import SelfMetrics

ROOT = Path(__file__).resolve().parents[2]
NAMES = (
    "always200_samples_total",
    "always200_sample_lag_seconds",
    "always200_alerts_open",
    "always200_incidents_open",
    "always200_llm_seconds",
    "always200_writeup_fallbacks_total",
    "always200_sink_errors_total",
)


def test_metric_names_are_the_contract_and_counters_are_not_doubled():
    metrics = SelfMetrics()
    text = generate_latest(metrics.registry).decode()
    for name in NAMES:
        assert name in text
    assert "always200_samples_total_total" not in text
    assert 'always200_incidents_open{kind="fault"}' in text
    assert 'always200_incidents_open{kind="monitoring_gap"}' in text


def test_serve_publishes_this_registry(monkeypatch):
    seen: dict = {}

    def fake_server(port: int, addr: str = "0.0.0.0", registry=None) -> None:
        seen["port"] = port
        seen["addr"] = addr
        seen["registry"] = registry

    monkeypatch.setattr("always_200.selfmetrics.start_http_server", fake_server)
    metrics = SelfMetrics()
    metrics.serve()
    assert seen == {"port": 9200, "addr": "0.0.0.0", "registry": metrics.registry}


def test_self_alert_watches_the_always_200_job():
    rules = yaml.safe_load((ROOT / "deploy" / "prometheus" / "rules" / "self.yml").read_text())
    alerts = [rule for group in rules["groups"] for rule in group["rules"]]
    assert len(alerts) == 1
    assert alerts[0]["alert"] == "Always200Down"
    assert alerts[0]["expr"] == 'up{job="always-200"} == 0'
    assert alerts[0]["for"] == "1m"
    config = (ROOT / "deploy" / "prometheus" / "prometheus.yml").read_text(encoding="utf-8")
    assert "job_name: always-200" in config
    assert "always-200:9200" in config
