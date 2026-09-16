"""Locked splits, the training channel, and the test-touch budget.

Report §4:

    the agent may propose any model it likes, but it cannot touch the harness,
    the holdout years, or the threshold.

The split years themselves come from the contract (`Contract.splits`). This
module provides the machinery that enforces them:

`TrainingView` is the only way a model receives data. It carries training-year
units *and* their labels (fitting needs them) and refuses to hand over anything
from validate or test. `PredictionRequest` carries units with no labels at all.

The touch budget is persisted to disk, not held in memory, precisely so that
re-running the process does not reset it.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Sequence

from readiness.contracts import Contract, ContractError, Split, Splits
from readiness.harness.features import FeatureFrame
from readiness.harness.labels import Panel, Unit

__all__ = [
    "Split",
    "Splits",
    "SplitViolation",
    "TrainingView",
    "PredictionRequest",
    "TouchBudget",
    "get_split",
    "split_panel",
    "coverage_report",
]


class SplitViolation(RuntimeError):
    """Raised when something reaches for data it is not entitled to see."""


def get_split(contract: Contract, name: str) -> Split:
    try:
        return contract.splits.get(name)
    except ContractError as exc:
        raise SplitViolation(str(exc)) from None


class TrainingView:
    """The only channel through which a model sees data.

    Exposes training-year units and labels. Any attempt to read a year outside
    the training split raises. Records a digest of exactly what was exposed, so
    the canary can later verify the model was fitted on what it claims.
    """

    def __init__(
        self, panel: Panel, split: Split, features: FeatureFrame | None = None
    ) -> None:
        leaked = sorted(set(panel.years) - set(split.years))
        if leaked:
            raise SplitViolation(
                f"TrainingView built over {split.name!r} but panel contains "
                f"out-of-split years {leaked}; refusing to expose holdout data"
            )
        if features is not None:
            strays = sorted(set(features.rows) - set(panel.units))
            if strays:
                raise SplitViolation(
                    f"TrainingView over {split.name!r} was handed features for "
                    f"{len(strays)} unit(s) outside its panel (first: {strays[0]}); "
                    "refusing to expose holdout-period features"
                )
        self._panel = panel
        self._split = split
        self._features = features
        self._digest = panel.digest()
        self.accessed = False

    @property
    def split(self) -> Split:
        return self._split

    @property
    def digest(self) -> str:
        """Fingerprint of the data this view exposed. Checked by the canary."""
        return self._digest

    @property
    def features(self) -> FeatureFrame | None:
        """The training units' feature rows, or None when the run has none."""
        self.accessed = True
        return self._features

    @property
    def feature_digest(self) -> str:
        """Fingerprint of the feature rows exposed; empty when there are none."""
        return self._features.digest() if self._features is not None else ""

    def restrict(self, years: Sequence[int]) -> "TrainingView":
        """A view over a subset of the training years, features sliced along.

        For a calibrator that holds out the last training years to fit its
        own correction: the result is still a training-only view by
        construction, because `years` can only narrow what this view holds.
        """
        keep = set(years) & set(self._split.years)
        panel = self._panel.filter_years(sorted(keep))
        frame = self._features.restrict(panel.units) if self._features else None
        return TrainingView(panel, self._split, frame)

    @property
    def base_rate(self) -> float:
        self.accessed = True
        return self._panel.base_rate

    def rows(self) -> list[tuple[Unit, int]]:
        self.accessed = True
        return list(zip(self._panel.units, self._panel.labels))

    def units(self) -> list[Unit]:
        self.accessed = True
        return list(self._panel.units)

    def __len__(self) -> int:
        return len(self._panel)

    def __repr__(self) -> str:
        return (
            f"<TrainingView {self._split.name} n={len(self._panel):,} "
            f"sha256:{self._digest}>"
        )


@dataclass(frozen=True)
class PredictionRequest:
    """Units to forecast, with labels deliberately absent."""

    units: tuple[Unit, ...]
    split_name: str
    #: Feature rows for exactly these units, built by the harness under its
    #: cutoffs. Still no labels: a frame is covariates, never outcomes.
    features: FeatureFrame | None = None

    @classmethod
    def from_panel(
        cls, panel: Panel, split: Split, features: FeatureFrame | None = None
    ) -> "PredictionRequest":
        return cls(units=panel.units, split_name=split.name, features=features)

    def row(self, unit: Unit) -> tuple[float, ...]:
        """The feature row for one unit; raises if the request carries none."""
        if self.features is None:
            raise LookupError("this request carries no features")
        return self.features.row(unit)

    def __len__(self) -> int:
        return len(self.units)

    def __iter__(self):
        return iter(self.units)


class TouchBudget:
    """Persistent count of how often each model version has been scored on TEST.

    Report §4: "final test [...] touched once". A budget that lives in memory is
    not a budget; this one lives in the repo next to the contract's ledger and
    is meant to be committed.
    """

    def __init__(self, path: pathlib.Path, budget: int) -> None:
        self.path = path
        self.budget = budget
        self._counts: dict[str, int] = {}
        if path.exists():
            self._counts = json.loads(path.read_text())

    def _key(self, model: str, version: str) -> str:
        return f"{model}@{version}"

    def count(self, model: str, version: str) -> int:
        return self._counts.get(self._key(model, version), 0)

    def check(self, model: str, version: str) -> None:
        """Raise if this model version has already spent its test budget."""
        used = self.count(model, version)
        if used >= self.budget:
            raise SplitViolation(
                f"{model}@{version} has already been scored against the test "
                f"split {used} time(s); budget is {self.budget}. "
                "Bump the model version and justify it on an experiment card, "
                "or accept the result you already have."
            )

    def spend(self, model: str, version: str) -> int:
        self.check(model, version)
        key = self._key(model, version)
        self._counts[key] = self._counts.get(key, 0) + 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._counts, indent=2, sort_keys=True) + "\n")
        return self._counts[key]

    def as_dict(self) -> dict[str, int]:
        return dict(self._counts)


def split_panel(panel: Panel, split: Split) -> Panel:
    """Slice a full panel down to one split's years."""
    sliced = panel.filter_years(split.years)
    if not len(sliced):
        raise SplitViolation(
            f"split {split.name!r} ({split.years[0]}-{split.years[-1]}) is empty "
            f"in this panel, which covers {panel.years[0]}-{panel.years[-1]}"
        )
    return sliced


def coverage_report(panel: Panel, splits: Splits) -> str:
    """Human-readable check that the panel actually spans all three splits."""
    lines = []
    have = set(panel.years)
    for split in splits:
        missing = sorted(set(split.years) - have)
        sliced = panel.filter_years(split.years)
        status = "ok" if not missing else f"MISSING {missing}"
        lines.append(
            f"  {split.name:<9} {split.years[0]}-{split.years[-1]}  "
            f"n={len(sliced):>7,}  positives={sum(sliced.labels):>5,}  "
            f"base={sliced.base_rate if len(sliced) else 0:.4f}  {status}"
        )
    return "\n".join(lines)
