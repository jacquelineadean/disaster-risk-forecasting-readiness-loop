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

Every check errs toward rejection: on this task — whether a region sees at
least one damaging event of some hazard in a given period — a forecast that
separates outcomes almost perfectly is far more likely to be leaking than to be
brilliant, whatever the hazard.
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
    declared_feature_digest: str | None = None,
    frame_digest: str | None = None,
) -> CanaryReport:
    """Screen one model's output for signs it saw the answers.

    `view` is what the harness handed the model; `declared_train_digest` is
    what the model says it fitted on. With a view, the two must match, and a
    model that declares nothing is treated as a mismatch.
    """
    findings: list[CanaryFinding] = []

    # 1. Implausible skill. Region-period hazard occurrence is genuinely hard
    #    for every hazard in the catalogue; near-perfect skill is evidence of
    #    leakage, not talent.
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
    #
    #    Judged per outcome class, deliberately. On a rare hazard an honest
    #    climatology forecasts well under 1% for most units, and those
    #    near-zero forecasts "agree" with the mostly-zero outcomes almost every
    #    time — a pooled agreement rate would flag it. What leakage actually
    #    looks like is near-one forecasts capturing the occurrences *and*
    #    near-zero forecasts capturing the non-occurrences. Honest rare-event
    #    forecasts have no near-one group at all.
    n_pos = sum(outcomes)
    n_neg = len(outcomes) - n_pos
    hits = sum(1 for p, y in zip(probs, outcomes) if p >= 0.99 and y == 1)
    misses = sum(1 for p, y in zip(probs, outcomes) if p <= 0.01 and y == 0)
    any_confident = any(p <= 0.01 or p >= 0.99 for p in probs)
    if any_confident:
        hit_share = hits / n_pos if n_pos else 0.0
        miss_share = misses / n_neg if n_neg else 0.0
        agree_trip = (
            hit_share > CANARY_MAX_AGREEMENT and miss_share > CANARY_MAX_AGREEMENT
        )
        detail = (
            f"{hit_share:.1%} of occurrences forecast >= 0.99 and {miss_share:.1%} "
            f"of non-occurrences forecast <= 0.01 (ceiling {CANARY_MAX_AGREEMENT:.1%} "
            "on both)"
        )
    else:
        agree_trip = False
        detail = "no near-binary forecasts issued"
    findings.append(CanaryFinding("outcome agreement", agree_trip, detail))

    # 4. Training provenance. The model must declare what it fitted on, and
    #    it must match what the harness exposed. A model that declares nothing
    #    trips the check rather than skipping it: silence is the cheapest way
    #    to hide a mismatch, and every model built on `engine.base.FittedModel`
    #    declares its digest for free, so an undeclared one is either not
    #    fitted through the training view or written to avoid saying so. The
    #    check is skipped only when the harness itself supplied no view, i.e.
    #    when there is nothing to compare against.
    findings.append(_provenance(view, declared_train_digest))

    # 5. Feature provenance. When the harness built a feature frame for the
    #    model, the model must declare the digest of the frame it was fitted
    #    on, and it must be that frame. Skipped only when there were no
    #    features in the run at all, so Phase 0 verdicts are unchanged.
    findings.append(_feature_provenance(frame_digest, declared_feature_digest))

    return CanaryReport(
        rejected=any(f.tripped for f in findings), findings=tuple(findings)
    )


def _provenance(view: TrainingView | None, declared: str | None) -> CanaryFinding:
    if view is None:
        return CanaryFinding(
            "train provenance", False, "harness supplied no training view; check skipped"
        )
    if declared is None:
        return CanaryFinding(
            "train provenance",
            True,
            f"model declared no training digest; harness exposed sha256:{view.digest}"
            "  <- undeclared",
        )
    mismatch = declared != view.digest
    return CanaryFinding(
        "train provenance",
        mismatch,
        f"model claims sha256:{declared}, harness exposed sha256:{view.digest}"
        + ("  <- mismatch" if mismatch else "  (match)"),
    )


def _feature_provenance(frame_digest: str | None, declared: str | None) -> CanaryFinding:
    if frame_digest is None:
        return CanaryFinding(
            "feature provenance", False, "no feature frame in this run; check skipped"
        )
    if declared is None:
        return CanaryFinding(
            "feature provenance",
            True,
            f"model declared no feature digest; harness exposed sha256:{frame_digest}"
            "  <- undeclared",
        )
    mismatch = declared != frame_digest
    return CanaryFinding(
        "feature provenance",
        mismatch,
        f"model claims sha256:{declared}, harness exposed sha256:{frame_digest}"
        + ("  <- mismatch" if mismatch else "  (match)"),
    )
