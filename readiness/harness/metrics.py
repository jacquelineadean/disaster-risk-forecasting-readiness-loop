"""Scoring rules for probabilistic binary forecasts.

Pure standard library, deliberately. The eval plane is the one component that
must produce identical numbers on any machine from a clean clone, so it takes no
dependency whose version could move under it. No LLM is involved here and none
ever should be — see the module docstring in `readiness/harness/__init__.py`.

References (report §5, source [19]): Brier (1950); Murphy (1973) decomposition;
ECMWF Forecast User Guide §12.B.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


class MetricError(ValueError):
    """Raised when inputs cannot be scored (rather than returning a fake number)."""


def _check(probs: Sequence[float], outcomes: Sequence[int]) -> None:
    if len(probs) != len(outcomes):
        raise MetricError(
            f"length mismatch: {len(probs)} forecasts vs {len(outcomes)} outcomes"
        )
    if not probs:
        raise MetricError("cannot score an empty forecast set")
    for p in probs:
        if not (0.0 <= p <= 1.0):
            raise MetricError(f"forecast probability out of [0,1]: {p!r}")
    for y in outcomes:
        if y not in (0, 1):
            raise MetricError(f"outcome must be 0 or 1, got {y!r}")


def base_rate(outcomes: Sequence[int]) -> float:
    """Observed event frequency — the climatological base rate."""
    if not outcomes:
        raise MetricError("cannot take base rate of an empty set")
    return sum(outcomes) / len(outcomes)


def brier_score(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error between forecast probability and the 0/1 outcome.

    Strictly proper: a forecaster minimises it only by reporting its true
    belief, so an iterating agent cannot improve its score by hedging.
    """
    _check(probs, outcomes)
    return sum((p - y) ** 2 for p, y in zip(probs, outcomes)) / len(probs)


def brier_skill_score(bs: float, bs_reference: float) -> float:
    """Fractional improvement over a reference forecast. >0 beats the reference.

    Raw Brier scores are not comparable across hazards with different base
    rates; skill relative to a stated reference is. The contract is written in
    terms of this number, never the raw score.
    """
    if bs_reference <= 0:
        raise MetricError(
            "reference Brier score is zero or negative; skill is undefined "
            "(a perfect reference means there is nothing to improve on)"
        )
    return 1.0 - (bs / bs_reference)


def sharpness(probs: Sequence[float]) -> float:
    """Standard deviation of the forecasts.

    A forecast that always issues the base rate is perfectly calibrated and
    perfectly useless. Sharpness is reported alongside reliability so that
    failure mode is visible rather than flattering.
    """
    if not probs:
        raise MetricError("cannot measure sharpness of an empty set")
    mean = sum(probs) / len(probs)
    var = sum((p - mean) ** 2 for p in probs) / len(probs)
    return var**0.5


@dataclass(frozen=True)
class ReliabilityBin:
    """One row of a reliability diagram."""

    lower: float
    upper: float
    count: int
    mean_forecast: float
    observed_frequency: float

    @property
    def deviation(self) -> float:
        """Signed calibration error in probability units. Positive = under-forecast."""
        return self.observed_frequency - self.mean_forecast


def reliability_table(
    probs: Sequence[float], outcomes: Sequence[int], n_bins: int = 10
) -> list[ReliabilityBin]:
    """Bin forecasts and compare mean forecast to observed frequency in each bin.

    This is the reliability diagram in tabular form: perfect calibration is
    `observed_frequency == mean_forecast` in every bin (the diagonal).
    """
    _check(probs, outcomes)
    if n_bins < 2:
        raise MetricError("need at least 2 reliability bins")

    buckets: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for p, y in zip(probs, outcomes):
        # Right-closed on the final bin so p == 1.0 lands in the last bucket.
        idx = min(int(p * n_bins), n_bins - 1)
        buckets[idx].append((p, y))

    table: list[ReliabilityBin] = []
    for i, bucket in enumerate(buckets):
        lower, upper = i / n_bins, (i + 1) / n_bins
        if not bucket:
            table.append(ReliabilityBin(lower, upper, 0, float("nan"), float("nan")))
            continue
        n = len(bucket)
        table.append(
            ReliabilityBin(
                lower=lower,
                upper=upper,
                count=n,
                mean_forecast=sum(p for p, _ in bucket) / n,
                observed_frequency=sum(y for _, y in bucket) / n,
            )
        )
    return table


@dataclass(frozen=True)
class MurphyDecomposition:
    """BS = reliability - resolution + uncertainty (Murphy 1973)."""

    reliability: float
    resolution: float
    uncertainty: float

    @property
    def recomposed(self) -> float:
        return self.reliability - self.resolution + self.uncertainty


def murphy_decomposition(
    probs: Sequence[float], outcomes: Sequence[int], n_bins: int = 10
) -> MurphyDecomposition:
    """Split the Brier score into its three interpretable parts.

    reliability  do predicted 20%s happen 20% of the time? (lower is better)
    resolution   does the forecast distinguish situations? (higher is better)
    uncertainty  the base rate's own variance — outside anyone's control

    The decomposition is exact only for the binned forecasts, so `recomposed`
    will differ slightly from `brier_score` on continuous forecasts. The caller
    is expected to report both rather than pretend otherwise.
    """
    _check(probs, outcomes)
    n = len(probs)
    table = reliability_table(probs, outcomes, n_bins)
    obar = base_rate(outcomes)

    reliability = sum(
        b.count * (b.mean_forecast - b.observed_frequency) ** 2
        for b in table
        if b.count
    ) / n
    resolution = sum(
        b.count * (b.observed_frequency - obar) ** 2 for b in table if b.count
    ) / n
    uncertainty = obar * (1.0 - obar)
    return MurphyDecomposition(reliability, resolution, uncertainty)


def roc_auc(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Area under the ROC curve, via the Mann-Whitney U identity.

    Equals the probability that a randomly chosen positive is ranked above a
    randomly chosen negative. Ties receive half credit, which is what makes a
    constant forecast (climatology) score exactly 0.5 rather than 0 or 1.
    """
    _check(probs, outcomes)
    n_pos = sum(outcomes)
    n_neg = len(outcomes) - n_pos
    if n_pos == 0 or n_neg == 0:
        raise MetricError(
            "AUC is undefined when outcomes are all one class "
            f"(positives={n_pos}, negatives={n_neg})"
        )

    # Rank with ties averaged, then apply U = R_pos - n_pos(n_pos+1)/2.
    order = sorted(range(len(probs)), key=lambda i: probs[i])
    ranks = [0.0] * len(probs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and probs[order[j + 1]] == probs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # 1-based, averaged over the tie group
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1

    rank_sum_pos = sum(r for r, y in zip(ranks, outcomes) if y == 1)
    u = rank_sum_pos - n_pos * (n_pos + 1) / 2.0
    return u / (n_pos * n_neg)
