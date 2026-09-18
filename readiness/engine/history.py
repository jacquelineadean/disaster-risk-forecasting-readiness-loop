"""Label-derived features, computed from training labels and nothing else.

Report §6 asks Phase 1 to build features from "historical frequencies". The
only historical frequency a model may know is the one in its own training
labels, and the `TrainingView` already protects those: a source built from
the ground truth is refused as a feature outright by the harness, so the
history a model uses has to be derived inside `fit()` from `view.rows()`.

The number is the seasonal climatology's shrunk rate for a (region, period)
cell — the same arithmetic, through `baseline.seasonal_rates` — expressed as
a logit so a linear model can put a coefficient on it.

The asymmetry this module exists for
-------------------------------------
For a *training* row the rate excludes that row's whole year at every level of
the back-off (leave-one-year-out); for a *holdout* unit it uses every training
year. A model that used the in-sample rate for its training rows would be
fitting a coefficient on a feature that already contains each row's own
label: the shrunk cell rate moves with the row's outcome, so the fit learns
an optimistic coefficient on its own labels and then emits
sharper-than-warranted probabilities on the holdout, where the feature no
longer contains the answer. That is exactly the failure the contract's
reliability clause punishes — a forecast that is more confident than the
observed frequencies justify. Leaving out the whole year, rather than the row
alone, gives the training rows a feature of the same kind the holdout rows
will get: a rate computed without the year being forecast. This is the regime
`PersistenceLastYear` already lives under: nothing a model sees at fit time
may contain the label it is being fitted to.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Sequence

from readiness.engine.baseline import seasonal_rates
from readiness.harness.labels import Unit

__all__ = ["HistoryFeatures", "PROB_FLOOR", "clip", "logit"]

#: Probabilities are clipped away from 0 and 1 before the logit so a cell
#: with no positives in twenty years is "very unlikely", not minus infinity.
PROB_FLOOR = 1e-4


def clip(p: float, lo: float, hi: float) -> float:
    return lo if p < lo else hi if p > hi else p


def logit(p: float) -> float:
    return math.log(p / (1.0 - p))


class HistoryFeatures:
    """The shrunk seasonal rate per (region, period), as a logit, from labels.

    `fit(rows)` stores the counts; `logit(unit, in_sample)` returns the feature
    for one unit. Holdout units get the rate `ClimatologySeasonal` would issue
    (`seasonal_rates` is shared, so it is the identical number); training rows
    get the leave-one-year-out rate described in the module docstring.
    """

    def __init__(self, shrinkage: float = 10.0) -> None:
        self.shrinkage = shrinkage
        self.pooled_rate = 0.0
        self.by_period: dict[int, float] = {}
        self.by_region_period: dict[tuple[str, int], float] = {}
        self._label: dict[Unit, int] = {}
        self._cell: dict[tuple[str, int], list[int]] = {}
        self._period: dict[int, list[int]] = {}
        self._period_year: dict[tuple[int, int], list[int]] = {}
        self._year: dict[int, list[int]] = {}
        self._total = [0, 0]

    def fit(self, rows: Sequence[tuple[Unit, int]]) -> "HistoryFeatures":
        """Count positives and rows per cell, per period, per year and overall.

        The per-year and per-period-and-year counts are what leave-one-year-out
        subtracts: a training row's whole year comes out of every level, not
        only the row itself.
        """
        self.pooled_rate, self.by_period, self.by_region_period = seasonal_rates(
            rows, self.shrinkage
        )
        cell: dict[tuple[str, int], list[int]] = defaultdict(lambda: [0, 0])
        period: dict[int, list[int]] = defaultdict(lambda: [0, 0])
        period_year: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])
        year: dict[int, list[int]] = defaultdict(lambda: [0, 0])
        total = [0, 0]
        self._label = {}
        for (region, yr, per), label in rows:
            self._label[(region, yr, per)] = label
            counters = (cell[(region, per)], period[per], period_year[(per, yr)],
                        year[yr], total)
            for counter in counters:
                counter[0] += label
                counter[1] += 1
        self._cell, self._period, self._total = dict(cell), dict(period), total
        self._period_year, self._year = dict(period_year), dict(year)
        return self

    def rate(self, unit: Unit, in_sample: bool) -> float:
        """The shrunk rate for a unit's cell, with or without its own row."""
        region, _year, period = unit
        if not in_sample:
            return self._holdout_rate(region, period)
        return self._loyo_rate(unit)

    def logit(self, unit: Unit, in_sample: bool) -> float:
        """`rate()` clipped to [PROB_FLOOR, 1 - PROB_FLOOR] and put on the logit scale."""
        return logit(clip(self.rate(unit, in_sample), PROB_FLOOR, 1.0 - PROB_FLOOR))

    def _holdout_rate(self, region: str, period: int) -> float:
        """Every training year, backed off exactly as `ClimatologySeasonal` does."""
        if (region, period) in self.by_region_period:
            return self.by_region_period[(region, period)]
        if period in self.by_period:
            return self.by_period[period]
        return self.pooled_rate

    def _loyo_rate(self, unit: Unit) -> float:
        """The training row's whole *year* removed from every level, then shrunk.

        Leave-one-year-out has to mean the year: a rate that dropped only the
        row itself would still be backed by the pooled and seasonal counts of
        the other regions in the same year, and those move with whatever made
        that year wet. The cell (region, period) holds exactly one row per
        year, so its own row is its whole year; the period and the pool each
        lose every row the year contributed.

        With the year gone, a cell that held only that row has zero
        observations and the formula returns its parent rate unchanged: the
        same back-off the holdout path takes for an unseen cell.
        """
        region, year, period = unit
        try:
            own = self._label[unit]
        except KeyError:
            raise ValueError(f"{unit} is not a training row; use in_sample=False") from None
        year_pos, year_n = self._year[year]
        pos, n = self._total
        pooled = (pos - year_pos) / (n - year_n) if n > year_n else 0.0
        pos, n = self._period[period]
        drop_pos, drop_n = self._period_year[(period, year)]
        by_period = (pos - drop_pos + self.shrinkage * pooled) / (
            n - drop_n + self.shrinkage
        )
        pos, n = self._cell[(region, period)]
        return (pos - own + self.shrinkage * by_period) / (n - 1 + self.shrinkage)
