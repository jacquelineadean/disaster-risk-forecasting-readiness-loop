"""The leakage canary.

Report §6, Phase 0 exit criteria:

    the harness rejects a deliberately leaked model (a canary test).

Be clear about what this is and is not. The *primary* defence against a model
scoring itself on data it should not have is structural: models receive a
`TrainingView` over training years only, `predict()` is handed bare units, and
labels are fetched after `predict()` returns. That structure is what you audit.

The canary is the tripwire for when the structure is breached — by an
accidental join that carries the outcome column, by a feature computed over the
full panel, or by an agent that reads a file it should not have. It is a set of
heuristics over the *output*, so it can be fooled by a sufficiently subtle leak
that produces merely-excellent rather than impossible scores. It is a smoke
alarm, not a proof.

Every check errs toward rejection: on this task, a forecast of quarterly county
flood risk that separates outcomes almost perfectly is far more likely to be
leaking than to be brilliant.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence

from readiness.config import (
    CANARY_MAX_AGREEMENT,
    CANARY_MAX_PLAUSIBLE_AUC,
    CANARY_MAX_PLAUSIBLE_BSS,
)
from readiness.harness.splits import TrainingView


@dataclass(frozen=True)
class CanaryFinding:
    check: str
    tripped: bool
    detail: str

    def format(self) -> str:
        mark = "TRIPPED" if self.tripped else "clear"
        return f"  [{mark:>7}] {self.check:<22} {self.detail}"


@dataclass(frozen=True)
class CanaryReport:
    rejected: bool
    findings: tuple[CanaryFinding, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def format(self) -> str:
        head = (
            "leakage canary -> REJECTED (suspected holdout leakage)"
            if self.rejected
            else "leakage canary -> clear"
        )
        return "\n".join([head, *(f.format() for f in self.findings)])


def run(
    *,
    probs: Sequence[float],
    outcomes: Sequence[int],
    brier_skill_score: float,
    auc: float,
    view: TrainingView | None = None,
    declared_train_digest: str | None = None,
) -> CanaryReport:
    """Screen one model's output for signs it saw the answers.

    `view` and `declared_train_digest` enable the provenance check: a model that
    reports which data it fitted on can be verified against what the harness
    actually handed it.
    """
    findings: list[CanaryFinding] = []

    # 1. Implausible skill. Quarterly county-level hazard occurrence is
    #    genuinely hard; near-perfect skill is evidence of leakage, not talent.
    bss_trip = brier_skill_score > CANARY_MAX_PLAUSIBLE_BSS
    findings.append(
        CanaryFinding(
            "implausible skill",
            bss_trip,
            f"BSS {brier_skill_score:+.4f} vs ceiling "
            f"{CANARY_MAX_PLAUSIBLE_BSS:+.2f}",
        )
    )

    # 2. Implausible discrimination.
    auc_trip = auc > CANARY_MAX_PLAUSIBLE_AUC
    findings.append(
        CanaryFinding(
            "implausible auc",
            auc_trip,
            f"AUC {auc:.5f} vs ceiling {CANARY_MAX_PLAUSIBLE_AUC:.3f}",
        )
    )

    # 3. Near-binary forecasts that agree with the truth. A model that has read
    #    the label column tends to emit values pinned near 0 and 1 that match.
    confident = [
        (p, y) for p, y in zip(probs, outcomes) if p <= 0.01 or p >= 0.99
    ]
    if confident:
        agree = sum(1 for p, y in confident if round(p) == y) / len(confident)
        share = len(confident) / len(probs)
        agree_trip = agree > CANARY_MAX_AGREEMENT and share > 0.5
        detail = (
            f"{share:.1%} of forecasts are near-binary and {agree:.3%} of those "
            f"match the outcome (ceiling {CANARY_MAX_AGREEMENT:.1%})"
        )
    else:
        agree_trip = False
        detail = "no near-binary forecasts issued"
    findings.append(CanaryFinding("outcome agreement", agree_trip, detail))

    # 4. Training provenance. If the model declares what it fitted on, it must
    #    match what the harness exposed.
    if declared_train_digest is None or view is None:
        findings.append(
            CanaryFinding(
                "train provenance",
                False,
                "model declared no training digest; check skipped",
            )
        )
    else:
        mismatch = declared_train_digest != view.digest
        findings.append(
            CanaryFinding(
                "train provenance",
                mismatch,
                f"model claims sha256:{declared_train_digest}, "
                f"harness exposed sha256:{view.digest}"
                + ("  <- mismatch" if mismatch else "  (match)"),
            )
        )

    return CanaryReport(
        rejected=any(f.tripped for f in findings), findings=tuple(findings)
    )
