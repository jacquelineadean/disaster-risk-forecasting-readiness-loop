"""Named model constructors, so the agent can request a model by string.

Keeping construction behind a registry means an agent (or a CLI user) never
imports engine classes directly and never gets to pass a panel into a
constructor — which is exactly how `LeakyOracle` cheats. The oracle is
registered but flagged, and building it requires explicitly passing the panel.
"""

from __future__ import annotations

from typing import Callable

from readiness.engine.baseline import (
    ClimatologyPooled,
    ClimatologySeasonal,
    LeakyOracle,
    PersistenceLastYear,
)
from readiness.harness.labels import Panel


class ModelSpec:
    def __init__(
        self,
        factory: Callable[..., object],
        description: str,
        *,
        needs_panel: bool = False,
        is_canary_target: bool = False,
    ) -> None:
        self.factory = factory
        self.description = description
        self.needs_panel = needs_panel
        self.is_canary_target = is_canary_target


REGISTRY: dict[str, ModelSpec] = {
    "climatology-pooled": ModelSpec(
        ClimatologyPooled,
        "training-period base rate, issued everywhere; the contract's reference",
    ),
    "climatology-seasonal": ModelSpec(
        ClimatologySeasonal,
        "per-region, per-period-of-year frequency, shrunk toward scope and pooled rates",
    ),
    "persistence-last-year": ModelSpec(
        PersistenceLastYear,
        "same period last year repeated; sharp and badly calibrated on purpose",
    ),
    "leaky-oracle": ModelSpec(
        LeakyOracle,
        "reads the outcomes; exists only to be rejected by the canary",
        needs_panel=True,
        is_canary_target=True,
    ),
}


def needs_panel(name: str) -> bool:
    spec = REGISTRY.get(name)
    return bool(spec and spec.needs_panel)


def build_model(name: str, *, panel: Panel | None = None, **kwargs):
    try:
        spec = REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown model {name!r}; known: {sorted(REGISTRY)}"
        ) from None
    if spec.needs_panel:
        if panel is None:
            raise ValueError(
                f"{name} requires an explicit panel — it is a canary target, "
                "not a forecaster, and the harness will reject its output"
            )
        return spec.factory(panel, **kwargs)
    return spec.factory(**kwargs)


def describe_registry() -> str:
    lines = []
    for name, spec in REGISTRY.items():
        tag = "  [canary target]" if spec.is_canary_target else ""
        lines.append(f"  {name:<28} {spec.description}{tag}")
    return "\n".join(lines)
