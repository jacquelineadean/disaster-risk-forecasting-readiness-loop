"""The models the agent is allowed to propose.

Report §5: "The agent proposes; the harness disposes." Everything in this
package is proposable. Nothing in it may import from `readiness.harness.contract`,
`readiness.harness.canary` or `readiness.contracts` — a model that can read its
own acceptance criteria is a model that can be written to satisfy them.

Models are hazard- and geography-agnostic by construction: they receive units
of the form (region, year, period) and nothing else, so the same model can be
proposed against any registered contract.

Phase 1 adds the CLIMADA glue here (report §3E: "the engine the agent should
drive, extend, and calibrate rather than reinvent"). Phase 0 ships climatologies
only, on purpose.
"""

from readiness.engine.base import FittedModel
from readiness.engine.registry import REGISTRY, build_model, describe_registry

__all__ = ["REGISTRY", "FittedModel", "build_model", "describe_registry"]
