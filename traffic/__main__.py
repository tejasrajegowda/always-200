"""Run seeded sessions against the shop. With no arguments, the flags come from the environment.

Compose must pass SEEDS, QUICK and SOAK_MIN as separate variables. One command string would
arrive as a single argument, and ``${VAR:-default}`` also fills the default when the variable
is empty, so a full run could not tell "quick is off" from "quick was not set".
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping

import httpx
from prometheus_client import start_http_server

from traffic.metrics import METRICS_PORT
from traffic.runner import Targets, run_session
from traffic.schedule import build_session

DEFAULT_TARGETS = Targets(
    base={
        "storefront": "http://storefront:8000",
        "orders": "http://orders:8000",
        "payments": "http://payments:8000",
    }
)


def argv_from_env(env: Mapping[str, str]) -> list[str]:
    """Build ``run --seeds ...`` from separate environment variables.

    SEEDS defaults to ``dev``. QUICK adds ``--quick`` only when it is exactly ``1``
    (empty, unset or ``0`` is a full session). A non-empty SOAK_MIN is passed as
    ``--soak-min`` and its own value, never joined into one string.
    """
    argv = ["run", "--seeds", env.get("SEEDS", "dev")]
    if env.get("QUICK") == "1":
        argv.append("--quick")
    soak_min = env.get("SOAK_MIN", "")
    if soak_min != "":
        argv.extend(["--soak-min", soak_min])
    return argv


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="traffic")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="play one or more sessions back to back")
    run.add_argument("--seeds", required=True, help="comma-separated seeds, for example B1,B2,B3")
    run.add_argument("--quick", action="store_true", help="short plan: three severe faults")
    run.add_argument(
        "--soak-min", type=float, default=None, help="override the clean soak, in minutes"
    )
    return parser


async def _run_all(seeds: list[str], quick: bool, soak_s: float | None, targets: Targets) -> None:
    async with httpx.AsyncClient() as client:
        for seed in seeds:
            plan = build_session(seed, quick=quick, soak_s=soak_s)
            await run_session(plan, targets, client)


def main(argv: list[str] | None = None) -> int:
    """Play the sessions and return 0. No arguments means ``argv_from_env(os.environ)``."""
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = argv_from_env(os.environ)
    args = _parser().parse_args(argv)
    seeds = [part.strip() for part in args.seeds.split(",") if part.strip()]
    if not seeds:
        raise SystemExit("traffic run: --seeds is empty")
    start_http_server(METRICS_PORT)
    soak_s = None if args.soak_min is None else args.soak_min * 60.0
    asyncio.run(_run_all(seeds, args.quick, soak_s, DEFAULT_TARGETS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
