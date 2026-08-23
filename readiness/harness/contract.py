"""Evaluate a scorecard against the pre-registered contract.

The verdict is mechanical. There is no judgement call, no "close enough", and
no LLM in the path. If the numbers clear the thresholds in `config`, the model
passes; otherwise it does not, and the failing checks say why.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from readiness.config import CONTRACT
from readiness.harness.scoring import Scorecard


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str

    def format(self) -> str:
        return f"  [{'PASS' if self.passed else 'FAIL'}] {self.name:<24} {self.detail}"


@dataclass(frozen=True)
class Verdict:
    passed: bool
    checks: tuple[Check, ...]
    contract_version: str
    contract_digest: str

    def to_dict(self) -> dict:
        return asdict(self)

    def format(self) -> str:
        head = (
            f"contract {self.contract_version} (sha256:{self.contract_digest}) -> "
            f"{'PASS' if self.passed else 'FAIL'}"
        )
        return "\n".join([head, *(c.format() for c in self.checks)])


def evaluate(card: Scorecard) -> Verdict:
    """Apply every contract clause to one scorecard."""
    checks: list[Check] = []

    # 1. Skill relative to climatology. Raw Brier is not comparable across
    #    hazards, so the contract is written in skill terms only.
    bss_ok = card.brier_skill_score > CONTRACT.min_brier_skill_score
    checks.append(
        Check(
            "brier skill score",
            bss_ok,
            f"{card.brier_skill_score:+.4f} vs required "
            f"> {CONTRACT.min_brier_skill_score:+.4f} "
            f"(reference: {CONTRACT.reference_model})",
        )
    )

    # 2. Calibration, in every populated bin. Thin bins are reported but not
    #    judged — a bin with 4 observations cannot fail a 5-point tolerance
    #    meaningfully.
    populated = [
        b
        for b in card.reliability_bins
        if b["count"] >= CONTRACT.reliability_min_bin_count
    ]
    worst: tuple[float, dict] | None = None
    for b in populated:
        dev = abs(b["observed_frequency"] - b["mean_forecast"])
        if worst is None or dev > worst[0]:
            worst = (dev, b)

    if not populated:
        checks.append(
            Check(
                "reliability",
                False,
                f"no bin reached n >= {CONTRACT.reliability_min_bin_count}; "
                "calibration is unmeasurable on this sample",
            )
        )
    else:
        assert worst is not None
        dev, b = worst
        rel_ok = dev <= CONTRACT.reliability_tolerance_pp
        checks.append(
            Check(
                "reliability",
                rel_ok,
                f"worst populated bin [{b['lower']:.1f},{b['upper']:.1f}) "
                f"n={b['count']:,} deviates {dev:.4f}, "
                f"tolerance {CONTRACT.reliability_tolerance_pp:.4f} "
                f"({len(populated)}/{len(card.reliability_bins)} bins populated)",
            )
        )

    # 3. Discrimination.
    auc_ok = card.auc >= CONTRACT.min_auc
    checks.append(
        Check(
            "auc",
            auc_ok,
            f"{card.auc:.4f} vs required >= {CONTRACT.min_auc:.2f}",
        )
    )

    # 4. Provenance: the card must have been produced under this contract.
    same_contract = card.contract_digest == CONTRACT.digest()
    checks.append(
        Check(
            "contract provenance",
            same_contract,
            f"card carries sha256:{card.contract_digest}, "
            f"current contract sha256:{CONTRACT.digest()}"
            + ("" if same_contract else "  <- contract changed since this run"),
        )
    )

    return Verdict(
        passed=all(c.passed for c in checks),
        checks=tuple(checks),
        contract_version=CONTRACT.version,
        contract_digest=CONTRACT.digest(),
    )
