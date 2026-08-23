"""The models the agent is allowed to propose.

Report §5: "The agent proposes; the harness disposes." Everything in this
package is proposable. Nothing in it may import from `readiness.harness.contract`
or `readiness.harness.canary` — a model that can read its own acceptance
criteria is a model that can be written to satisfy them.

Phase 1 adds the CLIMADA glue here (report §3E: "the engine the agent should
drive, extend, and calibrate rather than reinvent"). Phase 0 ships climatologies
only, on purpose.
"""

from readiness.engine.registry import REGISTRY, build_model, describe_registry

__all__ = ["REGISTRY", "build_model", "describe_registry"]
