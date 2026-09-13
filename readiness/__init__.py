"""The Readiness Loop — an open-source, agentic disaster-risk forecasting system.

Four planes, one contract (report §5):

    readiness/connectors/  data plane   fetch and pin public sources
    readiness/agent/       agent plane  propose models, write experiment cards
    readiness/harness/     eval plane   score them; no LLM, no agent write access
    readiness/engine/      the models themselves

The agent proposes; the harness disposes.
"""

__version__ = "0.2.0"
