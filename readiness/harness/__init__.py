"""The eval plane.

Report §5:

    Deterministic scoring harness with zero LLM involvement: locked holdouts,
    Brier / BSS / reliability / AUC, an append-only experiment ledger. The
    agent has read access and no write access.

Two invariants hold across this package, and every module here is written to
preserve them:

1. **No model ever receives a holdout label.** `splits.TrainingView` is the only
   channel through which a model sees data, and it exposes training years only.
   Scoring happens in `scoring.py`, after `predict()` has already returned.

2. **Nothing here calls a language model.** Verification must be rules-based to
   be worth anything (report §4, source [4]: rules-based feedback is the
   strongest kind). If an LLM ever needs to read these numbers, it does so
   downstream, from the ledger.

The agent is expected to *read* this package and to *never write to it*. Treat a
diff here from an automated run as a security bug, not a contribution.
"""

from readiness.harness import canary, contract, labels, ledger, metrics, scoring, splits

__all__ = [
    "canary",
    "contract",
    "labels",
    "ledger",
    "metrics",
    "scoring",
    "splits",
]
