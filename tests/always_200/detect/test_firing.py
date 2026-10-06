import pytest

from always_200.detect.firing import FireClear


def run(fc: FireClear, outs: list[bool]) -> list[tuple[bool, bool]]:
    return [fc.step(o) for o in outs]


def test_fires_on_the_third_consecutive_out_step():
    fc = FireClear()
    assert run(fc, [True, True, True]) == [(False, False), (False, False), (True, True)]
    assert fc.alerting


def test_an_in_step_resets_the_fire_count():
    fc = FireClear()
    assert run(fc, [True, True, False, True, True]) == [(False, False)] * 5
    assert not fc.alerting
    assert fc.step(True) == (True, True)


def test_clear_countdown_is_alerting_but_silent():
    fc = FireClear()
    run(fc, [True] * 3)
    for _ in range(3):
        assert fc.step(False) == (False, False)
        assert fc.alerting  # still alerting during the countdown
    assert fc.step(False) == (False, False)
    assert not fc.alerting  # the fourth in-band step clears


def test_an_out_step_during_the_countdown_emits_and_restarts_it():
    fc = FireClear()
    run(fc, [True] * 3 + [False] * 3)
    assert fc.step(True) == (True, False)  # emits, not a new first
    assert run(fc, [False] * 3) == [(False, False)] * 3
    assert fc.alerting  # the countdown started again from zero
    fc.step(False)
    assert not fc.alerting


def test_first_is_true_only_once_per_alert():
    fc = FireClear()
    firsts = [first for _, first in run(fc, [True] * 10)]
    assert firsts.count(True) == 1 and firsts[2] is True


def test_after_a_clear_it_needs_n_fire_again_and_marks_a_new_first():
    fc = FireClear()
    run(fc, [True] * 3 + [False] * 4)
    assert run(fc, [True] * 3) == [(False, False), (False, False), (True, True)]


def test_custom_n_fire_and_n_clear():
    fc = FireClear(n_fire=1, n_clear=1)
    assert fc.step(True) == (True, True)
    fc.step(False)
    assert not fc.alerting


@pytest.mark.parametrize("n_fire, n_clear", [(0, 4), (3, 0)])
def test_zero_counts_are_refused(n_fire, n_clear):
    with pytest.raises(ValueError):
        FireClear(n_fire, n_clear)
