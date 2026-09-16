"""Phase 0 baselines.

Report §6, Phase 0: "Build the eval plane first, with no forecasting at all"
— the only models here are climatologies. They exist to prove the harness
works end to end and to give Phase 1 something to beat. None of them knows
which hazard or which geography it is being scored on; they see only units
of the form (region, year, period) and their training labels.

`ClimatologyPooled` is the contract's named reference: a single number, the
training-period base rate, issued for every unit. `ClimatologySeasonal` is the
first thing with any structure in it — it knows that one region in one part
of the year is not another region in another part of the year — and it is the
candidate Phase 0 actually scores.

`LeakyOracle` is not a forecaster. It exists so the leakage canary has something
to reject, and it lives here rather than in the tests because Phase 0's exit
criteria require demonstrating the rejection as part of a normal run.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from readiness.engine.base import FittedModel
from readiness.harness.labels import Panel, Unit
from readiness.harness.scoring import climatology_reference
from readiness.harness.splits import PredictionRequest, TrainingView


class ClimatologyPooled(FittedModel):
    """One probability for everything: the training-period base rate.

    Perfectly calibrated in aggregate, completely unsharp. This is the bar the
    contract asks candidates to clear, and clearing it is not trivial: a model
    that adds noise without adding signal scores worse.
    """

    name = "climatology-pooled"
    version = "1.0.0"

    def __init__(self) -> None:
        super().__init__()
        self.p: float | None = None

    def _fit(self, view: TrainingView) -> None:
        rows = view.rows()
        # Deliberately calls the harness's own definition rather than
        # recomputing. This model IS the contract's reference forecast, so it
        # must be bit-identical to what the harness uses to compute skill —
        # otherwise it scores non-zero skill against itself. No smoothing: the
        # reference has to be one number computed one way.
        self.p = climatology_reference(sum(y for _, y in rows), len(rows))

    def _predict(self, request: PredictionRequest) -> Sequence[float]:
        assert self.p is not None  # the base class refuses to predict unfitted
        return [self.p] * len(request)


class ClimatologySeasonal(FittedModel):
    """Empirical frequency per (region, period-of-year), smoothed and backed off.

    Sparse cells are the whole difficulty: twenty training years gives twenty
    observations per region-period, so an unsmoothed frequency is 0.00 or 0.05
    and nothing between. The estimate shrinks toward the scope-wide rate for
    that period of the year, and then toward the pooled rate, with the
    shrinkage strength fixed rather than tuned — Phase 0 is not allowed to
    tune anything.
    """

    name = "climatology-seasonal"
    version = "1.0.0"

    def __init__(self, shrinkage: float = 10.0) -> None:
        super().__init__()
        #: Pseudo-observations pulling each cell toward its backoff. Fixed.
        self.shrinkage = shrinkage
        self.by_region_period: dict[tuple[str, int], float] = {}
        self.by_period: dict[int, float] = {}
        self.pooled_rate: float = 0.0

    def _fit(self, view: TrainingView) -> None:
        rows = view.rows()
        rp_pos: dict[tuple[str, int], int] = defaultdict(int)
        rp_n: dict[tuple[str, int], int] = defaultdict(int)
        p_pos: dict[int, int] = defaultdict(int)
        p_n: dict[int, int] = defaultdict(int)
        total_pos = 0

        for (region, _year, period), label in rows:
            rp_pos[(region, period)] += label
            rp_n[(region, period)] += 1
            p_pos[period] += label
            p_n[period] += 1
            total_pos += label

        self.pooled_rate = total_pos / len(rows)
        self.by_period = {
            p: (p_pos[p] + self.shrinkage * self.pooled_rate) / (p_n[p] + self.shrinkage)
            for p in p_n
        }
        self.by_region_period = {
            key: (
                rp_pos[key] + self.shrinkage * self.by_period[key[1]]
            )
            / (rp_n[key] + self.shrinkage)
            for key in rp_n
        }

    def _probability(self, unit: Unit) -> float:
        region, _year, period = unit
        if (region, period) in self.by_region_period:
            return self.by_region_period[(region, period)]
        if period in self.by_period:
            return self.by_period[period]
        return self.pooled_rate

    def _predict(self, request: PredictionRequest) -> Sequence[float]:
        return [self._probability(u) for u in request]


class PersistenceLastYear(FittedModel):
    """Repeat the last *training* year's outcome for the same region-period.

    The name suggests "same period last year", and within the training years
    that is what the history holds. But no holdout label ever reaches a model,
    so at prediction time the only year-before-this-one the model can look up
    is the last training year: the first holdout year repeats that year's
    outcomes, and every later holdout year finds no predecessor in its history
    and gets the `miss` probability everywhere — a constant forecast across
    those years. Version 1.0.0 has always behaved this way and its numbers on
    the committed ledgers stay; only the description was wrong.

    A deliberately crude second baseline. It is sharp and badly calibrated,
    which makes it a useful demonstration that the reliability clause of the
    contract has teeth — it usually fails on calibration while looking
    respectable on AUC.
    """

    name = "persistence-last-year"
    version = "1.0.0"

    def __init__(self, hit: float = 0.35, miss: float = 0.03) -> None:
        super().__init__()
        self.hit = hit
        self.miss = miss
        self.history: dict[Unit, int] = {}

    def _fit(self, view: TrainingView) -> None:
        self.history = {unit: label for unit, label in view.rows()}

    def _predict(self, request: PredictionRequest) -> Sequence[float]:
        out = []
        for region, year, period in request:
            prior = self.history.get((region, year - 1, period))
            out.append(self.hit if prior == 1 else self.miss)
        return out


class LeakyOracle(FittedModel):
    """A model that reads the outcomes it is about to be scored on.

    NOT A FORECASTER. This is the canary's target: it simulates the failure the
    report warns about in §7 — a feature computed over the full panel, a join
    that carries the label column, an agent that opened a file it should not
    have. Phase 0 does not pass until the harness rejects it.

    It cheats by being handed the full panel at construction, which is the one
    thing the harness never does for a real model.
    """

    name = "leaky-oracle"
    version = "1.0.0"

    def __init__(self, full_panel: Panel, confidence: float = 0.999) -> None:
        super().__init__()
        self._answers = dict(zip(full_panel.units, full_panel.labels))
        self.confidence = confidence

    def fit(self, view: TrainingView) -> None:
        # Overrides the base class's `fit` on purpose: it ignores the
        # legitimate channel entirely and reports a digest that cannot match
        # the view, which is check 4 of the canary. A real model never
        # overrides `fit`; that is the one thing this class is for.
        self.training_digest = "0" * 16

    def _predict(self, request: PredictionRequest) -> Sequence[float]:
        hi, lo = self.confidence, 1.0 - self.confidence
        return [hi if self._answers.get(u, 0) == 1 else lo for u in request]
