import pytest

from traffic.load import Load, day_curve


def test_day_curve_is_quiet_at_midnight_and_full_at_midday():
    assert day_curve(0) == pytest.approx(0.30, abs=1e-9)
    assert day_curve(1800) == pytest.approx(1.00, abs=1e-9)
    assert day_curve(3600) == pytest.approx(0.30, abs=1e-9)  # one hour is one whole "day"


def test_rate_stays_inside_the_noise_band():
    load = Load("A1")
    for t in range(0, 3601):
        assert 25 * 0.3 * 0.95 <= load.rate(t) <= 25 * 1.05


def test_spike_is_two_and_a_half_times_the_rate():
    load = Load("A1")
    for t in (0, 59.5, 900, 1800, 3599):
        assert load.rate(t, spike=True) == 2.5 * load.rate(t)


def test_same_seed_same_rates():
    a, b = Load("A1"), Load("A1")
    ts = [x * 7.3 for x in range(2000)]
    assert [a.rate(t) for t in ts] == [b.rate(t) for t in ts]


# Extra checks beyond the plan's list.


def test_rates_do_not_depend_on_the_order_of_the_questions():
    forward, backward = Load("B1"), Load("B1")
    ts = [x * 13.0 for x in range(500)]
    assert [forward.rate(t) for t in ts] == list(reversed([backward.rate(t) for t in ts[::-1]]))


def test_different_seeds_give_different_noise():
    assert [Load("A1").rate(t) for t in range(0, 600, 60)] != [
        Load("B1").rate(t) for t in range(0, 600, 60)
    ]


def test_noise_is_piecewise_linear_between_knots():
    load = Load("A1")
    assert load.noise(30) == pytest.approx((load.noise(0) + load.noise(60)) / 2, abs=1e-12)


def test_negative_time_is_refused():
    with pytest.raises(ValueError):
        Load("A1").rate(-1)
