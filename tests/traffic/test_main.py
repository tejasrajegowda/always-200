"""Compose passes one environment variable per setting. A flag string must never come back."""

from traffic.__main__ import argv_from_env, main
from traffic.runner import Targets


def test_a_full_run_gets_no_quick_flag():
    for env in ({"SEEDS": "A1"}, {"SEEDS": "A1", "QUICK": ""}, {"SEEDS": "A1", "QUICK": "0"}):
        assert "--quick" not in argv_from_env(env)
    assert argv_from_env({"SEEDS": "B1,B2,B3"}) == ["run", "--seeds", "B1,B2,B3"]


def test_quick_and_soak_min_are_separate_arguments():
    assert argv_from_env({"SEEDS": "dev", "QUICK": "1", "SOAK_MIN": "60"}) == [
        "run",
        "--seeds",
        "dev",
        "--quick",
        "--soak-min",
        "60",
    ]


def test_seeds_run_back_to_back(monkeypatch):
    seen: list[tuple[str, float, int]] = []

    async def fake_run(plan, targets: Targets, client, clock=None, sleep=None):
        assert targets.base["payments"] == "http://payments:8000"
        seen.append((plan.seed, plan.soak_s, len(plan.slots)))

    monkeypatch.setattr("traffic.__main__.run_session", fake_run)
    monkeypatch.setattr("traffic.__main__.start_http_server", lambda _port: None)
    assert main(["run", "--seeds", "B1,B2,B3", "--quick", "--soak-min", "1"]) == 0
    # One minute of soak, and quick mode's three faults. Back to back, in the order given.
    assert seen == [("B1", 60.0, 3), ("B2", 60.0, 3), ("B3", 60.0, 3)]
