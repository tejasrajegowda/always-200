import pytest

from always_200.config import graph_from, load_config
from always_200.graph import Graph

G = Graph({"storefront": ("orders",), "orders": ("payments",), "payments": ()})


def test_the_timing_constants_load():
    cfg = load_config()
    assert cfg["timing"]["grace_s"] == 90
    assert cfg["timing"]["step_s"] == 15


def test_the_graph_comes_from_the_config():
    assert graph_from(load_config()) == G


def test_frozen_cannot_be_read_before_it_exists(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.yaml").write_text("band: {k: 4.0}\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="frozen.yaml"):
        load_config(frozen=True, root=tmp_path)


def test_frozen_cannot_be_read_from_the_repo_yet():
    with pytest.raises(FileNotFoundError, match="frozen.yaml"):
        load_config(frozen=True)


def test_frozen_overlays_only_the_keys_it_sets(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.yaml").write_text(
        "band: {k: 4.0, window: 20}\ntiming: {grace_s: 90}\n", encoding="utf-8"
    )
    (tmp_path / "config" / "frozen.yaml").write_text("band: {k: 5.0}\n", encoding="utf-8")
    cfg = load_config(frozen=True, root=tmp_path)
    assert cfg["band"] == {"k": 5.0, "window": 20}
    assert cfg["timing"] == {"grace_s": 90}


def test_a_graph_that_disagrees_with_the_service_list_is_refused():
    cfg = {"services": ["storefront", "orders"], "graph": {"storefront": ["orders"]}}
    with pytest.raises(ValueError, match="services"):
        graph_from(cfg)
