"""Issuance: the promoted model, refitted, forecasting a period nobody scored.

Plan §3: "`readiness issue` refits the validated model through TrainingView,
requires the training and feature digests to match the test card, builds the
target period's features under the same firewall, and writes a probability per
county. No parameter exists through which a label could arrive."

That last sentence is structural, not a promise. A future period has no
labels — nothing has happened yet — and this module has nowhere to put one:
`issue()` takes a contract, a dataset, a model name, its constructor arguments
and a period, and the only labels that exist anywhere in the call are the
*training* labels the model has always been entitled to, handed over by the
same `TrainingView` the backtest used.

The guards, in the order they run:

1. **A validated model.** The contract's ledger must hold a passing,
   canary-clear *test* card for exactly this model, version and arguments
   under the current contract digest. There is no flag to skip it.
2. **The digests must match.** The refit's `training_digest` and
   `feature_digest` must equal the ones on that card. A revised extract, a
   changed county file or a different feature set moves them, and the issue is
   refused rather than silently reissued from a fit nobody scored.
3. **The frame must be clean.** The target period's rows are built by
   `build_frame` under the same firewall (cutoff = period start - lag) and
   `audit_frame` runs over the training *and* target units together.
4. **The period must be issuable.** If a series source's last pinned month is
   earlier than the cutoff, the period cannot be issued yet and the refusal
   names the month the data would have to reach. Issuance waits for the data;
   it never extrapolates.

Only then does the model predict, through a `PredictionRequest` that carries
units and feature rows and — like every other request in this repository — no
outcomes. What is written is `issued/<contract>/<period>.json`.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import pathlib
from typing import Callable, Mapping, Sequence

from readiness import data as data_mod
from readiness.agent import guard
from readiness.contracts import Contract
from readiness.engine import build_model
from readiness.harness.features import FeatureSpec, build_frame
from readiness.harness.features import audit_frame as audit_feature_frame
from readiness.harness.ledger import ExperimentCard, Ledger
from readiness.harness.splits import PredictionRequest, TrainingView, split_panel

Progress = Callable[[str], None]
Unit = tuple[str, int, int]

REPO_ROOT = data_mod.REPO_ROOT
#: Where `readiness issue` writes. Outputs, not sources: `issued/README.md` is
#: the only file of this tree that git carries.
ISSUED_DIR = REPO_ROOT / "issued"
#: Point issuance at another tree — for CI, or for a test that must not write
#: into the repository's own `issued/`.
ISSUED_DIR_ENV = "READINESS_ISSUED_DIR"

#: The period-label shapes, by the contract's `period`. A quarterly contract
#: issues `2026-Q4`, a monthly one `2026-M11`, an annual one `2026`.
PERIOD_MARKS: dict[str, str] = {"quarter": "Q", "month": "M", "year": ""}


class IssueRefused(RuntimeError):
    """A guard said no. Every refusal names exactly what was missing."""


def issued_root(issued_dir: pathlib.Path | None = None) -> pathlib.Path:
    if issued_dir is not None:
        return pathlib.Path(issued_dir)
    override = os.environ.get(ISSUED_DIR_ENV)
    return pathlib.Path(override) if override else ISSUED_DIR


def issued_path(
    contract_name: str, label: str, issued_dir: pathlib.Path | None = None
) -> pathlib.Path:
    return issued_root(issued_dir) / contract_name / f"{label}.json"


def issued_ref(contract_name: str, label: str) -> str:
    """How a brief cites an issued file: the path, then the period it covers.

    The spelling `tools/build_site.py`'s `brief_resolver` accepts, so a brief
    written here still validates when the website rebuilds it from the
    committed tree.
    """
    return f"issued/{contract_name}/{label}.json#{label}"


# ---------------------------------------------------------------------------
# Period labels
# ---------------------------------------------------------------------------


def parse_period(label: str, contract: Contract) -> tuple[int, int]:
    """`2026-Q4` -> (2026, 4), against the contract's own period length.

    A label whose shape does not match the contract is refused: a quarter
    cannot be issued under a monthly contract, and `2026-Q5` does not exist.
    The year is deliberately unbounded above — issuing a period after the last
    split year is the whole point of this module — and bounded below only by
    four digits, so a typo is not silently read as year 26.
    """
    text = str(label).strip()
    ppy = contract.periods_per_year
    mark = PERIOD_MARKS[contract.period]
    shape = f"YYYY-{mark}n" if mark else "YYYY"
    year_text, sep, period_text = text.partition("-")
    if not (len(year_text) == 4 and year_text.isdigit()):
        raise IssueRefused(
            f"period {label!r} is not a {contract.period}ly label for contract "
            f"{contract.name!r}; expected {shape}"
        )
    year = int(year_text)
    if not mark:
        if sep or period_text:
            raise IssueRefused(
                f"period {label!r} names a sub-year period, but contract "
                f"{contract.name!r} is annual; expected {shape}"
            )
        return year, 1
    if not (sep and period_text[:1] == mark and period_text[1:].isdigit()):
        raise IssueRefused(
            f"period {label!r} is not a {contract.period}ly label for contract "
            f"{contract.name!r}; expected {shape} (for example "
            f"{period_label(year, 1, contract)})"
        )
    period = int(period_text[1:])
    if not 1 <= period <= ppy:
        raise IssueRefused(
            f"period {label!r} is out of range: contract {contract.name!r} has "
            f"{ppy} period(s) per year"
        )
    return year, period


def period_label(year: int, period: int, contract: Contract) -> str:
    """(2026, 4) -> `2026-Q4`, the inverse of `parse_period`."""
    ppy = contract.periods_per_year
    if not 1 <= period <= ppy:
        raise IssueRefused(
            f"period {period} is out of range: contract {contract.name!r} has "
            f"{ppy} period(s) per year"
        )
    mark = PERIOD_MARKS[contract.period]
    if not mark:
        return f"{year:04d}"
    width = 2 if ppy > 9 else 1
    return f"{year:04d}-{mark}{period:0{width}d}"


def month_label(index: int) -> str:
    """A month index as `YYYY-MM`, for a refusal that names the data it needs."""
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


# ---------------------------------------------------------------------------
# What is written
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Issued:
    """One contract's probabilities for one period, with the fit's provenance.

    Everything here is either a criterion (the contract and its digest), the
    identity of the model that was validated (`model`, `version`,
    `model_kwargs`, `validated_by`), a fingerprint of what the fit and the
    features were built from, or the forecasts themselves. There is no field
    for an outcome, because the period has not happened.
    """

    contract: str
    contract_digest: str
    model: str
    version: str
    model_kwargs: dict
    #: The experiment id of the test card this issue leans on.
    validated_by: str
    period: tuple[int, int]
    period_label: str
    #: Region id (county FIPS) -> probability in [0, 1]. The county is the
    #: finest key that exists anywhere in this repository.
    probabilities: dict[str, float]
    train_digest: str
    feature_digest: str
    feature_version: str
    data_version: str
    #: The guarded code as it stood when this file was written, so a reader can
    #: ask whether the issuing harness is the one the card was scored under.
    harness_digest: str
    issued_at: str
    #: Every manifest key the panel and the features were built from.
    inputs: tuple[str, ...] = ()

    @property
    def year(self) -> int:
        return self.period[0]

    def probability(self, region: str) -> float | None:
        return self.probabilities.get(region)

    def covers(self, region: str) -> bool:
        return region in self.probabilities

    def to_dict(self) -> dict:
        return {
            "contract": self.contract,
            "contract_digest": self.contract_digest,
            "model": self.model,
            "version": self.version,
            "model_kwargs": dict(self.model_kwargs),
            "validated_by": self.validated_by,
            "period": [self.period[0], self.period[1]],
            "period_label": self.period_label,
            "probabilities": dict(self.probabilities),
            "train_digest": self.train_digest,
            "feature_digest": self.feature_digest,
            "feature_version": self.feature_version,
            "data_version": self.data_version,
            "harness_digest": self.harness_digest,
            "issued_at": self.issued_at,
            "inputs": list(self.inputs),
        }

    @classmethod
    def from_dict(cls, raw: Mapping) -> "Issued":
        period = tuple(raw["period"])
        return cls(
            contract=raw["contract"],
            contract_digest=raw["contract_digest"],
            model=raw["model"],
            version=raw["version"],
            model_kwargs=dict(raw.get("model_kwargs", {})),
            validated_by=raw["validated_by"],
            period=(int(period[0]), int(period[1])),
            period_label=raw["period_label"],
            probabilities={str(k): float(v) for k, v in raw["probabilities"].items()},
            train_digest=raw.get("train_digest", ""),
            feature_digest=raw.get("feature_digest", ""),
            feature_version=raw.get("feature_version", ""),
            data_version=raw.get("data_version", ""),
            harness_digest=raw.get("harness_digest", ""),
            issued_at=raw["issued_at"],
            inputs=tuple(raw.get("inputs", ())),
        )

    def to_json(self) -> str:
        """Stable bytes: sorted keys, so two issues of one period are one file."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    def write(self, issued_dir: pathlib.Path | None = None) -> pathlib.Path:
        path = issued_path(self.contract, self.period_label, issued_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def read(cls, path: pathlib.Path) -> "Issued":
        return cls.from_dict(json.loads(pathlib.Path(path).read_text(encoding="utf-8")))

    def summary(self) -> str:
        values = sorted(self.probabilities.values())
        middle = values[len(values) // 2] if values else float("nan")
        return (
            f"issued {self.contract} {self.period_label}: {len(self.probabilities):,} "
            f"region(s), median {middle:.4f}, from {self.model}@{self.version} "
            f"({self.validated_by})"
        )


def read_issued(
    contract_name: str | None = None, issued_dir: pathlib.Path | None = None
) -> list[Issued]:
    """Every issued file on disk, or one contract's, oldest path first."""
    root = issued_root(issued_dir)
    pattern = f"{contract_name}/*.json" if contract_name else "*/*.json"
    out = []
    for path in sorted(root.glob(pattern)):
        out.append(Issued.read(path))
    return out


def covering(
    fips: str, label: str, issued_dir: pathlib.Path | None = None
) -> list[Issued]:
    """The issued files for `label` that carry a probability for this county."""
    return [
        issued
        for issued in read_issued(None, issued_dir)
        if issued.period_label == label and issued.covers(fips)
    ]


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------


def _json_kwargs(kwargs: Mapping | None) -> dict:
    """Arguments as a card writes them: JSON, tuples as lists, sorted keys."""
    return json.loads(json.dumps(dict(kwargs or {}), sort_keys=True))


def validating_card(
    ledger: Ledger, contract: Contract, model: str, version: str, kwargs: Mapping
) -> ExperimentCard:
    """The test card that entitles this model to be issued, or a refusal saying why."""
    digest = contract.digest()
    wanted = _json_kwargs(kwargs)
    tests = [c for c in ledger.read() if c.split == "test" and c.contract_digest == digest]
    if not tests:
        raise IssueRefused(
            f"refusing to issue {model}@{version}: the ledger holds no test card "
            f"under contract {contract.name} (sha256:{digest}). Score it on validate "
            f"and then `readiness promote {model} -c {contract.name} "
            "--spend-test-touch`; issuance leans on a card, never on a hope."
        )
    mine = [
        c
        for c in tests
        if c.model == model
        and c.version == version
        and c.data_snapshot.get("model_kwargs") == wanted
    ]
    if not mine:
        held = "; ".join(
            f"{c.experiment_id} {c.model}@{c.version} with arguments "
            f"{json.dumps(c.data_snapshot.get('model_kwargs', {}), sort_keys=True)}"
            for c in tests
        )
        raise IssueRefused(
            f"refusing to issue {model}@{version} with arguments "
            f"{json.dumps(wanted, sort_keys=True)}: no test card under contract "
            f"{contract.name} (sha256:{digest}) names exactly this model, version and "
            f"arguments. The ledger holds: {held}."
        )
    card = mine[0]
    if card.canary and card.canary.get("rejected"):
        tripped = ", ".join(
            f["check"] for f in card.canary.get("findings", ()) if f.get("tripped")
        )
        raise IssueRefused(
            f"refusing to issue {model}@{version}: its test card {card.experiment_id} "
            f"was rejected by the leakage canary (tripped: {tripped}). A rejected "
            "model's probabilities are not believed, so they are not published."
        )
    if not (card.verdict or {}).get("passed"):
        failed = ", ".join(
            c["name"] for c in (card.verdict or {}).get("checks", ()) if not c["passed"]
        )
        raise IssueRefused(
            f"refusing to issue {model}@{version}: its test card "
            f"{card.experiment_id} did not pass contract {contract.name} "
            f"(failed: {failed or 'unknown'}). The published record is the result; "
            "issuance is for a model that cleared the contract."
        )
    return card


def _target_units(dataset: data_mod.Dataset, year: int, period: int) -> list[Unit]:
    """One unit per region in the contract's universe. No labels exist for them."""
    if not dataset.regions:
        raise IssueRefused(
            "refusing to issue: the dataset carries no region universe, so there is "
            "nothing to forecast. Build it with `readiness panel` first."
        )
    return [(region.fips, year, period) for region in dataset.regions]


def _check_digests(model, card: ExperimentCard, contract: Contract) -> None:
    """The refit must be the fit that was scored, or nothing is published."""
    want_train = (card.scorecard or {}).get("train_digest")
    want_feature = (card.scorecard or {}).get("feature_digest") or None
    if model.training_digest != want_train:
        raise IssueRefused(
            f"refusing to issue {model.name}@{model.version}: the refit was trained on "
            f"sha256:{model.training_digest}, but test card {card.experiment_id} was "
            f"scored on sha256:{want_train}. The training data has moved since the "
            "card was written (a revised extract, a changed county file); rescore "
            f"under a new contract version rather than reissuing {contract.name} from "
            "a fit nobody scored."
        )
    if model.feature_digest != want_feature:
        raise IssueRefused(
            f"refusing to issue {model.name}@{model.version}: the refit's feature "
            f"digest is {model.feature_digest!r}, but test card {card.experiment_id} "
            f"records {want_feature!r}. The feature rows behind the fit are not the "
            "ones that were scored."
        )


def _series_reach(
    specs: Sequence[FeatureSpec],
    sources: Mapping,
    units: Sequence[Unit],
    ppy: int,
) -> None:
    """Refuse a period whose features would be built from data that is not in yet.

    The firewall says which months a period may read: everything strictly
    before `period start - lag`. If a series source's last pinned month is
    earlier than that, the honest answer is not a forecast from a shorter
    window — it is "not yet", with the month the data has to reach.
    """
    for spec in specs:
        if spec.is_static:
            continue
        source = sources[spec.source]
        for unit in units:
            cutoff = spec.cutoff(unit, ppy)
            series = source.series(unit[0], spec.variable)
            if series is None or series.end >= cutoff:
                continue
            raise IssueRefused(
                f"period cannot be issued yet: data through {month_label(cutoff - 1)} "
                f"needed for column {spec.column!r} ({spec.source}.{spec.variable}), "
                f"but {spec.source} ends at {month_label(series.end - 1)}. The cutoff "
                "is the period's first month minus the feature's lag; issuance waits "
                "for the data rather than extrapolating."
            )


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def issue(
    contract: Contract,
    dataset: data_mod.Dataset,
    model_name: str,
    kwargs: Mapping | None = None,
    period: tuple[int, int] | str = "",
    *,
    experiments_dir: pathlib.Path | None = None,
    issued_dir: pathlib.Path | None = None,
    progress: Progress = lambda _m: None,
) -> Issued:
    """Refit the validated model and forecast one period, or refuse and say why.

    `period` is a `(year, period)` pair or a label the contract's shape
    accepts. Nothing here reads a holdout label: the panel's training split is
    handed to the model through `TrainingView` exactly as the backtest did,
    and the target units are bare `(region, year, period)` triples for a period
    that has not happened.
    """
    if isinstance(period, str):
        year, index = parse_period(period, contract)
    else:
        year, index = int(period[0]), int(period[1])
    label = period_label(year, index, contract)
    if dataset.contract.digest() != contract.digest():
        raise IssueRefused(
            f"refusing to issue {contract.name}: the dataset was built for "
            f"{dataset.contract.name} (sha256:{dataset.contract.digest()}), not for "
            f"this contract (sha256:{contract.digest()})."
        )

    where = data_mod.paths(contract, experiments_dir=experiments_dir)
    model = build_model(model_name, **dict(kwargs or {}))
    card = validating_card(
        Ledger(where.ledger), contract, model.name, model.version, kwargs or {}
    )
    progress(
        f"  validated by     {card.experiment_id} ({card.model}@{card.version} on "
        f"{card.split}, BSS {card.scorecard.get('brier_skill_score', float('nan')):+.4f})"
    )

    train_split = contract.splits.train
    train_panel = split_panel(dataset.panel, train_split)
    units = _target_units(dataset, year, index)
    specs: tuple[FeatureSpec, ...] = tuple(getattr(model, "feature_specs", ()) or ())
    ppy = contract.periods_per_year

    every = list(train_panel.units) + units
    frame = train_frame = target_frame = None
    if specs:
        progress(
            f"  features         {len(specs)} column(s) over {len(every):,} units "
            f"(training + {label})"
        )
        # One build over both sets of units, so the rows the model is fitted on
        # and the rows it forecasts come from the same pass of the same specs.
        frame = build_frame(specs, dataset.sources, every, ppy)
        train_frame = frame.restrict(train_panel.units)
        target_frame = frame.restrict(units)

    view = TrainingView(train_panel, train_split, train_frame)
    progress(f"  refit            {model.name}@{model.version} on {train_split}")
    model.fit(view)
    _check_digests(model, card, contract)

    if specs and frame is not None:
        audit = audit_feature_frame(specs, dataset.sources, every, contract, frame)
        if not audit.clean:
            raise IssueRefused(
                f"refusing to issue {contract.name} {label}: the feature audit is not "
                f"clean.\n{audit.format()}"
            )
        _series_reach(specs, dataset.sources, units, ppy)

    request = PredictionRequest(tuple(units), "issue", target_frame)
    probs = list(model.predict(request))
    if len(probs) != len(units):
        raise IssueRefused(
            f"{model.name} returned {len(probs)} forecasts for {len(units)} region(s)"
        )
    probabilities = {
        unit[0]: min(1.0, max(0.0, float(p))) for unit, p in zip(units, probs)
    }

    issued = Issued(
        contract=contract.name,
        contract_digest=contract.digest(),
        model=model.name,
        version=model.version,
        model_kwargs=_json_kwargs(kwargs),
        validated_by=card.experiment_id,
        period=(year, index),
        period_label=label,
        probabilities=probabilities,
        train_digest=model.training_digest or "",
        feature_digest=model.feature_digest or "",
        feature_version=dataset.feature_version,
        data_version=dataset.data_version,
        harness_digest=guard.tree_digest(),
        issued_at=utc_now(),
        inputs=tuple(
            sorted(set(data_mod.input_keys(contract)) | set(dataset.feature_inputs))
        ),
    )
    path = issued.write(issued_dir)
    progress(f"  wrote            {data_mod.relative(path)}")
    return issued


__all__ = [
    "ISSUED_DIR",
    "ISSUED_DIR_ENV",
    "Issued",
    "IssueRefused",
    "covering",
    "issue",
    "issued_path",
    "issued_ref",
    "issued_root",
    "month_label",
    "parse_period",
    "period_label",
    "read_issued",
    "validating_card",
]
