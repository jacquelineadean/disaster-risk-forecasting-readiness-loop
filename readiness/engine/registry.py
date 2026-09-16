"""Named model constructors, so the agent can request a model by string.

Keeping construction behind a registry means an agent (or a CLI user) never
imports engine classes directly and never gets to pass a panel into a
constructor — which is exactly how `LeakyOracle` cheats. The oracle is
registered and flagged as the canary target, and only a canary target ever
receives the `canary_panel` a caller passes: a forecaster requested with one
never sees it, and the target requested without one is refused. Call sites
pass the dataset's panel once and stop caring which model they are building.
"""

from __future__ import annotations

from typing import Callable, Mapping

from readiness.engine.baseline import (
    ClimatologyPooled,
    ClimatologySeasonal,
    LeakyOracle,
    PersistenceLastYear,
)
from readiness.harness.labels import Panel


#: What one constructor keyword looks like in `ModelSpec.params`.
ParamSpec = Mapping[str, object]


def param(type_: str, default: object, help_: str) -> ParamSpec:
    """One entry of a model's parameter schema: its JSON type, default and meaning."""
    return {"type": type_, "default": default, "help": help_}


class ModelSpec:
    def __init__(
        self,
        factory: Callable[..., object],
        description: str,
        *,
        params: Mapping[str, ParamSpec] | None = None,
        is_canary_target: bool = False,
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
        lines.append(f"  {name:<28} {spec.description}{tag}")
        for key, p in spec.params.items():
            lines.append(
                f"      {key:<24} {p['type']}, default {p['default']!r}: {p['help']}"
            )
    return "\n".join(lines)
