"""Named model constructors, so the agent can request a model by string.

Keeping construction behind a registry means an agent (or a CLI user) never
imports engine classes directly and never gets to pass a panel into a
constructor — which is exactly how `LeakyOracle` cheats. The oracle is
registered and flagged as the canary target, and only a canary target ever
receives the `canary_panel` a caller passes: a forecaster requested with one
never sees it, and the target requested without one is refused. Call sites
pass the dataset's panel once and stop caring which model they are building.

Phase 1 adds the feature models. A `ModelSpec` says whether a model asks the
harness for features (`needs_features`), so an orchestrator can skip such a
candidate when no sources are loaded instead of discovering it at fit time.
The calibrated variants are registered under their own names with explicit
factories, so their knobs are documented like any other model's.
"""

from __future__ import annotations

from typing import Callable, Mapping

from readiness.engine.baseline import (
    ClimatologyPooled,
    ClimatologySeasonal,
    LeakyOracle,
    PersistenceLastYear,
)
from readiness.engine.boosting import GradientBoosting
from readiness.engine.calibrate import Calibrated
from readiness.engine.linear import LogisticRegression
from readiness.harness.labels import Panel


#: What one constructor keyword looks like in `ModelSpec.params`.
ParamSpec = Mapping[str, object]


#: The JSON types a parameter may declare. "list" is a list of strings (the
#: feature-set names); its documented default is a list, and the constructor
#: it maps to accepts any sequence and keeps a tuple.
PARAM_TYPES = ("float", "int", "bool", "str", "list")


def param(type_: str, default: object, help_: str) -> ParamSpec:
    """One entry of a model's parameter schema: its JSON type, default and meaning."""
    if type_ not in PARAM_TYPES:
        raise ValueError(f"unknown parameter type {type_!r}; known: {PARAM_TYPES}")
    return {"type": type_, "default": default, "help": help_}


class ModelSpec:
    def __init__(
        self,
        factory: Callable[..., object],
        description: str,
        *,
        params: Mapping[str, ParamSpec] | None = None,
        is_canary_target: bool = False,
        needs_features: bool = False,
    ) -> None:
        self.factory = factory
        self.description = description
        #: The keywords `build_model(name, **kwargs)` accepts, in the order the
        #: constructor declares them, each with its type, default and meaning.
        #: This is the one statement of a model's knobs: the CLI prints it, the
        #: site exports it, and a caller can validate a proposal against it
        #: without importing the model class.
        self.params: dict[str, ParamSpec] = dict(params or {})
        #: A canary target is not a forecaster: it is built with the full
        #: panel so the harness has something to reject.
        self.is_canary_target = is_canary_target
        #: The model, as built by default, declares feature specs and so needs
        #: the harness to have sources loaded. A caller can still build it
        #: with an empty `feature_sets` for a history-only variant.
        self.needs_features = needs_features


DEFAULT_FEATURE_SETS = ["era5-antecedent", "terrain"]


def logistic_isotonic(
    feature_sets=("era5-antecedent", "terrain"),
    history: bool = True,
    l2: float = 1.0,
    iters: int = 400,
    lr: float = 0.1,
    shrinkage: float = 10.0,
    holdout_years: int = 3,
) -> Calibrated:
    """`logistic` wrapped in an isotonic map fitted on the last training years."""
    inner = LogisticRegression(
        feature_sets=feature_sets, history=history, l2=l2, iters=iters, lr=lr,
        shrinkage=shrinkage,
    )
    return Calibrated(inner, "isotonic", holdout_years)


def gbm_isotonic(
    feature_sets=("era5-antecedent", "terrain"),
    history: bool = True,
    rounds: int = 150,
    depth: int = 2,
    lr: float = 0.1,
    bins: int = 32,
    min_leaf: int = 20,
    shrinkage: float = 10.0,
    holdout_years: int = 3,
) -> Calibrated:
    """`gbm` wrapped in an isotonic map fitted on the last training years."""
    inner = GradientBoosting(
        feature_sets=feature_sets, history=history, rounds=rounds, depth=depth, lr=lr,
        bins=bins, min_leaf=min_leaf, shrinkage=shrinkage,
    )
    return Calibrated(inner, "isotonic", holdout_years)


_FEATURE_PARAMS = {
    "feature_sets": param(
        "list", DEFAULT_FEATURE_SETS,
        "feature sets to ask the harness for (see engine.features.FEATURE_SETS); "
        "empty for history only",
    ),
    "history": param(
        "bool", True, "add the leave-one-year-out seasonal-rate logit from training labels"
    ),
}
_LOGISTIC_PARAMS = {
    **_FEATURE_PARAMS,
    "l2": param("float", 1.0, "L2 penalty in pseudo-observations (intercept unpenalised)"),
    "iters": param("int", 400, "gradient-descent steps, fixed"),
    "lr": param("float", 0.1, "gradient-descent step size"),
    "shrinkage": param("float", 10.0, "shrinkage κ of the history feature's rates"),
}
_GBM_PARAMS = {
    **_FEATURE_PARAMS,
    "rounds": param("int", 150, "boosting rounds (trees)"),
    "depth": param("int", 2, "maximum tree depth"),
    "lr": param("float", 0.1, "learning rate applied to every leaf value"),
    "bins": param("int", 32, "quantile bins per column"),
    "min_leaf": param("int", 20, "minimum training rows on each side of a split"),
    "shrinkage": param("float", 10.0, "shrinkage κ of the history feature's rates"),
}
_CALIBRATION_PARAMS = {
    "holdout_years": param(
        "int", 3, "last training years held out to fit the isotonic map"
    ),
}


REGISTRY: dict[str, ModelSpec] = {
    "climatology-pooled": ModelSpec(
        ClimatologyPooled,
        "training-period base rate, issued everywhere; the contract's reference",
    ),
    "climatology-seasonal": ModelSpec(
        ClimatologySeasonal,
        "per-region, per-period-of-year frequency, shrunk toward scope and pooled rates",
        params={
            "shrinkage": param(
                "float", 10.0,
                "shrinkage κ (pseudo-observations pulling each rate toward its parent)",
            ),
        },
    ),
    "persistence-last-year": ModelSpec(
        PersistenceLastYear,
        "same period last year repeated; sharp and badly calibrated on purpose",
        params={
            "hit": param(
                "float", 0.35,
                "forecast after a hit (same period last year had a damaging event)",
            ),
            "miss": param("float", 0.03, "forecast after a miss (it did not)"),
        },
    ),
    "leaky-oracle": ModelSpec(
        LeakyOracle,
        "reads the outcomes; exists only to be rejected by the canary",
        params={
            "confidence": param(
                "float", 0.999,
                "how close to 0 and 1 the cheat pins its forecasts",
            ),
        },
        is_canary_target=True,
    ),
    "logistic": ModelSpec(
        LogisticRegression,
        "L2 logistic regression on harness-built features, full-batch GD, no RNG",
        params=_LOGISTIC_PARAMS,
        needs_features=True,
    ),
    "logistic+iso": ModelSpec(
        logistic_isotonic,
        "logistic, then an isotonic map fitted on the last training years",
        params={**_LOGISTIC_PARAMS, **_CALIBRATION_PARAMS},
        needs_features=True,
    ),
    "gbm": ModelSpec(
        GradientBoosting,
        "histogram gradient boosting, depth-limited trees, deterministic",
        params=_GBM_PARAMS,
        needs_features=True,
    ),
    "gbm+iso": ModelSpec(
        gbm_isotonic,
        "gbm, then an isotonic map fitted on the last training years",
        params={**_GBM_PARAMS, **_CALIBRATION_PARAMS},
        needs_features=True,
    ),
}


def build_model(name: str, *, canary_panel: Panel | None = None, **kwargs):
    """Construct a registered model by name.

    `canary_panel` is handed to the model only when the registry marks it as a
    canary target. Any other model silently never receives it, so a call site
    can pass the dataset's panel unconditionally without opening a channel by
    which a forecaster could be handed the outcomes it is scored on.
    """
    try:
        spec = REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown model {name!r}; known: {sorted(REGISTRY)}"
        ) from None
    if not spec.is_canary_target:
        return spec.factory(**kwargs)
    if canary_panel is None:
        raise ValueError(
            f"{name} requires an explicit canary_panel — it is a canary target, "
            "not a forecaster, and the harness will reject its output"
        )
    return spec.factory(canary_panel, **kwargs)


def describe_registry() -> str:
    """One line per model, with its parameters (if any) indented beneath it."""
    lines = []
    for name, spec in REGISTRY.items():
        tag = "  [canary target]" if spec.is_canary_target else ""
        tag += "  [needs features]" if spec.needs_features else ""
        lines.append(f"  {name:<28} {spec.description}{tag}")
        for key, p in spec.params.items():
            lines.append(
                f"      {key:<24} {p['type']}, default {p['default']!r}: {p['help']}"
            )
    return "\n".join(lines)
