"""Pre-registered contracts, as data.

A contract is everything that defines one experiment series: the hazard, the
geography, the forecast period, what counts as a damaging event, the locked
splits, and the thresholds a model must clear. It is written down *before* any
model is fitted, its criteria are hashed, and the hash is recorded on every
experiment card. Change a criterion and the hash changes, which marks every
prior experiment as visibly incomparable. That is the point.

From the research report, §5:

    "Within an acceptable threshold" then becomes a contract, not a feeling —
    versioned in the repo before iteration begins.

Contracts are JSON files, one per file, in `contracts/`:

    {
      "name": "flood-example",
      "version": "1.0.0",
      "description": "Damaging inland flooding, one state, quarterly.",
      "hazard": "inland_flood",
      "scope": {"country": "US", "states": ["XX"]},
      "period": "quarter",
      "damaging": {"property_usd_min": 10000, "count_casualties": true},
      "zone_policy": "drop",
      "splits": {"train": [1996, 2015], "validate": [2016, 2020], "test": [2021, 2025]},
      "test_touch_budget": 1,
      "reference_model": "climatology-pooled",
      "thresholds": {
        "min_brier_skill_score": 0.0,
        "reliability_tolerance_pp": 0.05,
        "reliability_min_bin_count": 30,
        "min_auc": 0.70,
        "n_reliability_bins": 10
      }
    }

The forecast unit a contract defines is:

    P(at least one damaging event of `hazard` in region R during period T)

where regions come from the scope (US counties, for the Storm Events ground
truth) and periods are months, quarters or years.

`zone_policy` says what to do with events Storm Events codes against NWS
forecast zones rather than counties: `drop` them (the default; honest for
county-coded hazards, badly under-counting for zone-coded ones) or `expand`
each to every county in its zone via the NWS crosswalk. It is a criterion —
it changes the labels — so it is hashed like the rest.

Nothing here imports the engine, the agent, or the harness. The harness reads
contracts; contracts do not read the harness.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
from dataclasses import asdict, dataclass, field
from typing import Iterator, Mapping, Sequence

from readiness.config import HAZARDS, RECORD_START_YEAR

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CONTRACTS_DIR = REPO_ROOT / "contracts"

#: Environment overrides, so tests and CI can point at a different registry
#: without touching the repository's.
CONTRACTS_DIR_ENV = "READINESS_CONTRACTS_DIR"
CONTRACT_ENV = "READINESS_CONTRACT"

PERIODS: dict[str, int] = {"month": 12, "quarter": 4, "year": 1}
ZONE_POLICIES: tuple[str, ...] = ("drop", "expand")

#: The only reference forecast the harness implements: the unsmoothed training
#: base rate, issued everywhere. A contract must name it so the choice is
#: explicit on the card, but the harness will not fit anything else as a
#: reference — see `readiness.harness.scoring.climatology_reference`.
REFERENCE_MODELS: tuple[str, ...] = ("climatology-pooled",)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
STATE_RE = re.compile(r"^[A-Z]{2}$")
YEAR_RANGE_RE = re.compile(r"^(\d{4})-(\d{4})$")


class ContractError(ValueError):
    """A contract that cannot be registered, loaded, or resolved."""


# --------------------------------------------------------------------------
# Splits
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    name: str
    years: tuple[int, ...]
    purpose: str

    def __contains__(self, year: int) -> bool:
        return year in self.years

    def __str__(self) -> str:
        return f"{self.name} ({self.years[0]}-{self.years[-1]})"


@dataclass(frozen=True)
class Splits:
    """The three locked splits of one contract."""

    train: Split
    validate: Split
    test: Split

    def __iter__(self) -> Iterator[Split]:
        return iter((self.train, self.validate, self.test))

    def get(self, name: str) -> Split:
        for split in self:
            if split.name == name:
                return split
        raise ContractError(
            f"unknown split {name!r}; known: ['train', 'validate', 'test']"
        )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self)


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Contract:
    """The frozen acceptance criteria for one experiment series.

    `name`, `version` and `description` are labels: they identify the file and
    tell a reader what it is for, and they are *not* part of the digest. Every
    other field is a criterion, and every criterion is hashed.
    """

    name: str
    hazard: str
    event_types: tuple[str, ...]
    country: str
    states: tuple[str, ...]   # empty = every region in the country's universe
    period: str               # "month" | "quarter" | "year"
    damage_property_usd_min: float
    damage_count_casualties: bool
    zone_policy: str          # "drop" | "expand"
    train_years: tuple[int, ...]
    validate_years: tuple[int, ...]
    test_years: tuple[int, ...]
    test_touch_budget: int
    reference_model: str
    min_brier_skill_score: float
    reliability_tolerance_pp: float
    reliability_min_bin_count: int
    min_auc: float
    n_reliability_bins: int
    version: str = "1.0.0"
    description: str = ""

    LABELS = ("name", "version", "description")

    def __post_init__(self) -> None:
        self.validate()

    # -- derived ------------------------------------------------------------

    @property
    def periods_per_year(self) -> int:
        return PERIODS[self.period]

    @property
    def scope_key(self) -> str:
        """A short, filename-safe name for the geography, e.g. `US:XX+YY` or `US:all`."""
        return f"{self.country}:{'+'.join(self.states) if self.states else 'all'}"

    @property
    def scope_label(self) -> str:
        if not self.states:
            return f"{self.country}, every region"
        return f"{self.country}, {', '.join(self.states)}"

    def all_years(self) -> tuple[int, ...]:
        return tuple(
            sorted(set(self.train_years + self.validate_years + self.test_years))
        )

    @property
    def splits(self) -> Splits:
        return Splits(
            train=Split(
                "train",
                self.train_years,
                f"fit models; the {len(self.train_years)}-year climatology window",
            ),
            validate=Split(
                "validate",
                self.validate_years,
                "iterate freely; scores may be read often",
            ),
            test=Split(
                "test",
                self.test_years,
                f"touched {self.test_touch_budget} time(s) per model version, "
                "then never again",
            ),
        )

    # -- identity ------------------------------------------------------------

    def criteria(self) -> dict:
        """Every field that is hashed: the contract minus its labels."""
        d = asdict(self)
        for label in self.LABELS:
            d.pop(label)
        return d

    def canonical_json(self) -> str:
        """Stable serialisation of the criteria — the thing that gets hashed."""
        return json.dumps(self.criteria(), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()[:16]

    # -- serialisation --------------------------------------------------------

    def to_spec(self) -> dict:
        """The JSON layout, as written to `contracts/<name>.json`."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "hazard": self.hazard,
            "event_types": list(self.event_types),
            "scope": {"country": self.country, "states": list(self.states)},
            "period": self.period,
            "damaging": {
                "property_usd_min": self.damage_property_usd_min,
                "count_casualties": self.damage_count_casualties,
            },
            "zone_policy": self.zone_policy,
            "splits": {
                "train": [self.train_years[0], self.train_years[-1]],
                "validate": [self.validate_years[0], self.validate_years[-1]],
                "test": [self.test_years[0], self.test_years[-1]],
            },
            "test_touch_budget": self.test_touch_budget,
            "reference_model": self.reference_model,
            "thresholds": {
                "min_brier_skill_score": self.min_brier_skill_score,
                "reliability_tolerance_pp": self.reliability_tolerance_pp,
                "reliability_min_bin_count": self.reliability_min_bin_count,
                "min_auc": self.min_auc,
                "n_reliability_bins": self.n_reliability_bins,
            },
        }

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_spec(cls, spec: Mapping) -> "Contract":
        """Build a contract from the JSON layout, filling defaults where allowed."""
        try:
            hazard = str(spec["hazard"])
            scope = spec.get("scope") or {}
            damaging = spec.get("damaging") or {}
            splits = spec["splits"]
            thresholds = spec.get("thresholds") or {}
        except KeyError as exc:
            raise ContractError(
                f"contract is missing required field {exc.args[0]!r}"
            ) from None
        except TypeError:
            raise ContractError("contract spec must be a JSON object") from None

        event_types = spec.get("event_types")
        if event_types is None:
            if hazard not in HAZARDS:
                raise ContractError(
                    f"hazard {hazard!r} is not in the catalogue and no event_types "
                    f"were given; known hazards: {sorted(HAZARDS)}"
                )
            event_types = HAZARDS[hazard].event_types

        return cls(
            name=str(spec.get("name", "")),
            version=str(spec.get("version", "1.0.0")),
            description=str(spec.get("description", "")),
            hazard=hazard,
            event_types=tuple(str(t) for t in event_types),
            country=str(scope.get("country", "US")),
            states=tuple(str(s).upper() for s in scope.get("states", ())),
            period=str(spec.get("period", "quarter")),
            damage_property_usd_min=float(damaging.get("property_usd_min", 10_000.0)),
            damage_count_casualties=bool(damaging.get("count_casualties", True)),
            zone_policy=str(spec.get("zone_policy", "drop")),
            train_years=_years(splits, "train"),
            validate_years=_years(splits, "validate"),
            test_years=_years(splits, "test"),
            test_touch_budget=int(spec.get("test_touch_budget", 1)),
            reference_model=str(spec.get("reference_model", REFERENCE_MODELS[0])),
            min_brier_skill_score=float(thresholds.get("min_brier_skill_score", 0.0)),
            reliability_tolerance_pp=float(
                thresholds.get("reliability_tolerance_pp", 0.05)
            ),
            reliability_min_bin_count=int(
                thresholds.get("reliability_min_bin_count", 30)
            ),
            min_auc=float(thresholds.get("min_auc", 0.70)),
            n_reliability_bins=int(thresholds.get("n_reliability_bins", 10)),
        )

    @classmethod
    def from_path(cls, path: pathlib.Path) -> "Contract":
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ContractError(f"{path}: not valid JSON ({exc})") from None
        contract = cls.from_spec(spec)
        if contract.name != path.stem:
            raise ContractError(
                f"{path.name} declares name {contract.name!r}; the file name and "
                "the contract name must agree so that a ledger can be found from "
                "either"
            )
        return contract

    def save(self, directory: pathlib.Path, *, force: bool = False) -> pathlib.Path:
        path = directory / f"{self.name}.json"
        if path.exists() and not force:
            raise ContractError(
                f"{path} already exists. A registered contract is not edited in "
                "place — its experiments carry its digest. Register a new name, "
                "or pass --force if nothing has been run against it yet."
            )
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_spec(), indent=2) + "\n", encoding="utf-8")
        return path

    # -- validation -----------------------------------------------------------

    def validate(self) -> None:
        if not NAME_RE.match(self.name):
            raise ContractError(
                f"contract name {self.name!r} must be lowercase letters, digits and "
                "hyphens (it names a file and a ledger directory)"
            )
        if not self.event_types:
            raise ContractError(f"contract {self.name!r} has no event types")
        if self.hazard in HAZARDS and set(self.event_types) - set(
            HAZARDS[self.hazard].event_types
        ):
            extra = sorted(set(self.event_types) - set(HAZARDS[self.hazard].event_types))
            raise ContractError(
                f"contract {self.name!r}: event types {extra} are not part of the "
                f"catalogued hazard {self.hazard!r}. Either use the catalogue's "
                "types or give the hazard a new name."
            )
        if self.country != "US":
            raise ContractError(
                f"contract {self.name!r}: country {self.country!r} is not supported "
                "by any connector yet; the Storm Events ground truth is US-only"
            )
        for state in self.states:
            if not STATE_RE.match(state):
                raise ContractError(
                    f"contract {self.name!r}: state {state!r} is not a two-letter code"
                )
        if len(set(self.states)) != len(self.states):
            raise ContractError(f"contract {self.name!r}: duplicate state in scope")
        if self.period not in PERIODS:
            raise ContractError(
                f"contract {self.name!r}: period {self.period!r} must be one of "
                f"{sorted(PERIODS)}"
            )
        if self.damage_property_usd_min < 0:
            raise ContractError(f"contract {self.name!r}: damage threshold is negative")
        if not (self.damage_property_usd_min > 0 or self.damage_count_casualties):
            raise ContractError(
                f"contract {self.name!r}: with a zero damage threshold and casualties "
                "not counted, every recorded event is 'damaging' — that is not a "
                "damage definition"
            )
        if self.zone_policy not in ZONE_POLICIES:
            raise ContractError(
                f"contract {self.name!r}: zone_policy {self.zone_policy!r} must be one "
                f"of {list(ZONE_POLICIES)}"
            )
        self._validate_splits()
        if self.test_touch_budget < 1:
            raise ContractError(
                f"contract {self.name!r}: test touch budget must be >= 1"
            )
        if self.reference_model not in REFERENCE_MODELS:
            raise ContractError(
                f"contract {self.name!r}: reference model {self.reference_model!r} is "
                f"not one the harness can compute; known: {list(REFERENCE_MODELS)}"
            )
        if not (0.0 < self.reliability_tolerance_pp < 1.0):
            raise ContractError(
                f"contract {self.name!r}: reliability tolerance must be in (0, 1)"
            )
        if self.reliability_min_bin_count < 1:
            raise ContractError(f"contract {self.name!r}: min bin count must be >= 1")
        if not (0.5 <= self.min_auc <= 1.0):
            raise ContractError(f"contract {self.name!r}: min AUC must be in [0.5, 1]")
        if self.n_reliability_bins < 2:
            raise ContractError(
                f"contract {self.name!r}: need at least 2 reliability bins"
            )

    def _validate_splits(self) -> None:
        for label, years in (
            ("train", self.train_years),
            ("validate", self.validate_years),
            ("test", self.test_years),
        ):
            if not years:
                raise ContractError(f"contract {self.name!r}: {label} split is empty")
            if years[0] < RECORD_START_YEAR:
                raise ContractError(
                    f"contract {self.name!r}: {label} split starts {years[0]}, before "
                    f"the record can be trusted ({RECORD_START_YEAR})"
                )
            if list(years) != list(range(years[0], years[-1] + 1)):
                raise ContractError(
                    f"contract {self.name!r}: {label} split must be a contiguous "
                    "run of years"
                )
        seen: dict[int, str] = {}
        for label, years in (
            ("train", self.train_years),
            ("validate", self.validate_years),
            ("test", self.test_years),
        ):
            for year in years:
                if year in seen:
                    raise ContractError(
                        f"contract {self.name!r}: year {year} appears in both "
                        f"{seen[year]!r} and {label!r}; splits must be disjoint or "
                        "every score is contaminated"
                    )
                seen[year] = label
        if not (max(self.train_years) < min(self.validate_years) < min(self.test_years)):
            raise ContractError(
                f"contract {self.name!r}: splits must be ordered in time — "
                "train, then validate, then test. Training on the future to "
                "predict the past is the classic leak."
            )

    # -- presentation ---------------------------------------------------------

    def describe(self) -> str:
        c = self
        unit = {"month": "month", "quarter": "quarter", "year": "year"}[c.period]
        lines = [
            f"contract        {c.name} {c.version}  (sha256:{c.digest()})",
            f"hazard          {c.hazard}  {list(c.event_types)}",
            f"geography       {c.scope_label}",
            f"forecast unit   region x {unit}",
            f"damaging event  property >= ${c.damage_property_usd_min:,.0f}"
            + (" or any casualty" if c.damage_count_casualties else ""),
            "zone events     "
            + (
                "expanded to every county in the zone (NWS crosswalk)"
                if c.zone_policy == "expand"
                else "dropped (do not join to counties)"
            ),
            f"train           {c.train_years[0]}-{c.train_years[-1]}"
            f"  ({len(c.train_years)}y)",
            f"validate        {c.validate_years[0]}-{c.validate_years[-1]}"
            f"  ({len(c.validate_years)}y)",
            f"test            {c.test_years[0]}-{c.test_years[-1]}"
            f"  ({len(c.test_years)}y, {c.test_touch_budget} touch)",
            f"reference       {c.reference_model}",
            f"passes when     BSS > {c.min_brier_skill_score}"
            f"  |  reliability within +/-{c.reliability_tolerance_pp:.0%}"
            f" per populated bin (n >= {c.reliability_min_bin_count})"
            f"  |  AUC >= {c.min_auc}",
        ]
        if c.description:
            lines.insert(1, f"                {c.description}")
        return "\n".join(lines)


def _years(splits: Mapping, label: str) -> tuple[int, ...]:
    try:
        raw = splits[label]
    except (KeyError, TypeError):
        raise ContractError(
            f"splits.{label} is required, as [first_year, last_year]"
        ) from None
    if isinstance(raw, str) and YEAR_RANGE_RE.match(raw):
        first, last = (int(x) for x in YEAR_RANGE_RE.match(raw).groups())
    elif isinstance(raw, Sequence) and len(raw) == 2 and not isinstance(raw, str):
        first, last = int(raw[0]), int(raw[1])
    else:
        raise ContractError(
            f"splits.{label} must be [first_year, last_year] or 'YYYY-YYYY', got {raw!r}"
        )
    if last < first:
        raise ContractError(f"splits.{label}: {first}-{last} runs backwards")
    return tuple(range(first, last + 1))


# --------------------------------------------------------------------------
# Building a contract from options (the `readiness register` command)
# --------------------------------------------------------------------------


def new(
    name: str,
    *,
    hazard: str,
    states: Sequence[str] = (),
    period: str = "quarter",
    event_types: Sequence[str] | None = None,
    property_usd_min: float = 10_000.0,
    count_casualties: bool = True,
    zone_policy: str = "drop",
    train: str = "1996-2015",
    validate: str = "2016-2020",
    test: str = "2021-2025",
    description: str = "",
    version: str = "1.0.0",
    **thresholds,
) -> Contract:
    spec: dict = {
        "name": name,
        "version": version,
        "description": description,
        "hazard": hazard,
        "scope": {"country": "US", "states": list(states)},
        "period": period,
        "damaging": {
            "property_usd_min": property_usd_min,
            "count_casualties": count_casualties,
        },
        "zone_policy": zone_policy,
        "splits": {"train": train, "validate": validate, "test": test},
        "thresholds": {k: v for k, v in thresholds.items() if v is not None},
    }
    if event_types is not None:
        spec["event_types"] = list(event_types)
    return Contract.from_spec(spec)


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------


def contracts_dir(directory: pathlib.Path | None = None) -> pathlib.Path:
    if directory is not None:
        return directory
    override = os.environ.get(CONTRACTS_DIR_ENV)
    return pathlib.Path(override) if override else DEFAULT_CONTRACTS_DIR


def registered(directory: pathlib.Path | None = None) -> dict[str, Contract]:
    """Every contract in the registry, by name. A broken file is an error, not a skip."""
    root = contracts_dir(directory)
    found: dict[str, Contract] = {}
    if not root.exists():
        return found
    for path in sorted(root.glob("*.json")):
        found[path.stem] = Contract.from_path(path)
    return found


def load(name_or_path: str, directory: pathlib.Path | None = None) -> Contract:
    """A contract by registered name, or by path to a JSON file."""
    candidate = pathlib.Path(name_or_path)
    if candidate.suffix == ".json" or "/" in name_or_path:
        if not candidate.exists():
            raise ContractError(f"no contract file at {candidate}")
        return Contract.from_path(candidate)
    path = contracts_dir(directory) / f"{name_or_path}.json"
    if not path.exists():
        known = sorted(registered(directory))
        raise ContractError(
            f"no registered contract named {name_or_path!r}"
            + (f"; registered: {known}" if known else "; the registry is empty")
            + ". Register one with `readiness register`."
        )
    return Contract.from_path(path)


def resolve(name: str | None = None, directory: pathlib.Path | None = None) -> Contract:
    """The contract a command should run against.

    Explicit name or path first; then the `READINESS_CONTRACT` environment
    variable; then the sole registered contract if there is exactly one.
    Anything else is an error that lists the choices — a command must never
    silently pick a contract.
    """
    if name:
        return load(name, directory)
    env = os.environ.get(CONTRACT_ENV)
    if env:
        return load(env, directory)
    known = registered(directory)
    if len(known) == 1:
        return next(iter(known.values()))
    if not known:
        raise ContractError(
            "no contract is registered. Register one first, for example:\n"
            "    readiness register flood-xx --hazard inland_flood --state XX\n"
            "then pass it with -c/--contract (or set READINESS_CONTRACT)."
        )
    raise ContractError(
        "several contracts are registered; choose one with -c/--contract "
        f"(or set {CONTRACT_ENV}): {sorted(known)}"
    )


def describe_registry(directory: pathlib.Path | None = None) -> str:
    known = registered(directory)
    if not known:
        return f"no contracts registered in {contracts_dir(directory)}"
    width = max(len(n) for n in known) + 2
    hazard_w = max([len("hazard")] + [len(c.hazard) for c in known.values()]) + 2
    scope_w = max([len("scope")] + [len(c.scope_key) for c in known.values()]) + 2
    lines = [
        f"  {'name':<{width}}{'hazard':<{hazard_w}}{'scope':<{scope_w}}{'period':<9}"
        f"{'sha256':<18}description"
    ]
    for name, c in known.items():
        lines.append(
            f"  {name:<{width}}{c.hazard:<{hazard_w}}{c.scope_key:<{scope_w}}"
            f"{c.period:<9}{c.digest():<18}{c.description}"
        )
    return "\n".join(lines)
