"""The validated risk layer, as a gap report is allowed to see it.

Phase 2 writes `issued/<contract>/<period>.json`: one probability per county,
per contract, for a period nobody has scored, with the id of the test card
that entitled the model to be issued. This module reads those files and, for
each contract that covers a county, **three fields of the ledger card the
issued file names** — `experiment_id`, `model` and `version`, which is what a
citation to that card needs and all this module keeps. It does not fit,
refit, score, or reach for a model; it does not read a panel; and it has no
path that produces a number from anything but a file `readiness issue`
already wrote. Nothing a card holds about held-out performance — a scorecard,
a label count, a split — ever enters a `plans` object, so no rule can cite
one even by accident.

Two properties matter for Phase 3:

* **Absence is stated, not filled.** With no issued file covering a county for
  a period, `probability` returns None and `absence` returns a `computed`
  claim that says exactly that. There is no fallback to a neighbour, a state
  average or a national rate.
* **A county number is never an intensity.** The probability of at least one
  damaging event in a county over a quarter is not the depth of water at a
  switchgear. `rules.py` may cite it as context; no rule may answer inject 3
  with it, and `tests/test_rules.py` asserts that no rule ever does.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
from typing import Iterable, Mapping

from readiness import cite
from readiness import issue as issue_mod
from readiness.harness.ledger import Ledger

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

#: Where the ledgers live. Spelled here rather than imported from
#: `readiness.data`, which is the panel builder: `readiness/plans` does not
#: import the panel, and `tests/test_boundaries.py` enforces that.
EXPERIMENTS_DIR = REPO_ROOT / "experiments"
EXPERIMENTS_DIR_ENV = "READINESS_EXPERIMENTS_DIR"


def experiments_root() -> pathlib.Path:
    override = os.environ.get(EXPERIMENTS_DIR_ENV)
    return pathlib.Path(override) if override else EXPERIMENTS_DIR

#: How a probability claim reads in prose.
PROBABILITY_FMT = "{:.0%}"


@dataclasses.dataclass(frozen=True)
class RiskLayer:
    """Every issued probability for one period and a set of counties.

    `cards` holds the backing test card records by `<contract>/<card id>` and
    by card id, so a document that cites the card behind a probability
    resolves through the same grammar the county brief uses.
    """

    period_label: str
    #: (county FIPS, contract name) -> probability.
    probabilities: dict[tuple[str, str], float]
    #: Contract name -> the issued file it came from.
    issued: dict[str, issue_mod.Issued]
    #: Ledger card records, for the citation resolver.
    cards: dict[str, dict] = dataclasses.field(default_factory=dict)

    # -- what it knows -----------------------------------------------------

    def contracts(self) -> tuple[str, ...]:
        return tuple(sorted(self.issued))

    def covering(self, fips: str) -> tuple[str, ...]:
        """The contracts with a probability for this county, in name order."""
        return tuple(sorted(name for (f, name) in self.probabilities if f == fips))

    def probability(self, fips: str, contract: str) -> cite.Claim | None:
        """A cited probability, or None. None is never silently a number."""
        value = self.probabilities.get((fips, contract))
        if value is None:
            return None
        issued = self.issued[contract]
        return cite.Claim(
            id=f"risk-{contract}-{fips}",
            text=(
                f"probability of at least one damaging event in county {fips} during "
                f"{self.period_label}, from the issued file for {contract}"
            ),
            value=value,
            source=cite.Source(
                "issued", issue_mod.issued_ref(contract, issued.period_label)
            ),
            fmt=PROBABILITY_FMT,
        )

    def card_claim(self, contract: str) -> cite.Claim | None:
        """The test card the issued file names, so a reader can check the skill."""
        issued = self.issued.get(contract)
        if issued is None:
            return None
        ref = f"{contract}/{issued.validated_by}"
        if ref not in self.cards and issued.validated_by not in self.cards:
            return None
        return cite.Claim(
            id=f"risk-{contract}-card",
            text=(
                f"the test card that validated {issued.model}@{issued.version} for "
                f"{contract}, which the issued file names"
            ),
            value=None,
            source=cite.Source("ledger", ref),
        )

    def absence(self, fips: str, *, derived_from: str) -> cite.Claim:
        """An explicit claim that nothing is issued. Never a substitute number.

        An absence has no artefact to point at, so the claim is `computed` and
        derived from a claim the document already carries — the same shape the
        county brief uses when no exposure extract is pinned.
        """
        return cite.Claim(
            id=f"risk-absent-{fips}",
            text=(
                f"no validated issuance covers county {fips} for period "
                f"{self.period_label}, so no probability is reported for it"
            ),
            value=None,
            source=cite.Source("computed", derived_from),
        )

    def issued_refs(self) -> dict[str, set[str]]:
        """The `issued` refs this layer licenses, for a `cite` resolver."""
        return {
            issue_mod.issued_ref(name, one.period_label).split("#")[0]: {one.period_label}
            for name, one in self.issued.items()
        }

    def summary(self) -> str:
        return (
            f"risk layer {self.period_label}: {len(self.issued)} issued file(s) "
            f"({', '.join(self.contracts()) or 'none'}), "
            f"{len(self.probabilities)} county-contract probability/probabilities"
        )

    # -- loading -----------------------------------------------------------

    @classmethod
    def empty(cls, period_label: str) -> "RiskLayer":
        """Nothing issued: every question about a probability answers "absent"."""
        return cls(period_label=period_label, probabilities={}, issued={}, cards={})

    @classmethod
    def load(
        cls,
        period_label: str,
        counties: Iterable[str],
        *,
        issued_dir: pathlib.Path | None = None,
        experiments_dir: pathlib.Path | None = None,
    ) -> "RiskLayer":
        """Every issued probability for these counties and this period.

        Reads `issued/` through `readiness.issue` and, for each contract that
        covers a county, the ledger card the issued file names. Nothing else is
        opened: no panel, no snapshot, no model.
        """
        wanted = sorted({str(f) for f in counties})
        probabilities: dict[tuple[str, str], float] = {}
        issued: dict[str, issue_mod.Issued] = {}
        for one in issue_mod.read_issued(None, issued_dir):
            if one.period_label != period_label:
                continue
            hits = {f: one.probability(f) for f in wanted if one.covers(f)}
            if not hits:
                continue
            issued[one.contract] = one
            for fips, value in hits.items():
                probabilities[(fips, one.contract)] = float(value)
        return cls(
            period_label=period_label,
            probabilities=probabilities,
            issued=issued,
            cards=_card_records(issued, experiments_dir),
        )


def _card_records(
    issued: Mapping[str, issue_mod.Issued], experiments_dir: pathlib.Path | None
) -> dict[str, dict]:
    """The cards the issued files name, by `<contract>/<id>` and by id.

    **Three fields, deliberately.** `card_claim` cites a card so a reader can
    check which model was validated; it needs the id, the model and the
    version, and nothing else a card holds belongs on this side of the seam.
    A whole card carries `scorecard["test"]` — held-out Brier, AUC and label
    counts — and this module's resolver would then license a sentence to cite
    it. It does not, because they are not here.

    A missing ledger is not an error here: the layer still holds the
    probabilities, and a document that cannot cite the card simply does not.
    """
    root = pathlib.Path(experiments_dir) if experiments_dir else experiments_root()
    out: dict[str, dict] = {}
    for name, one in issued.items():
        path = root / name / "ledger.jsonl"
        if not path.exists():
            continue
        try:
            cards = list(Ledger(path).read())
        except (OSError, ValueError):
            continue
        for card in cards:
            if card.experiment_id != one.validated_by:
                continue
            record = {
                "experiment_id": card.experiment_id,
                "model": card.model,
                "version": card.version,
            }
            out[f"{name}/{card.experiment_id}"] = record
            out[card.experiment_id] = record
    return out


__all__ = [
    "EXPERIMENTS_DIR",
    "EXPERIMENTS_DIR_ENV",
    "PROBABILITY_FMT",
    "RiskLayer",
    "experiments_root",
]
