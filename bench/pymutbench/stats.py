"""Statistics of the benchmark: intervals and paired tests over bugs nested in projects (stdlib only).

* ``wilson`` — the Wilson score interval of a binomial proportion;
* ``cluster_bootstrap`` — a percentile interval that resamples whole projects (bugs of one project are not
  independent), with a fixed seed;
* ``mcnemar_midp`` — the McNemar mid-p test of a paired binary outcome (Fagerland, Lydersen and Laake 2013);
* ``holm`` — Holm's step-down adjustment of a family of p-values.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence


def wilson(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def cluster_bootstrap(
    clusters: dict[str, list[float]],
    statistic: Callable[[dict[str, list[float]]], float],
    *,
    replicates: int = 10000,
    seed: int = 20261105,
    alpha: float = 0.05,
) -> tuple[float, float]:
    names = sorted(clusters)
    if not names:
        return (math.nan, math.nan)
    rng = random.Random(seed)
    values = []
    for _ in range(replicates):
        sample: dict[str, list[float]] = {}
        for index in range(len(names)):
            name = rng.choice(names)
            sample[f"{name}#{index}"] = clusters[name]
        values.append(statistic(sample))
    values.sort()
    low = values[int(math.floor(alpha / 2 * replicates))]
    high = values[min(replicates - 1, int(math.ceil((1 - alpha / 2) * replicates)) - 1)]
    return (low, high)


def pooled_mean(clusters: dict[str, list[float]]) -> float:
    flat = [v for values in clusters.values() for v in values]
    return sum(flat) / len(flat) if flat else math.nan


def project_mean(clusters: dict[str, list[float]]) -> float:
    """The mean of per-project means: every project weighs the same, however many bugs it contributes."""
    means = [sum(v) / len(v) for v in clusters.values() if v]
    return sum(means) / len(means) if means else math.nan


def _binom_pmf(k: int, n: int) -> float:
    return math.comb(n, k) * 0.5**n


def mcnemar_midp(only_first: int, only_second: int) -> float:
    """Two-sided mid-p of McNemar's test from the discordant counts (Fagerland et al. 2013, eq. 5-6).

    For unequal counts it is twice the lower tail with half the probability of the observed count; for equal
    counts the two tails overlap and the mid-p is ``1 - f(b | n) / 2``. It ignores the clustering of bugs in
    projects: the project-bootstrap interval of the paired difference is the primary inference.
    """
    n = only_first + only_second
    if n == 0:
        return 1.0
    low = min(only_first, only_second)
    if only_first == only_second:
        return 1.0 - 0.5 * _binom_pmf(low, n)
    return min(1.0, 2 * (sum(_binom_pmf(i, n) for i in range(low)) + 0.5 * _binom_pmf(low, n)))


def holm(pvalues: Sequence[float]) -> list[float]:
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted = [0.0] * len(pvalues)
    running = 0.0
    m = len(pvalues)
    for rank, index in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[index]))
        adjusted[index] = running
    return adjusted


def hit_probability(population: int, hits: int, draws: int) -> float:
    """P(a uniform sample of ``draws`` of ``population`` items contains at least one of ``hits``)."""
    if population <= 0 or hits <= 0:
        return 0.0
    draws = min(draws, population)
    if population - hits < draws:
        return 1.0
    return 1.0 - math.comb(population - hits, draws) / math.comb(population, draws)
