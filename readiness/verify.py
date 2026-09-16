"""Phase 0 verification as a library.

Report §6, Phase 0:

    Exit when the agent reproduces the climatology baseline's scores
    bit-for-bit from a clean clone, and the harness rejects a deliberately
    leaked model (a canary test).

`readiness verify` prints the outcome; the browser sandbox recomputes the
fingerprints in Pyodide; CI runs the same check against the pinned data. Three
callers, one definition: the fingerprint fields, the models that must
reproduce, and the checks themselves live here so no caller can drift from the
others through a private import.

`REPRO_FIELDS` and `repro_fingerprint` are load-bearing: the blessed files in
`harness_expected/` were written by them, and a change to either would make
every committed fingerprint unverifiable. They do not move.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass

from readiness import data as data_mod
from readiness.contracts import Contract
from readiness.engine import build_model
from readiness.harness import scoring
from readiness.harness.contract import Check
from readiness.harness.ledger import Ledger

#: Scorecard fields that must reproduce exactly. Reliability bins are included
#: via a hash so a bin-level difference cannot hide behind matching aggregates.
REPRO_FIELDS: tuple[str, ...] = (
    "n_units",
    "n_positive",
    "base_rate",
    "brier_score",
    "brier_score_reference",
    "brier_skill_score",
    "auc",
    "sharpness",
    "reliability",
    "resolution",
    "uncertainty",
    "panel_digest",
    "train_digest",
    "contract_digest",
)

#: The baselines whose scores must reproduce bit-for-bit from a clean clone.
REPRO_MODELS: tuple[str, ...] = ("climatology-pooled", "climatology-seasonal")

#: The model the harness must reject for the second criterion to hold.
CANARY_MODEL = "leaky-oracle"


def repro_fingerprint(card: scoring.Scorecard) -> dict:
    """The reproducibility fingerprint of one scorecard.

    Byte-identical to what wrote `harness_expected/*.json`: the repro fields
    as they are, plus a short hash of the canonical reliability bins.
    """
    bins = json.dumps(card.reliability_bins, sort_keys=True, separators=(",", ":"))
    out = {f: getattr(card, f) for f in REPRO_FIELDS}
    out["reliability_bins_sha256"] = hashlib.sha256(bins.encode()).hexdigest()[:16]
    return out


def fingerprints(dataset: data_mod.Dataset) -> dict:
    """Every fingerprint a blessed file records, recomputed from this dataset.

    The layout is the blessed file's: one entry per repro model on the
    validate split, plus the data version and the contract digest so a
    comparison also notices when the inputs or the criteria moved.
    """
    c = dataset.contract
    split = c.splits.validate
    observed: dict = {}
    for name in REPRO_MODELS:
        card = scoring.score(build_model(name), dataset.panel, c, split)
        observed[name] = repro_fingerprint(card)
    observed["_data_version"] = dataset.data_version
    observed["_contract"] = c.digest()
    return observed


def diff_keys(expected: dict, observed: dict) -> list[str]:
    """The top-level fingerprint groups that differ, in a stable order."""
    return [
        key
        for key in sorted(set(expected) | set(observed))
        if expected.get(key) != observed.get(key)
    ]


@dataclass(frozen=True)
class Phase0Result:
    """The Phase 0 criteria for one contract, each as a check with its evidence.

    `checks` is ordered as the CLI prints it: the contract, reproducibility,
    the canary, the ledger. `observed` is the fingerprint set the
    reproducibility check compared (or blessed), so a caller that wants to
    show the numbers need not score again.
    """

    contract: str
    checks: tuple[Check, ...]
    observed: dict
    expected_path: pathlib.Path
    blessed: bool = False

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def failures(self) -> list[str]:
        """One line per failed check: the first line of its detail."""
        return [c.detail.splitlines()[0] for c in self.checks if not c.passed]


def phase0(
    contract: Contract,
    dataset: data_mod.Dataset,
    *,
    ledger_path: pathlib.Path,
    expected_path: pathlib.Path,
    bless: bool = False,
) -> Phase0Result:
    """Evaluate the Phase 0 exit criteria for one contract against one dataset.

    With `bless`, the observed fingerprints are written to `expected_path`
    instead of compared, which is how a contract gets its first blessed file.
    """
    observed = fingerprints(dataset)
    checks = (
        _contract_check(contract),
        _write_blessed(observed, expected_path)
        if bless
        else _reproducibility_check(observed, contract, expected_path),
        _canary_check(dataset),
        _ledger_check(ledger_path),
    )
    return Phase0Result(contract.name, checks, observed, expected_path, bless)


def _contract_check(contract: Contract) -> Check:
    # A Contract validates itself on construction, so reaching this point is
    # the evidence; the row records what was checked and under which digest.
    return Check(
        "contract",
        True,
        f"contract {contract.name} validates (sha256:{contract.digest()}); "
        "splits are disjoint",
    )


def _write_blessed(observed: dict, expected_path: pathlib.Path) -> Check:
    expected_path.parent.mkdir(parents=True, exist_ok=True)
    expected_path.write_text(json.dumps(observed, indent=2, sort_keys=True) + "\n")
    return Check(
        "reproducibility",
        True,
        f"blessed baseline fingerprints -> {data_mod.relative(expected_path)}",
    )


def _reproducibility_check(
    observed: dict, contract: Contract, expected_path: pathlib.Path
) -> Check:
    if not expected_path.exists():
        return Check(
            "reproducibility",
            False,
            "reproducibility: nothing to compare against\n"
            f"no blessed baseline at {data_mod.relative(expected_path)}; "
            f"run `readiness verify -c {contract.name} --bless` once, then commit it",
        )
    expected = json.loads(expected_path.read_text())
    diffs = diff_keys(expected, observed)
    if not diffs:
        return Check(
            "reproducibility", True, "climatology baselines reproduce bit-for-bit"
        )
    lines = [f"reproducibility: {len(diffs)} field group(s) differ"]
    for key in diffs:
        lines.append(key)
        lines.append(f"  expected {json.dumps(expected.get(key), sort_keys=True)}")
        lines.append(f"  observed {json.dumps(observed.get(key), sort_keys=True)}")
    return Check("reproducibility", False, "\n".join(lines))


def _canary_check(dataset: data_mod.Dataset) -> Check:
    c = dataset.contract
    oracle = build_model(CANARY_MODEL, canary_panel=dataset.panel)
    _card, report = scoring.screen(oracle, dataset.panel, c, c.splits.validate)
    if not report.rejected:
        return Check("leakage canary", False, "leakage canary accepted a leaked model")
    tripped = ", ".join(f.check for f in report.findings if f.tripped)
    return Check(
        "leakage canary",
        True,
        f"leakage canary rejected {CANARY_MODEL} (tripped: {tripped})",
    )


def _ledger_check(ledger_path: pathlib.Path) -> Check:
    status = Ledger(ledger_path).verify()
    return Check("ledger", status.valid, status.format())
