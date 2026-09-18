"""The models the agent is allowed to propose.

Report §5: "The agent proposes; the harness disposes." Everything in this
package is proposable. Nothing in it may import from `readiness.harness.contract`,
`readiness.harness.canary` or `readiness.contracts` — a model that can read its
own acceptance criteria is a model that can be written to satisfy them.

Models are hazard- and geography-agnostic by construction: they receive units
of the form (region, year, period) and nothing else, so the same model can be
proposed against any registered contract.

Phase 0 shipped climatologies only, on purpose. Phase 1 adds the feature
models — `logistic`, `gbm` and their isotonic-calibrated variants — which ask
the harness for named feature sets (`FEATURE_SETS`, `specs_for`) and derive
their history feature from training labels alone. The CLIMADA subprocess model
(report §3E: "the engine the agent should drive, extend, and calibrate rather
than reinvent") is deferred; Phase 1 reads a pinned CLIMADA layer as a static
feature set instead.
"""

from readiness.engine.base import FittedModel
from readiness.engine.features import FEATURE_SETS, specs_for
from readiness.engine.registry import REGISTRY, build_model, describe_registry

__all__ = [
    "FEATURE_SETS",
    "REGISTRY",
    "FittedModel",
    "build_model",
    "describe_registry",
    "specs_for",
]
