"""`make record`: start a detached recording run of one or more seeded sessions.

A full session takes about 8.8 hours, longer than any terminal or chat stays open. So the run is
started with `docker compose up -d` (detached): Docker keeps the containers running after this
command returns, and closing the terminal does not stop the recording.

Ruling P12: every run gets its own Prometheus volume, `always-200-prom-<RUN>`. A name that already
exists is refused, so a re-recording always gets a new name (for example `A1-r2`) and the old data
is never mixed in or overwritten. The Compose file (step 5) names the volume from `RUN`.

The seeds, the quick flag and the run name reach Compose as three separate environment variables,
never as one flag string, so a value can never be read as an extra option.
"""

import os
import re
import subprocess
import time
from collections.abc import Callable
from datetime import datetime

from traffic.schedule import build_session

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SEED = re.compile(r"[A-Za-z0-9_-]+")

Runner = Callable[..., subprocess.CompletedProcess]


def volume_name(run_name: str) -> str:
    return f"always-200-prom-{run_name}"


def check_names(seeds: list[str], run_name: str) -> None:
    if not seeds:
        raise ValueError("give at least one seed")
    for seed in seeds:
        if not SEED.fullmatch(seed):
            raise ValueError(f"seed {seed!r} may hold only letters, digits, '_' and '-'")
    if not NAME.fullmatch(run_name):
        raise ValueError(f"run name {run_name!r} may hold only letters, digits, '_', '.' and '-'")


def expected_seconds(seeds: list[str], quick: bool = False) -> float:
    """How long the run takes: the seeds' sessions back to back."""
    return sum(build_session(seed, quick).total_s for seed in seeds)


def start(
    seeds: list[str],
    run_name: str,
    quick: bool = False,
    *,
    runner: Runner = subprocess.run,
    now: float | None = None,
    say: Callable[[str], None] = print,
) -> None:
    """docker compose --profile traffic up -d --build with SEEDS, QUICK ("1" or "0") and RUN set as
    separate environment variables (never one flag string). RUN names the run's own Prometheus
    volume (ruling P12); refuses if a volume of that name already exists, so a re-recording always
    gets a new name (for example A1-r2) and the old data is kept. Prints the expected end time in
    local time, from sum(build_session(s, quick).total_s for s in seeds)."""
    check_names(seeds, run_name)
    volume = volume_name(run_name)
    probe = runner(["docker", "volume", "inspect", volume], capture_output=True, check=False)
    if probe.returncode == 0:
        raise FileExistsError(
            f"the volume {volume} already exists: run {run_name} was recorded before. "
            f"Pick a new run name (for example {run_name}-r2); the old data is kept."
        )
    env = {**os.environ, "SEEDS": ",".join(seeds), "QUICK": "1" if quick else "0", "RUN": run_name}
    runner(
        ["docker", "compose", "--profile", "traffic", "up", "-d", "--build"], env=env, check=True
    )
    total = expected_seconds(seeds, quick)
    began = time.time() if now is None else now
    ends = datetime.fromtimestamp(began + total).astimezone()
    say(
        f"Recording run {run_name} ({', '.join(seeds)}{', quick' if quick else ''}) started, "
        f"detached. Expected end: {ends:%Y-%m-%d %H:%M} local time, about {total / 3600:.1f} h "
        f"from now. Closing this terminal does not stop it. When the traffic container has "
        f"exited, run the export."
    )
