import pytest

from always_200.graph import Graph

G = Graph({"storefront": ("orders",), "orders": ("payments",), "payments": ()})


def test_depth_follows_the_call_chain():
    assert [G.depth(s) for s in ("storefront", "orders", "payments")] == [0, 1, 2]


def test_upstream_is_everything_that_can_reach_a_service():
    assert G.upstream("payments") == {"storefront", "orders"}
    assert G.upstream("storefront") == frozenset()


def test_depth_is_longest_path_in_a_dag():
    d = Graph({"a": ("b", "c"), "b": ("c",), "c": ()})
    assert d.depth("c") == 2


def test_a_cycle_is_refused():
    with pytest.raises(ValueError, match="cycle"):
        Graph({"a": ("b",), "b": ("a",)})


def test_a_callee_that_is_not_a_service_is_refused():
    with pytest.raises(ValueError, match="not a service"):
        Graph({"a": ("b",)})


def test_an_unknown_service_is_a_key_error():
    with pytest.raises(KeyError):
        G.depth("postgres")
    with pytest.raises(KeyError):
        G.upstream("postgres")
