"""`record.start` with a fake command runner: no Docker is started."""

import subprocess
from datetime import datetime

import pytest

from always_200.bench.record import expected_seconds, start, volume_name
from traffic.schedule import build_session


class FakeDocker:
    def __init__(self, volume_exists: bool = False) -> None:
        self.volume_exists = volume_exists
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append((args, kwargs))
        code = 0 if (args[:3] != ["docker", "volume", "inspect"] or self.volume_exists) else 1
        return subprocess.CompletedProcess(args, code)


def test_start_sets_three_separate_variables_and_runs_detached():
    docker, said = FakeDocker(), []
    start(["B1", "B2", "B3"], "B-run", runner=docker, now=1_759_740_000.0, say=said.append)
    (probe, _), (up, kwargs) = docker.calls
    assert probe == ["docker", "volume", "inspect", "always-200-prom-B-run"]
    assert up == ["docker", "compose", "--profile", "traffic", "up", "-d", "--build"]
    env = kwargs["env"]
    assert (env["SEEDS"], env["QUICK"], env["RUN"]) == ("B1,B2,B3", "0", "B-run")
    assert "PATH" in env or "Path" in env  # the rest of the environment is kept


def test_start_prints_the_expected_end_in_local_time():
    said: list[str] = []
    now = 1_759_740_000.0
    start(["dev"], "dev", quick=True, runner=FakeDocker(), now=now, say=said.append)
    total = build_session("dev", quick=True).total_s
    assert expected_seconds(["dev"], quick=True) == total
    ends = datetime.fromtimestamp(now + total).astimezone()
    assert f"{ends:%Y-%m-%d %H:%M}" in said[0]
    assert "detached" in said[0]


def test_a_full_session_lasts_about_eight_point_eight_hours():
    assert 8.0 * 3600 < expected_seconds(["A1"]) < 9.5 * 3600


def test_an_existing_volume_is_refused_and_nothing_starts():
    docker = FakeDocker(volume_exists=True)
    with pytest.raises(FileExistsError, match="A1-r2"):
        start(["A1"], "A1", runner=docker, say=lambda _: None)
    assert len(docker.calls) == 1  # only the probe; compose up never ran
    assert volume_name("A1") == "always-200-prom-A1"


@pytest.mark.parametrize(
    ("seeds", "run_name"),
    [([], "x"), (["A1,B1"], "x"), (["A1 --rm"], "x"), (["A1"], "--build"), (["A1"], "a/b")],
)
def test_bad_names_are_refused_before_docker_is_called(seeds, run_name):
    docker = FakeDocker()
    with pytest.raises(ValueError):
        start(seeds, run_name, runner=docker, say=lambda _: None)
    assert docker.calls == []
