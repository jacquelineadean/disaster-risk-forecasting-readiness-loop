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

from readiness.harness.labels import Panel, Unit
from readiness.harness.scoring import climatology_reference
from readiness.harness.splits import PredictionRequest, TrainingView


class ClimatologyPooled:
    """One probability for everything: the training-period base rate.

    Perfectly calibrated in aggregate, completely unsharp. This is the bar the
    contract asks candidates to clear, and clearing it is not trivial: a model
    that adds noise without adding signal scores worse.
    """

    name = "climatology-pooled"
    version = "1.0.0"

    def __init__(self) -> None:
        self.p: float | None = None
        self.training_digest: str | None = None

    def fit(self, view: TrainingView) -> None:
        rows = view.rows()
        # Deliberately calls the harness's own definition rather than
        # recomputing. This model IS the contract's reference forecast, so it
        # must be bit-identical to what the harness uses to compute skill —
        # otherwise it scores non-zero skill against itself. No smoothing: the
        # reference has to be one number computed one way.
        self.p = climatology_reference(sum(y for _, y in rows), len(rows))
        self.training_digest = view.digest

    def predict(self, request: PredictionRequest) -> Sequence[float]:
        if self.p is None:
            raise RuntimeError(f"{self.name} was not fitted")
        return [self.p] * len(request)


class ClimatologySeasonal:
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
        #: Pseudo-observations pulling each cell toward its backoff. Fixed.
        self.shrinkage = shrinkage
        self.by_region_period: dict[tuple[str, int], float] = {}
        self.by_period: dict[int, float] = {}
        self.pooled_rate: float = 0.0
        self.training_digest: str | None = None

    def fit(self, view: TrainingView) -> None:
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
        self.training_digest = view.digest

    def _probability(self, unit: Unit) -> float:
        region, _year, period = unit
        if (region, period) in self.by_region_period:
            return self.by_region_period[(region, period)]
        if period in self.by_period:
            return self.by_period[period]
        return self.pooled_rate

    def predict(self, request: PredictionRequest) -> Sequence[float]:
        if self.training_digest is None:
            raise RuntimeError(f"{self.name} was not fitted")
        return [self._probability(u) for u in request]


class PersistenceLastYear:
    """Did this region-period have a damaging event in the same period last year?

    A deliberately crude second baseline. It is sharp and badly calibrated,
    which makes it a useful demonstration that the reliability clause of the
    contract has teeth — it usually fails on calibration while looking
    respectable on AUC.
    """

    name = "persistence-last-year"
    version = "1.0.0"

    def __init__(self, hit: float = 0.35, miss: float = 0.03) -> None:
        self.hit = hit
        self.miss = miss
        self.history: dict[Unit, int] = {}
        self.training_digest: str | None = None

    def fit(self, view: TrainingView) -> None:
        self.history = {unit: label for unit, label in view.rows()}
        self.training_digest = view.digest

    def predict(self, request: PredictionRequest) -> Sequence[float]:
        if self.training_digest is None:
            raise RuntimeError(f"{self.name} was not fitted")
        out = []
        for region, year, period in request:
            prior = self.history.get((region, year - 1, period))
            out.append(self.hit if prior == 1 else self.miss)
        return out


class LeakyOracle:
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
        self._answers = dict(zip(full_panel.units, full_panel.labels))
        self.confidence = confidence
        self.training_digest: str | None = None

    def fit(self, view: TrainingView) -> None:
        # Ignores the legitimate channel entirely — and reports a digest that
        # does not match, which is check 4 of the canary.
        self.training_digest = "0" * 16

    def predict(self, request: PredictionRequest) -> Sequence[float]:
        hi, lo = self.confidence, 1.0 - self.confidence
        return [hi if self._answers.get(u, 0) == 1 else lo for u in request]
