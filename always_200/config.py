"""Loads the shared configuration: timing constants, the graph and the detector settings.

config/default.yaml holds everything. config/frozen.yaml, written once after tuning on session A,
overrides only the keys it sets. Scoring session B reads the frozen view, so it must fail loudly
while the frozen file does not exist yet.
"""

from pathlib import Path

import yaml

from always_200.graph import Graph

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"config/{path.name} must hold a mapping at the top level")
    return data


def _overlay(base: dict, top: dict) -> dict:
    merged = dict(base)
    for key, value in top.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _overlay(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(frozen: bool = False, root: Path | None = None) -> dict:
    folder = (root or REPO_ROOT) / "config"
    cfg = _read(folder / "default.yaml")
    if frozen:
        frozen_path = folder / "frozen.yaml"
        if not frozen_path.exists():
            raise FileNotFoundError(
                "config/frozen.yaml does not exist yet. It is written once, after tuning on "
                "session A, and held-out scoring cannot run before it is committed."
            )
        cfg = _overlay(cfg, _read(frozen_path))
    return cfg


def graph_from(cfg: dict) -> Graph:
    graph = {caller: tuple(callees) for caller, callees in cfg["graph"].items()}
    if set(graph) != set(cfg["services"]):
        raise ValueError("the graph's services and the services list differ")
    return Graph(graph)
