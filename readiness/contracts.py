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
from types import MappingProxyType
from typing import Iterator, Mapping, Sequence

from readiness.config import (
    GROUND_TRUTH_SOURCES,
    HAZARD_CATEGORIES,
    HAZARDS,
    global_hazards,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CONTRACTS_DIR = REPO_ROOT / "contracts"

#: Environment overrides, so tests and CI can point at a different registry
#: without touching the repository's.
CONTRACTS_DIR_ENV = "READINESS_CONTRACTS_DIR"
CONTRACT_ENV = "READINESS_CONTRACT"

PERIODS: dict[str, int] = {"month": 12, "quarter": 4, "year": 1}
ZONE_POLICIES: tuple[str, ...] = ("drop", "expand")

#: Admin levels a geoBoundaries release is read at. ADM1 is a province or
#: state, ADM2 a district or municipality. Lower than ADM2 is not offered:
#: the ground truth is too sparse to populate a reliability bin there, and a
#: contract that cannot fill a bin cannot be judged.
ADMIN_LEVELS: tuple[str, ...] = ("ADM1", "ADM2")

#: Region universes the data plane can assemble.
REGION_SOURCES: tuple[str, ...] = ("census", "geoboundaries")

#: The geoBoundaries release `readiness register` writes when none is named.
#: A literal, not an import: `contracts` must not depend on a connector (the
#: label builder imports one already, and the cycle would close).
#: `tests/test_connectors.py` ties it to `geoboundaries.DEFAULT_RELEASE`.
GEOBOUNDARIES_RELEASE = "gbOpen 6.0.0"

#: What a contract that predates the schema's two source fields meant.
#:
#: Every contract registered before Phase 4 was implicitly US Storm Events
#: against the Census county universe, and its digest was computed over a
#: criteria dict that had no such fields. `criteria()` therefore drops any
#: field listed here whose value *is* the default, so those digests do not
#: move and a spec that names the defaults explicitly hashes the same as one
#: that omits them. A pilot names something else, the field survives the
#: elision, and the digest says so.
SCHEMA_V1_DEFAULTS: Mapping[str, dict] = MappingProxyType(
    {
        "ground_truth": {"source": "storm_events"},
        "regions": {"source": "census"},
    }
)

#: The only reference forecast the harness implements: the unsmoothed training
#: base rate, issued everywhere. A contract must name it so the choice is
#: explicit on the card, but the harness will not fit anything else as a
#: reference — see `readiness.harness.scoring.climatology_reference`.
REFERENCE_MODELS: tuple[str, ...] = ("climatology-pooled",)

#: What a contract gets for everything it does not say. This is the one
#: statement of the documented defaults: `Contract.from_spec` fills a spec's
#: gaps from it, `new()` reads its keyword defaults from it, and the
#: `readiness register` parser takes every `default=` and "default N" help
#: text from it, so the three cannot disagree. Read-only, because a default
#: that one caller edits at run time is a criterion nobody hashed.
DEFAULTS: Mapping[str, object] = MappingProxyType(
    {
        "version": "1.0.0",
        "country": "US",
        "period": "quarter",
        "property_usd_min": 10_000.0,
        "count_casualties": True,
        "zone_policy": "drop",
        "train": "1996-2015",
        "validate": "2016-2020",
        "test": "2021-2025",
        "test_touch_budget": 1,
        "reference_model": REFERENCE_MODELS[0],
        "min_brier_skill_score": 0.0,
        "reliability_tolerance_pp": 0.05,
        "reliability_min_bin_count": 30,
        "min_auc": 0.70,
        "n_reliability_bins": 10,
        # The pilot side of `readiness register`: what it writes when a
        # contract says nothing. `regions.source` has no flag — outside the US
        # there is one region universe and it is geoBoundaries — so it is not
        # listed here; `SCHEMA_V1_DEFAULTS` is where the *schema's* defaults
        # live, and this mapping is only what the command line falls back to.
        "ground_truth": "storm_events",
        "admin_level": ADMIN_LEVELS[0],
    }
)

# Every pattern below ends in `\Z`, not `$`. Python's `$` also matches just
# before a final newline, so `"ZZ\n"` would validate as a country code and
# then reach a manifest key, a cache filename and a URL. `\Z` is the end of
# the string and nothing else.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")
STATE_RE = re.compile(r"^[A-Z]{2}\Z")
#: ISO 3166-1 alpha-2, the code geoBoundaries files a release under.
COUNTRY_RE = re.compile(r"^[A-Z]{2}\Z")
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
#: A ground-truth file is named by its basename alone. The directory is a
#: property of whoever holds the bytes, and a committed contract must not
#: record one operator's filesystem.
BASENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
#: The one basename in `snapshots/records/` that is *not* a ground-truth file:
#: the committed EM-DAT admin-name crosswalk (`emdat.crosswalk_path`). It is
#: the single re-inclusion in `.gitignore`, so a contract that pinned a
#: partner file under that name would have git commit the bytes. Refused here
#: as well, because a structural guard belongs on both sides of the boundary.
CROSSWALK_BASENAME_RE = re.compile(r"^[A-Za-z]{2}_emdat_regions\.csv\Z")
#: `regions.release`, checked at registration rather than at build time: a
#: contract is a pre-registered, hashed artefact, so a release geoBoundaries
#: cannot resolve must not reach a digest. Kept in step with
#: `connectors.geoboundaries._RELEASE_RE` by a test.
RELEASE_RE = re.compile(r"^gbOpen[ @][0-9][0-9A-Za-z._-]*\Z")
YEAR_RANGE_RE = re.compile(r"^(\d{4})-(\d{4})\Z")


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
    #: Where the labels come from. `{"source": "storm_events"}` — the default,
    #: and the only thing a US contract may say. A pilot names its own record:
    #: `{"source": "national_records" | "emdat", "file": ..., "sha256": ...,
    #: "record_start_year": ...}`. The sha256 is a criterion, so a partner
    #: sending a corrected export produces a visibly different contract rather
    #: than a quietly different panel under the same digest.
    ground_truth: dict = field(default_factory=dict)
    #: Where the region universe comes from. `{"source": "census"}` by default;
    #: a pilot names `{"source": "geoboundaries", "admin_level": "ADM1"|"ADM2",
    #: "release": "gbOpen 6.0.0"}`.
    regions: dict = field(default_factory=dict)
    version: str = DEFAULTS["version"]
    description: str = ""

    LABELS = ("name", "version", "description")

    def __post_init__(self) -> None:
        # A contract built by `dataclasses.replace` or by hand skips
        # `from_spec`, so the two source fields are defaulted here as well.
        # Frozen, hence `object.__setattr__`; nothing else in this class needs
        # it, and nothing outside may do it.
        for name, default in SCHEMA_V1_DEFAULTS.items():
            if not getattr(self, name):
                object.__setattr__(self, name, dict(default))
        self.validate()

    # -- derived ------------------------------------------------------------

    @property
    def periods_per_year(self) -> int:
        return PERIODS[self.period]

    @property
    def ground_truth_source(self) -> str:
        return str(self.ground_truth.get("source", "storm_events"))

    @property
    def regions_source(self) -> str:
        return str(self.regions.get("source", "census"))

    @property
    def is_pilot(self) -> bool:
        """Whether this contract is scored outside the United States."""
        return self.country != "US"

    @property
    def admin_level(self) -> str:
        """The geoBoundaries level a pilot's regions come from; "county" in the US."""
        return str(self.regions.get("admin_level", "county"))

    @property
    def record_start_year(self) -> int:
        """The first year this contract's ground truth may be trusted.

        `config.GROUND_TRUTH_SOURCES` holds the floor per source, and `None`
        there means only the contract can say — a partner's archive starts
        where their archive starts. No split may begin before this year.
        """
        floor = GROUND_TRUTH_SOURCES.get(self.ground_truth_source)
        declared = self.ground_truth.get("record_start_year")
        if declared is None:
            if floor is None:
                raise ContractError(
                    f"contract {self.name!r}: ground_truth.source "
                    f"{self.ground_truth_source!r} carries no fixed start year, so "
                    "the contract must declare ground_truth.record_start_year"
                )
            return int(floor)
        return int(declared)

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
        """Every field that is hashed: the contract minus its labels.

        A schema field still at its documented default is dropped rather than
        hashed (`SCHEMA_V1_DEFAULTS`). Two things follow, both wanted: every
        digest committed before Phase 4 stays exactly what it was, and a spec
        that spells the defaults out hashes the same as one that leaves them
        implicit — the criteria are what the contract *means*, not how much of
        it the author chose to type.
        """
        d = asdict(self)
        for label in self.LABELS:
            d.pop(label)
        for name, default in SCHEMA_V1_DEFAULTS.items():
            if d.get(name) == default:
                d.pop(name)
        return d

    def canonical_json(self) -> str:
        """Stable serialisation of the criteria — the thing that gets hashed."""
        return json.dumps(self.criteria(), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()[:16]

    # -- serialisation --------------------------------------------------------

    def to_spec(self) -> dict:
        """The JSON layout, as written to `contracts/<name>.json`."""
        spec: dict = {
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
        for name in SCHEMA_V1_DEFAULTS:
            value = getattr(self, name)
            if value != SCHEMA_V1_DEFAULTS[name]:
                spec[name] = dict(value)
        return spec

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_spec(cls, spec: Mapping) -> "Contract":
        """Build a contract from the JSON layout, filling defaults where allowed."""
        if not isinstance(spec, Mapping):
            raise ContractError("contract spec must be a JSON object")
        try:
            hazard = str(spec["hazard"])
            splits = spec["splits"]
        except KeyError as exc:
            raise ContractError(
                f"contract is missing required field {exc.args[0]!r}"
            ) from None
        scope = _section(spec, "scope")
        damaging = _section(spec, "damaging")
        thresholds = _section(spec, "thresholds")

        d = DEFAULTS
        country = str(scope.get("country", d["country"])).upper()

        event_types = spec.get("event_types")
        if country != "US":
            # Outside the US there is no Storm Events vocabulary to name, so
            # the field is emptied whether or not the spec named one. The
            # hazard reaches the records through `config.HAZARD_CATEGORIES`,
            # which `validate` insists on and `_hazard_matches` short-circuits
            # for a `RecordEvent`, so copying NOAA's strings into a pilot's
            # digest would hash a criterion nothing reads — and two pilots with
            # identical panels would carry different digests, which marks their
            # ledgers incomparable for no reason. `readiness register` refuses
            # `--event-type` outside the US rather than dropping it silently.
            event_types = ()
        elif event_types is None:
            if hazard in HAZARDS:
                event_types = HAZARDS[hazard].event_types
            else:
                raise ContractError(
                    f"hazard {hazard!r} is not in the catalogue and no event_types "
                    f"were given; known hazards: {sorted(HAZARDS)}"
                )

        return cls(
            name=str(spec.get("name", "")),
            version=str(spec.get("version", d["version"])),
            description=str(spec.get("description", "")),
            hazard=hazard,
            event_types=_cast("event_types", _strings, event_types, "a list"),
            country=country,
            states=_cast(
                "scope.states", _state_codes, scope.get("states", ()), "a list"
            ),
            period=str(spec.get("period", d["period"])),
            damage_property_usd_min=_cast(
                "damaging.property_usd_min", float,
                damaging.get("property_usd_min", d["property_usd_min"]),
            ),
            damage_count_casualties=bool(
                damaging.get("count_casualties", d["count_casualties"])
            ),
            zone_policy=str(spec.get("zone_policy", d["zone_policy"])),
            train_years=_years(splits, "train"),
            validate_years=_years(splits, "validate"),
            test_years=_years(splits, "test"),
            test_touch_budget=_cast(
                "test_touch_budget", int,
                spec.get("test_touch_budget", d["test_touch_budget"]),
            ),
            reference_model=str(spec.get("reference_model", d["reference_model"])),
            min_brier_skill_score=_threshold(thresholds, "min_brier_skill_score", float),
            reliability_tolerance_pp=_threshold(
                thresholds, "reliability_tolerance_pp", float
            ),
            reliability_min_bin_count=_threshold(
                thresholds, "reliability_min_bin_count", int
            ),
            min_auc=_threshold(thresholds, "min_auc", float),
            n_reliability_bins=_threshold(thresholds, "n_reliability_bins", int),
            ground_truth=_source_section(spec, "ground_truth"),
            regions=_source_section(spec, "regions"),
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
        if not COUNTRY_RE.match(self.country):
            raise ContractError(
                f"contract {self.name!r}: country {self.country!r} must be an "
                "uppercase ISO 3166-1 alpha-2 code (the code geoBoundaries files "
                "a release under)"
            )
        if not self.event_types and not self.is_pilot:
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
        self._validate_sources()
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

    def _validate_sources(self) -> None:
        """Where the labels and the regions come from, and what that requires.

        The United States is the one place the harness has a default answer
        for: Storm Events against the Census county universe, which is what
        every contract written before Phase 4 meant. Everywhere else the
        contract must say, in full, which record it is judged against and which
        boundary release its regions are — because "whatever the connector
        happened to download" is not a pre-registered criterion, and a pilot
        whose ground truth silently changes edition has no reproducible panel.
        """
        name = self.name
        gt_source = self.ground_truth_source
        rg_source = self.regions_source
        if gt_source not in GROUND_TRUTH_SOURCES:
            raise ContractError(
                f"contract {name!r}: ground_truth.source {gt_source!r} is not one the "
                f"harness can read; known: {sorted(GROUND_TRUTH_SOURCES)}"
            )
        if rg_source not in REGION_SOURCES:
            raise ContractError(
                f"contract {name!r}: regions.source {rg_source!r} is not one the "
                f"harness can read; known: {sorted(REGION_SOURCES)}"
            )
        if not self.is_pilot:
            if gt_source != "storm_events" or rg_source != "census":
                raise ContractError(
                    f"contract {name!r}: a US contract is scored against Storm Events "
                    "over the Census county universe; it cannot name "
                    f"ground_truth.source {gt_source!r} with regions.source "
                    f"{rg_source!r}"
                )
            extra = sorted(
                (set(self.ground_truth) | set(self.regions)) - {"source"}
            )
            if extra:
                raise ContractError(
                    f"contract {name!r}: a US contract declares only the source of "
                    f"each, and this one also carries {extra}. Those fields exist so "
                    "that a pilot can pin a record file by hash; in the US the record "
                    "is a public archive the connector pins for itself."
                )
            return

        # -- a pilot ---------------------------------------------------------
        if gt_source == "storm_events":
            raise ContractError(
                f"contract {name!r}: Storm Events is a US record, so a contract for "
                f"{self.country} must declare ground_truth.source as one of "
                "['emdat', 'national_records'] (plan §5)"
            )
        if rg_source != "geoboundaries":
            raise ContractError(
                f"contract {name!r}: outside the US the region universe comes from "
                f"geoBoundaries, not {rg_source!r}"
            )
        if self.states:
            raise ContractError(
                f"contract {name!r}: scope.states is a two-letter US state list and "
                f"must be empty for {self.country}; the sub-national universe is "
                "regions.admin_level"
            )
        if self.zone_policy != "drop":
            raise ContractError(
                f"contract {name!r}: zone_policy {self.zone_policy!r} expands NWS "
                "forecast zones to US counties, which do not exist in "
                f"{self.country}; it must be 'drop' outside the US"
            )
        if self.hazard not in HAZARD_CATEGORIES:
            raise ContractError(
                f"contract {name!r}: hazard {self.hazard!r} has no global mapping, so "
                "no EM-DAT type or partner hazard value can be matched to it; "
                f"mapped hazards: {list(global_hazards())}"
            )
        if gt_source not in HAZARD_CATEGORIES[self.hazard]:
            # The table is data meant to be extended, so an entry that maps a
            # hazard for one global source and not the other is expected to
            # happen. It must be refused here, not at build time: a contract is
            # hashed before anything reads a record.
            raise ContractError(
                f"contract {name!r}: hazard {self.hazard!r} has no {gt_source!r} "
                "mapping in config.HAZARD_CATEGORIES, so no record value can be "
                f"matched to it; it maps only "
                f"{sorted(HAZARD_CATEGORIES[self.hazard])}. Hazards registrable "
                f"outside the US: {list(global_hazards())}"
            )
        sha = str(self.ground_truth.get("sha256", ""))
        if not SHA256_RE.match(sha):
            raise ContractError(
                f"contract {name!r}: ground_truth.sha256 must be 64 lowercase hex "
                f"digits, got {sha!r}. `readiness register --records PATH` computes it "
                "from the file; the bytes are never committed, the hash is."
            )
        basename = str(self.ground_truth.get("file", ""))
        if not BASENAME_RE.match(basename):
            raise ContractError(
                f"contract {name!r}: ground_truth.file must be the records file's "
                f"basename (it names the manifest key), got {basename!r}"
            )
        if CROSSWALK_BASENAME_RE.match(basename):
            raise ContractError(
                f"contract {name!r}: ground_truth.file {basename!r} is the shape of "
                "the committed EM-DAT admin-name crosswalk, which is the one file "
                "under snapshots/records/ that git does *not* ignore. A ground-truth "
                "file named that way would be committed; rename the export."
            )
        floor = GROUND_TRUTH_SOURCES[gt_source]
        declared = self.ground_truth.get("record_start_year")
        if declared is None:
            raise ContractError(
                f"contract {name!r}: ground_truth.record_start_year is required for "
                f"{gt_source!r} — the first year the record is complete enough to "
                "read an absence as a zero rather than as a gap"
            )
        if not isinstance(declared, int) or isinstance(declared, bool):
            # Not coerced: the value is hashed as it is written, so `"2005"`
            # and `2005` would be the same contract under two digests.
            raise ContractError(
                f"contract {name!r}: ground_truth.record_start_year must be a JSON "
                f"integer, got {declared!r} ({type(declared).__name__}). It is "
                "hashed as written, so a string and a number would be the same "
                "contract with two digests."
            )
        year = int(declared)
        if floor is not None and year < floor:
            raise ContractError(
                f"contract {name!r}: ground_truth.record_start_year {year} precedes "
                f"{floor}, the first year {gt_source!r} can be trusted "
                "(readiness/config.py: GROUND_TRUTH_SOURCES)"
            )
        level = str(self.regions.get("admin_level", ""))
        if level not in ADMIN_LEVELS:
            raise ContractError(
                f"contract {name!r}: regions.admin_level must be one of "
                f"{list(ADMIN_LEVELS)}, got {level!r}"
            )
        release = str(self.regions.get("release", ""))
        if not RELEASE_RE.match(release):
            raise ContractError(
                f"contract {name!r}: regions.release must be \"gbOpen <version>\" "
                f'(e.g. "{GEOBOUNDARIES_RELEASE}"), got {release!r}. Boundaries are '
                "renumbered between releases, so a panel is only reproducible "
                "against a release the connector can actually resolve — and a "
                "contract is hashed before anything tries."
            )
        regions_sha = self.regions.get("sha256")
        if regions_sha is not None and not SHA256_RE.match(str(regions_sha)):
            raise ContractError(
                f"contract {name!r}: regions.sha256, when given, must be 64 "
                f"lowercase hex digits, got {regions_sha!r}. It is optional (an "
                "existing pilot's digest does not move when it is absent) and "
                "pins the boundary file's exact bytes when present."
            )
        extra_gt = sorted(
            set(self.ground_truth)
            - {"source", "file", "sha256", "record_start_year"}
        )
        extra_rg = sorted(
            set(self.regions) - {"source", "admin_level", "release", "sha256"}
        )
        if extra_gt or extra_rg:
            # Exactly the rule the US branch applies. `to_spec` writes both
            # sections back verbatim and `criteria()` hashes them, so an extra
            # key — an operator's absolute path, say — would be committed,
            # packed and published, which is what BASENAME_RE exists to prevent.
            raise ContractError(
                f"contract {name!r}: ground_truth and regions carry only the keys "
                f"the schema defines, and this one also carries "
                f"{extra_gt + extra_rg}. Both sections are hashed and published "
                "verbatim, so an unknown key is a criterion nobody agreed to."
            )

    def _validate_splits(self) -> None:
        for label, years in (
            ("train", self.train_years),
            ("validate", self.validate_years),
            ("test", self.test_years),
        ):
            if not years:
                raise ContractError(f"contract {self.name!r}: {label} split is empty")
            if years[0] < self.record_start_year:
                raise ContractError(
                    f"contract {self.name!r}: {label} split starts {years[0]}, before "
                    f"the record can be trusted ({self.record_start_year}, "
                    f"{self.ground_truth_source})"
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

    def hazard_values(self) -> list[str]:
        """The record values that constitute this hazard, in its own vocabulary.

        Storm Events event types in the US; the EM-DAT type/subtype strings or
        the partner's hazard values for a pilot (`config.HAZARD_CATEGORIES`).
        """
        if not self.is_pilot:
            return list(self.event_types)
        mapping = HAZARD_CATEGORIES.get(self.hazard, {})
        return list(mapping.get(self.ground_truth_source, ()))

    def ground_truth_label(self) -> str:
        """One line naming the record the labels come from, and from when."""
        if self.ground_truth_source == "storm_events":
            return f"NOAA Storm Events, from {self.record_start_year}"
        what = {
            "national_records": "partner national records",
            "emdat": "EM-DAT export",
        }[self.ground_truth_source]
        sha = str(self.ground_truth.get("sha256", ""))[:16]
        return (
            f"{what} {self.ground_truth.get('file', '')} "
            f"(sha256:{sha}), from {self.record_start_year}"
        )

    def regions_label(self) -> str:
        """One line naming the region universe."""
        if self.regions_source == "census":
            return "US Census counties (national county file, 2020)"
        return (
            f"geoBoundaries {self.admin_level} for {self.country} "
            f"({self.regions.get('release', '')})"
        )

    def describe(self) -> str:
        c = self
        unit = {"month": "month", "quarter": "quarter", "year": "year"}[c.period]
        lines = [
            f"contract        {c.name} {c.version}  (sha256:{c.digest()})",
            f"hazard          {c.hazard}  {c.hazard_values()}",
            f"geography       {c.scope_label}",
            f"ground truth    {c.ground_truth_label()}",
            f"regions         {c.regions_label()}",
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


def _cast(field: str, caster, value, kind: str | None = None):
    """Coerce one spec value, naming the field when it cannot be coerced.

    A `float("high")` or `int(None)` deep inside `from_spec` would otherwise
    surface as a traceback that never says which field of the contract was
    wrong; the CLI turns a `ContractError` into a one-line refusal instead.
    """
    try:
        return caster(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(
            f"contract field {field!r}: cannot read {value!r} as "
            f"{kind or caster.__name__} ({exc})"
        ) from None


def _section(spec: Mapping, key: str) -> Mapping:
    """An optional nested object of the spec, absent meaning empty."""
    value = spec.get(key) or {}
    if not isinstance(value, Mapping):
        raise ContractError(
            f"contract field {key!r} must be a JSON object, got {value!r}"
        )
    return value


def _source_section(spec: Mapping, key: str) -> dict:
    """One of the two source sections, defaulted from `SCHEMA_V1_DEFAULTS`.

    Absent means the documented default, which is what every contract written
    before Phase 4 meant. Present but not an object is a mistake worth naming.
    """
    if spec.get(key) is None:
        return dict(SCHEMA_V1_DEFAULTS[key])
    value = spec[key]
    if not isinstance(value, Mapping):
        raise ContractError(
            f"contract field {key!r} must be a JSON object, got {value!r}"
        )
    return dict(value)


def _threshold(thresholds: Mapping, key: str, caster):
    return _cast(f"thresholds.{key}", caster, thresholds.get(key, DEFAULTS[key]))


def _strings(values) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError("expected a list of strings, not one string")
    return tuple(str(v) for v in values)


def _state_codes(values) -> tuple[str, ...]:
    return tuple(s.upper() for s in _strings(values))


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
        first = _cast(f"splits.{label}[0]", int, raw[0])
        last = _cast(f"splits.{label}[1]", int, raw[1])
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
    country: str = DEFAULTS["country"],
    states: Sequence[str] = (),
    ground_truth: Mapping | None = None,
    regions: Mapping | None = None,
    period: str = DEFAULTS["period"],
    event_types: Sequence[str] | None = None,
    property_usd_min: float = DEFAULTS["property_usd_min"],
    count_casualties: bool = DEFAULTS["count_casualties"],
    zone_policy: str = DEFAULTS["zone_policy"],
    train: str = DEFAULTS["train"],
    validate: str = DEFAULTS["validate"],
    test: str = DEFAULTS["test"],
    description: str = "",
    version: str = DEFAULTS["version"],
    **thresholds,
) -> Contract:
    """A contract from options, the way `readiness register` builds one.

    A threshold passed as None means "the default", so a caller can forward
    optional values without knowing them; `from_spec` fills the gaps from
    `DEFAULTS`.
    """
    spec: dict = {
        "name": name,
        "version": version,
        "description": description,
        "hazard": hazard,
        "scope": {"country": str(country).upper(), "states": list(states)},
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
    if ground_truth is not None:
        spec["ground_truth"] = dict(ground_truth)
    if regions is not None:
        spec["regions"] = dict(regions)
    return Contract.from_spec(spec)


def pilot_sources(
    *,
    source: str,
    file: str,
    sha256: str,
    record_start_year: int,
    admin_level: str | None = None,
    release: str | None = None,
    regions_sha256: str | None = None,
) -> tuple[dict, dict]:
    """The `(ground_truth, regions)` pair a pilot contract needs, as data.

    One place builds the two sections, so `readiness register --country ZZ`,
    a test fixture and anything written later cannot disagree about which keys
    a pilot carries. `admin_level` and `release` default here rather than in
    the argument parser, so that `readiness register` can tell "not given"
    from "given" and refuse either for a US contract instead of ignoring it.

    `regions_sha256` is optional and omitted from the section when absent, so
    a pilot registered without it keeps the digest it already has.
    """
    regions: dict = {
        "source": "geoboundaries",
        "admin_level": admin_level or DEFAULTS["admin_level"],
        "release": release or GEOBOUNDARIES_RELEASE,
    }
    if regions_sha256:
        regions["sha256"] = regions_sha256
    return (
        {
            "source": source,
            "file": file,
            "sha256": sha256,
            "record_start_year": int(record_start_year),
        },
        regions,
    )


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
