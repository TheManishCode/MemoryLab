"""Unit tests for api.stats — pure, no database. Everything is deterministic (fixed seed)."""

import math

import pytest

from api.stats import ArmStats, _percentile, compare, summarize


def test_percentile_linear_interpolation() -> None:
    assert _percentile([0.0, 1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5          # pos 1.5 -> 2 + (3-2)*0.5
    assert _percentile([10.0], 0.5) == 10.0
    assert _percentile([0.0, 10.0], 0.0) == 0.0 and _percentile([0.0, 10.0], 1.0) == 10.0


def test_summarize_point_estimates_are_exact() -> None:
    s = summarize([10.0, 20.0, 30.0, 40.0])
    assert (s.n, s.mean, s.median, s.min, s.max) == (4, 25.0, 25.0, 10.0, 40.0)
    assert s.stdev == pytest.approx(12.909944, abs=1e-5)


def test_summarize_empty_and_singleton() -> None:
    assert summarize([]) == ArmStats(0, None, None, None, None, None, None)
    one = summarize([7.0])
    assert (one.n, one.mean, one.median, one.min, one.max) == (1, 7.0, 7.0, 7.0, 7.0)
    assert one.stdev is None and one.ci95 is None            # no spread, no CI from one point


def test_ci_is_within_range_brackets_the_mean_and_is_deterministic() -> None:
    values = [1.0, 2.0, 2.0, 3.0, 8.0]
    a, b = summarize(values), summarize(values)
    assert a.ci95 == b.ci95                                   # same seed -> identical
    assert a.ci95 is not None
    lo, hi = a.ci95
    assert min(values) <= lo <= a.mean <= hi <= max(values)   # bootstrap means never leave [min,max]


def test_ci_of_identical_values_is_a_point() -> None:
    s = summarize([5.0, 5.0, 5.0])
    assert s.stdev == 0.0 and s.ci95 == (5.0, 5.0)


def test_compare_needs_both_arms() -> None:
    assert compare("a", [], "b", [1.0, 2.0]) is None
    assert compare("a", [1.0], "b", []) is None


def test_compare_exact_permutation_small_sample() -> None:
    # pooled [0,0,2,2], C(4,2)=6 splits; only the two all-same splits reach |diff|>=2 -> p = 2/6
    c = compare("lo", [0.0, 0.0], "hi", [2.0, 2.0])
    assert c is not None
    assert c.exact is True
    assert c.p_value == pytest.approx(2 / 6)
    assert c.diff_means == 2.0 and c.diff_ci95 == (2.0, 2.0)   # each arm constant -> difference constant
    assert c.significant is False and c.underpowered is True   # n < 5 per arm


def test_compare_clear_effect_is_significant_and_powered() -> None:
    c = compare("control", [0.0] * 5, "treat", [5.0] * 5)
    assert c is not None
    assert c.exact is True                                     # C(10,5)=252 enumerated
    assert c.p_value == pytest.approx(2 / math.comb(10, 5))
    assert c.significant is True and c.underpowered is False
    assert c.diff_means == 5.0


def test_compare_samples_when_enumeration_is_too_large() -> None:
    a = [float(i % 3) for i in range(12)]
    b = [float(i % 3) + 1 for i in range(12)]                  # C(24,12) ~ 2.7M > PERM_SAMPLES
    c1 = compare("a", a, "b", b)
    c2 = compare("a", a, "b", b)
    assert c1 is not None and c2 is not None
    assert c1.exact is False
    assert 0.0 < c1.p_value <= 1.0
    assert (c1.p_value, c1.diff_ci95) == (c2.p_value, c2.diff_ci95)   # seeded -> reproducible


def test_compare_difference_direction_is_b_minus_a() -> None:
    c = compare("a", [10.0, 10.0], "b", [4.0, 4.0])
    assert c is not None and c.diff_means == -6.0


def test_serialisation_shapes_are_json_friendly() -> None:
    s = summarize([1.0, 2.0, 3.0]).to_dict()
    assert isinstance(s["ci95"], list) and len(s["ci95"]) == 2
    assert summarize([]).to_dict()["ci95"] is None
    c = compare("a", [1.0, 2.0], "b", [3.0, 4.0])
    assert c is not None
    d = c.to_dict()
    assert isinstance(d["diff_ci95"], list) and {"p_value", "significant", "note"} <= d.keys()
