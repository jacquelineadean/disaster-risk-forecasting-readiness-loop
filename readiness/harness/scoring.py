"""Turn a set of forecasts into a scorecard.

This is where a model's `predict()` output meets labels it was never shown. The
sequence is fixed: build the prediction request, call the model, *then* fetch
the labels. Keeping those steps in that order in one place is what makes the
"no model ever sees a holdout label" invariant checkable by reading one file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping, Protocol, Sequence, runtime_checkable

from readiness.contracts import Contract, Split
from readiness.harness import metrics
from readiness.harness.features import (
    FeatureAdmissionError,
    FeatureAudit,
    FeatureFrame,
    FeatureSource,
    FeatureSpec,
    audit_frame,
    build_frame,
)
from readiness.harness.labels import Panel, Unit
from readiness.harness.splits import (
    PredictionRequest,
    TrainingView,
    get_split,
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
    contract: str
    contract_digest: str
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
    # Trailing, defaulted: cards from Phase 0 have none of these, and the
    # reproducibility fingerprint never reads them.
    feature_digest: str = ""
    feature_columns: tuple[str, ...] = ()
    feature_audit: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def format(self) -> str:
        lines = [
            f"  model            {self.model}@{self.version}",
            f"  contract         {self.contract}  (sha256:{self.contract_digest})",
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
            flag = "" if b["populated"] else "  (thin)"
            lines.append(
                f"    {b['lower']:.2f}-{b['upper']:.2f}  {b['count']:>8,}"
                f"{b['mean_forecast']:>11.4f}{b['observed_frequency']:>11.4f}"
                f"{b['observed_frequency'] - b['mean_forecast']:>+9.4f}{flag}"
            )
        return "\n".join(lines)


def climatology_reference(n_positive: int, n_total: int) -> float:
    """**The** definition of the contract's reference forecast.

    Every contract names `climatology-pooled` as its reference, and every Brier
    Skill Score in this project is measured against it. So the reference must
    be one number computed one way — the unsmoothed training base rate — and
    both the harness and the engine's `ClimatologyPooled` must obtain it from
    here.

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


def _resolve(contract: Contract, split: Split | str) -> Split:
    return get_split(contract, split) if isinstance(split, str) else split


@dataclass(frozen=True)
class _Run:
    """One fit/predict pass, with the labels fetched only after it finished.

    Everything a scorecard or a canary needs comes from here, so a model is
    fitted exactly once per screening and the card and the canary arrays are
    guaranteed to describe the same forecasts.
    """

    split: Split
    view: TrainingView
    train_panel: Panel
    eval_panel: Panel
    probs: list[float]
    outcomes: list[int]
    audit: FeatureAudit | None = None


Sources = Mapping[str, FeatureSource]


def _features(
    model: Model,
    contract: Contract,
    train_panel: Panel,
    eval_panel: Panel,
    sources: Sources | None,
) -> tuple[FeatureFrame | None, FeatureFrame | None, FeatureAudit | None]:
    """Build and audit the frames a model asks for, or nothing at all.

    A model declares `feature_specs`; the harness builds the rows for the
    training and the scored units from the loaded sources under its own
    cutoffs, audits the whole frame, and refuses to go on unless the audit is
    clean. A model with no specs gets no frame, and a Phase 0 run is
    byte-identical to before.
    """
    specs: tuple[FeatureSpec, ...] = tuple(getattr(model, "feature_specs", ()) or ())
    if not specs:
        return None, None, None
    if not sources:
        raise FeatureAdmissionError(
            f"{model.name} declares {len(specs)} feature column(s) but no feature "
            "sources were loaded; run with the sources the contract's data pins"
        )
    units = list(train_panel.units) + list(eval_panel.units)
    frame = build_frame(specs, sources, units, contract.periods_per_year)
    audit = audit_frame(specs, sources, units, contract, frame)
    if not audit.clean:
        raise FeatureAdmissionError(
            f"features refused for {model.name}:\n{audit.format()}"
        )
    return frame.restrict(train_panel.units), frame.restrict(eval_panel.units), audit


def _fit_predict(
    model: Model,
    panel: Panel,
    contract: Contract,
    split: Split,
    sources: Sources | None = None,
) -> _Run:
    """The fixed sequence: training view, fit, bare request, predict, *then* labels.

    This is the one place the sequence is written down. `score`, `predictions_for`
    and `screen` all go through it, so the "no model ever sees a holdout label"
    invariant is audited by reading this function and nothing else. Features,
    when a model asks for them, are built and audited before the view exists,
    from units alone.
    """
    train_split = contract.splits.train
    train_panel = split_panel(panel, train_split)
    eval_panel = split_panel(panel, split)

    train_frame, eval_frame, audit = _features(
        model, contract, train_panel, eval_panel, sources
    )
    view = TrainingView(train_panel, train_split, train_frame)
    model.fit(view)

    request = PredictionRequest.from_panel(eval_panel, split, eval_frame)
    probs = list(model.predict(request))
    if len(probs) != len(request):
        raise ValueError(
            f"{model.name} returned {len(probs)} forecasts for "
            f"{len(request)} units"
        )

    # Labels are fetched only now — after predict() has returned.
    outcomes = list(eval_panel.labels)
    return _Run(split, view, train_panel, eval_panel, probs, outcomes, audit)


def _scorecard(model: Model, contract: Contract, run: _Run) -> Scorecard:
    """Every number on the card, from one run's forecasts and outcomes."""
    probs, outcomes = run.probs, run.outcomes
    p_ref = _reference_probability(run.train_panel)
    reference_probs = [p_ref] * len(outcomes)

    bs = metrics.brier_score(probs, outcomes)
    bs_ref = metrics.brier_score(reference_probs, outcomes)
    murphy = metrics.murphy_decomposition(probs, outcomes, contract.n_reliability_bins)
    table = metrics.reliability_table(probs, outcomes, contract.n_reliability_bins)

    return Scorecard(
        model=model.name,
        version=model.version,
        contract=contract.name,
        contract_digest=contract.digest(),
        split=run.split.name,
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
                "populated": b.count >= contract.reliability_min_bin_count,
                "mean_forecast": b.mean_forecast if b.count else None,
                "observed_frequency": b.observed_frequency if b.count else None,
            }
            for b in table
        ),
        panel_digest=run.eval_panel.digest(),
        train_digest=run.view.digest,
        feature_digest=run.view.feature_digest,
        feature_columns=run.view.features.columns if run.view.features else (),
        feature_audit=run.audit.to_dict() if run.audit is not None else None,
    )


def score(
    model: Model,
    panel: Panel,
    contract: Contract,
    split: Split | str,
    *,
    sources: Sources | None = None,
) -> Scorecard:
    """Fit `model` on the contract's training split and score it against `split`.

    The reference forecast is always the harness's own constant climatology —
    the one the contract names. There is deliberately no way to pass another:
    a caller-supplied reference would make the skill score measure whatever
    the caller chose. `sources` are the loaded feature sources, used only when
    the model declares feature specs.
    """
    split = _resolve(contract, split)
    return _scorecard(model, contract, _fit_predict(model, panel, contract, split, sources))


def predictions_for(
    model: Model,
    panel: Panel,
    contract: Contract,
    split: Split | str,
    *,
    sources: Sources | None = None,
) -> tuple[list[Unit], list[float], list[int]]:
    """Same fit/predict sequence as `score`, exposing the raw arrays.

    Used by the reproducibility guard, which pins the forecasts themselves
    rather than the summary statistics.
    """
    run = _fit_predict(model, panel, contract, _resolve(contract, split), sources)
    return list(run.eval_panel.units), run.probs, run.outcomes


def screen(
    model: Model,
    panel: Panel,
    contract: Contract,
    split: Split | str,
    *,
    sources: Sources | None = None,
):
    """Score a model and run the leakage canary over the same forecasts.

    Returns `(scorecard, canary_report)`. Every command that scores anything
    goes through here so that a scorecard is never produced without its canary,
    and the model is fitted once: the card and the canary describe one set of
    probabilities, not two runs that are merely assumed to agree.
    """
    from readiness.harness import canary as canary_mod

    run = _fit_predict(model, panel, contract, _resolve(contract, split), sources)
    card = _scorecard(model, contract, run)
    report = canary_mod.run(
        probs=run.probs,
        outcomes=run.outcomes,
        brier_skill_score=card.brier_skill_score,
        auc=card.auc,
        view=run.view,
        declared_train_digest=getattr(model, "training_digest", None),
        declared_feature_digest=getattr(model, "feature_digest", None),
        frame_digest=run.view.feature_digest or None,
    )
    return card, report
