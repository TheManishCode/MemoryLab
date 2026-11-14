"""Small, dependency-free statistics for experiment arms and their A/B comparison.

An experiment is a one-variable A/B: its runs differ in exactly one configuration field (the
*variable*), and the question is whether that field changes the score. Scores are small integers
over few runs, so everything here is nonparametric and makes no normality assumption:

- an arm's uncertainty is a **percentile bootstrap** 95% CI for its mean;
- the difference between two arms is tested with an **exact permutation test** (every way to relabel
  the pooled scores) when that is cheap, and a seeded sample of relabelings otherwise.

Everything is deterministic: the same scores always give the same numbers (a fixed seed), so an
experiment's statistics do not drift between requests and the unit tests can assert exact values.
Nothing here does I/O; it is pure and fully unit-tested.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import asdict, dataclass
from itertools import combinations
from typing import Any

SEED = 20261114            # fixed: an experiment's statistics are stable across requests and in tests
BOOTSTRAP = 2000           # resamples for a mean / difference CI
PERM_SAMPLES = 20000       # relabelings sampled when exact enumeration would be larger than this
MIN_ARM_N = 5              # below this an arm (or a comparison) is flagged underpowered, not a finding
_CI = 0.95


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted list (NumPy's default 'linear' method)."""
    if not sorted_values:
        raise ValueError("percentile of no data")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _bootstrap_means(values: list[float], rng: random.Random, resamples: int) -> list[float]:
    n = len(values)
    return [statistics.fmean(rng.choices(values, k=n)) for _ in range(resamples)]


def _ci_bounds(samples: list[float], level: float = _CI) -> tuple[float, float]:
    s = sorted(samples)
    tail = (1 - level) / 2
    return (_percentile(s, tail), _percentile(s, 1 - tail))


@dataclass(frozen=True)
class ArmStats:
    n: int
    mean: float | None
    median: float | None
    stdev: float | None                 # sample stdev; None for n < 2
    min: float | None
    max: float | None
    ci95: tuple[float, float] | None     # bootstrap 95% CI for the mean; None for n < 2

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ci95"] = list(self.ci95) if self.ci95 is not None else None
        return d


def summarize(values: list[float], *, seed: int = SEED) -> ArmStats:
    """Point estimates plus a bootstrap 95% CI for the mean. Empty input gives an all-None `ArmStats`."""
    n = len(values)
    if n == 0:
        return ArmStats(0, None, None, None, None, None, None)
    mean = statistics.fmean(values)
    median = statistics.median(values)
    if n == 1:
        return ArmStats(1, mean, median, None, values[0], values[0], None)
    rng = random.Random(seed)
    ci = _ci_bounds(_bootstrap_means(values, rng, BOOTSTRAP))
    return ArmStats(n, mean, median, statistics.stdev(values), min(values), max(values), ci)


@dataclass(frozen=True)
class Comparison:
    a_label: str
    b_label: str
    diff_means: float               # mean(b) - mean(a)
    diff_ci95: tuple[float, float]  # bootstrap 95% CI for that difference
    p_value: float                  # two-sided permutation test on the difference of means
    exact: bool                     # True when every relabeling was enumerated
    significant: bool               # p_value < 0.05
    underpowered: bool              # either arm has fewer than MIN_ARM_N runs
    note: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["diff_ci95"] = list(self.diff_ci95)
        return d


def _permutation_p(a: list[float], b: list[float], seed: int) -> tuple[float, bool]:
    """Two-sided p for |mean(b) - mean(a)| under relabeling of the pooled scores. Returns (p, exact)."""
    pool = a + b
    na, total = len(a), len(a) + len(b)
    observed = abs(statistics.fmean(b) - statistics.fmean(a))
    eps = 1e-9
    n_splits = math.comb(total, na)
    if n_splits <= PERM_SAMPLES:                       # exact: enumerate every way to pick arm A
        extreme = 0
        for idx in combinations(range(total), na):
            pick = set(idx)
            group_a = [pool[i] for i in idx]
            group_b = [pool[i] for i in range(total) if i not in pick]
            if abs(statistics.fmean(group_b) - statistics.fmean(group_a)) >= observed - eps:
                extreme += 1
        return extreme / n_splits, True
    rng = random.Random(seed)                          # sampled, with the +1 correction (p is never 0)
    extreme = 0
    for _ in range(PERM_SAMPLES):
        shuffled = pool[:]
        rng.shuffle(shuffled)
        group_a, group_b = shuffled[:na], shuffled[na:]
        if abs(statistics.fmean(group_b) - statistics.fmean(group_a)) >= observed - eps:
            extreme += 1
    return (extreme + 1) / (PERM_SAMPLES + 1), False


def compare(a_label: str, a: list[float], b_label: str, b: list[float], *, seed: int = SEED) -> Comparison | None:
    """Compare two arms. None if either arm has no scored runs (there is nothing to compare)."""
    if not a or not b:
        return None
    diff = statistics.fmean(b) - statistics.fmean(a)
    rng = random.Random(seed + 1)
    diffs = [mb - ma for ma, mb in zip(_bootstrap_means(a, rng, BOOTSTRAP),
                                       _bootstrap_means(b, rng, BOOTSTRAP), strict=True)]
    diff_ci = _ci_bounds(diffs)
    p, exact = _permutation_p(a, b, seed)
    significant = p < 0.05
    underpowered = min(len(a), len(b)) < MIN_ARM_N
    if significant and underpowered:
        note = "A difference is detectable, but with fewer than 5 runs in an arm; add runs to confirm."
    elif significant:
        note = "The difference is unlikely under no effect (p < 0.05)."
    elif underpowered:
        note = "No difference detected, but fewer than 5 runs in an arm: this is not evidence of no effect."
    else:
        note = "No significant difference at this sample size."
    return Comparison(a_label, b_label, diff, diff_ci, p, exact, significant, underpowered, note)
