import pytest

from always_200.bench.stats import Z95, bootstrap_median_ci, poisson_rate_ci, wilson

# The plan's tests (step 10.1): the formulas pinned to published values.


def test_wilson_known_values():
    p, lo, hi = wilson(85, 100)
    assert abs(lo - 0.7672) < 1e-3 and abs(hi - 0.9069) < 1e-3
    assert wilson(0, 10)[1] == 0.0 and abs(wilson(0, 10)[2] - 0.2775) < 1e-3


def test_poisson_exact_known_values():
    assert abs(poisson_rate_ci(0, 17.0)[2] - 3.6889 / 17) < 1e-3
    r, lo, hi = poisson_rate_ci(5, 10.0)
    assert abs(lo - 0.16235) < 1e-3 and abs(hi - 1.16683) < 1e-3


def test_bootstrap_is_seeded_and_brackets_the_median():
    a = bootstrap_median_ci([30, 35, 40, 45, 50, 60, 90], seed=0)
    assert a == bootstrap_median_ci([30, 35, 40, 45, 50, 60, 90], seed=0)
    assert a[1] <= a[0] <= a[2]


# Extra tests pinning the edges of each formula.


def test_z95_is_the_two_sided_95_percent_normal_quantile():
    from scipy.stats import norm

    assert abs(Z95 - norm.ppf(0.975)) < 1e-12


def test_wilson_point_is_the_plain_share_and_all_hits_reaches_one():
    p, lo, hi = wilson(3, 4)
    assert p == 0.75 and lo < 0.75 < hi
    assert wilson(10, 10)[2] == 1.0 and wilson(10, 10)[1] < 1.0


def test_wilson_with_no_denominator_is_none():
    assert wilson(0, 0) is None


def test_wilson_refuses_impossible_counts():
    with pytest.raises(ValueError):
        wilson(5, 4)
    with pytest.raises(ValueError):
        wilson(-1, 4)


def test_poisson_rate_is_count_over_exposure_and_zero_has_zero_lower_bound():
    r, lo, hi = poisson_rate_ci(0, 17.0)
    assert r == 0.0 and lo == 0.0 and hi > 0.0
    r, lo, hi = poisson_rate_ci(5, 10.0)
    assert r == 0.5 and lo < r < hi


def test_poisson_refuses_zero_exposure():
    with pytest.raises(ValueError):
        poisson_rate_ci(1, 0.0)


def test_bootstrap_of_nothing_is_none_and_of_one_value_is_that_value():
    assert bootstrap_median_ci([]) is None
    assert bootstrap_median_ci([42.0]) == (42.0, 42.0, 42.0)


def test_bootstrap_point_is_the_plain_median_and_seed_matters():
    values = [10, 20, 30, 40, 1000]
    a = bootstrap_median_ci(values, seed=0)
    assert a[0] == 30.0  # the median ignores the one huge value; a mean would be 220
    assert bootstrap_median_ci(values, seed=1) is not None
