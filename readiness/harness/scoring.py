"""Turn a set of forecasts into a scorecard.

This is where a model's `predict()` output meets labels it was never shown. The
sequence is fixed: build the prediction request, call the model, *then* fetch
the labels. Keeping those steps in that order in one place is what makes the
"no model ever sees a holdout label" invariant checkable by reading one file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol, Sequence, runtime_checkable

from readiness.config import CONTRACT
from readiness.harness import metrics
from readiness.harness.labels import Panel, Unit
from readiness.harness.splits import (
    TRAIN,
    PredictionRequest,
    Split,
    TrainingView,
    split_panel,
)


@runtime_checkable
class Model(Protocol):
    """What the engine must provide. Deliberately tiny.

    A model sees a `TrainingView` (training years only) and then a list of bare
    units. It never receives an outcome for anything it is scored on.
    """

    name: str
    version: str

    def fit(self, view: TrainingView) -> None: ...

    def predict(self, request: PredictionRequest) -> Sequence[float]: ...


@dataclass(frozen=True)
class Scorecard:
    """Every number the contract needs, plus the provenance to reproduce it."""

    model: str
    version: str
    split: str
    n_units: int
    n_positive: int
    base_rate: float
    brier_score: float
    brier_score_reference: float
    brier_skill_score: float
    auc: float
    sharpness: float
    reliability: float
    resolution: float
    uncertainty: float
    reliability_bins: tuple[dict, ...]
    panel_digest: str
    train_digest: str
    contract_digest: str = field(default_factory=CONTRACT.digest)

    def to_dict(self) -> dict:
        return asdict(self)

    def format(self) -> str:
        lines = [
            f"  model            {self.model}@{self.version}",
            f"  split            {self.split}  "
            f"({self.n_units:,} units, {self.n_positive:,} positive, "
            f"base rate {self.base_rate:.4f})",
            f"  brier            {self.brier_score:.6f}"
            f"   (reference {self.brier_score_reference:.6f})",
            f"  brier skill      {self.brier_skill_score:+.4f}",
            f"  auc              {self.auc:.4f}",
            f"  sharpness        {self.sharpness:.4f}",
            f"  murphy           reliability {self.reliability:.6f}"
            f"  resolution {self.resolution:.6f}"
            f"  uncertainty {self.uncertainty:.6f}",
            "  reliability diagram",
            f"    {'bin':<12}{'n':>8}{'forecast':>11}{'observed':>11}{'dev':>9}",
        ]
        for b in self.reliability_bins:
            if not b["count"]:
                continue
            flag = "" if b["count"] >= CONTRACT.reliability_min_bin_count else "  (thin)"
            lines.append(
                f"    {b['lower']:.2f}-{b['upper']:.2f}  {b['count']:>8,}"
                f"{b['mean_forecast']:>11.4f}{b['observed_frequency']:>11.4f}"
                f"{b['observed_frequency'] - b['mean_forecast']:>+9.4f}{flag}"
            )
        return "\n".join(lines)


def climatology_reference(n_positive: int, n_total: int) -> float:
    """**The** definition of the contract's reference forecast.

    `CONTRACT.reference_model` names `climatology-global`, and every Brier Skill
    Score in this project is measured against it. So the reference must be one
    number computed one way — the unsmoothed training base rate — and both the
    harness and the engine's `ClimatologyGlobal` must obtain it from here.

    An earlier version let the engine apply Laplace smoothing while the harness
    used the raw rate. The difference was ~1e-4 in probability, and it meant the
    reference model scored a non-zero skill *against itself*: the yardstick was
    not the thing it claimed to measure. Keep this as the single source.

    Lives in the harness because the reference is part of the measuring
    apparatus, not a model the agent may propose or swap.
    """
    if n_total <= 0:
        raise ValueError("cannot compute a base rate over zero units")
    return n_positive / n_total


def _reference_probability(train_panel: Panel) -> float:
    return climatology_reference(sum(train_panel.labels), len(train_panel))


def score(
    model: Model,
    panel: Panel,
    split: Split,
    *,
    train_split: Split = TRAIN,
    reference_probs: Sequence[float] | None = None,
) -> Scorecard:
    """Fit `model` on the training split and score it against `split`.

    `reference_probs` lets a caller supply an explicit reference forecast; by
    default the harness uses its own constant climatology, which is what the
    contract names.
    """
    train_panel = split_panel(panel, train_split)
    eval_panel = split_panel(panel, split)

    view = TrainingView(train_panel, train_split)
    model.fit(view)

    request = PredictionRequest.from_panel(eval_panel, split)
    probs = list(model.predict(request))
    if len(probs) != len(request):
        raise ValueError(
            f"{model.name} returned {len(probs)} forecasts for "
            f"{len(request)} units"
        )

    # Labels are fetched only now — after predict() has returned.
    outcomes = list(eval_panel.labels)

    if reference_probs is None:
        p_ref = _reference_probability(train_panel)
        reference_probs = [p_ref] * len(outcomes)

    bs = metrics.brier_score(probs, outcomes)
    bs_ref = metrics.brier_score(reference_probs, outcomes)
    murphy = metrics.murphy_decomposition(probs, outcomes, CONTRACT.n_reliability_bins)
    table = metrics.reliability_table(probs, outcomes, CONTRACT.n_reliability_bins)

    return Scorecard(
        model=model.name,
        version=model.version,
        split=split.name,
        n_units=len(outcomes),
        n_positive=sum(outcomes),
        base_rate=metrics.base_rate(outcomes),
        brier_score=bs,
        brier_score_reference=bs_ref,
        brier_skill_score=metrics.brier_skill_score(bs, bs_ref),
        auc=metrics.roc_auc(probs, outcomes),
        sharpness=metrics.sharpness(probs),
        reliability=murphy.reliability,
        resolution=murphy.resolution,
        uncertainty=murphy.uncertainty,
        reliability_bins=tuple(
            {
                "lower": b.lower,
                "upper": b.upper,
                "count": b.count,
                "mean_forecast": b.mean_forecast if b.count else None,
                "observed_frequency": b.observed_frequency if b.count else None,
            }
            for b in table
        ),
        panel_digest=eval_panel.digest(),
        train_digest=view.digest,
    )


def predictions_for(
    model: Model, panel: Panel, split: Split, *, train_split: Split = TRAIN
) -> tuple[list[Unit], list[float], list[int]]:
    """Same fit/predict sequence as `score`, exposing the raw arrays.

    Used by the canary, which needs to inspect the forecasts themselves rather
    than the summary statistics.
    """
    train_panel = split_panel(panel, train_split)
    eval_panel = split_panel(panel, split)
    view = TrainingView(train_panel, train_split)
    model.fit(view)
    request = PredictionRequest.from_panel(eval_panel, split)
    probs = list(model.predict(request))
    return list(eval_panel.units), probs, list(eval_panel.labels)
