"""The facility record: what a planner tells us, and what we refuse to be told.

Report §6 Phase 3 asks for "an agent that takes a facility or jurisdiction
(address, occupancy, dependencies) plus the validated risk layer". Plan §4
narrows that deliberately: **no address or coordinate field exists here**. The
design intensity that inject 3 of the 96-hour scenario needs — the base flood
elevation for this building — is supplied by the planner from their own
Elevation Certificate or FIRM panel and cited as a facility document. Nothing
address-level is ever pinned, joined or written, so nothing address-level can
leak out of a report, a review record or the manifest.

Four rules make the record trustworthy enough to reason over:

1. **Unknown keys are refused, by name.** A field this module does not know is
   a field no rule reads, and a silent drop would make a gap report look
   complete when the planner thought it had said something.
2. **Every populated leaf must be named by an evidence key.** `power.fuel_hours`
   is not a number a planner remembers; it is a number on a document. The
   evidence entry says which document and which page, and every sentence a
   rule writes about that field cites the field path, which resolves back here.
3. **Keys *and* values are scanned for places.** `FORBIDDEN_KEYS` is the key
   scan; `FORBIDDEN_VALUE_PATTERNS` is the value scan, because a street
   address in a free-text note is an address whatever the key is called. A
   bare five-digit number is a county FIPS and is not a finding.
4. **Every record carries its own random `blind_id`.** The label a blinded
   report shows is a prefix of it, so the blinded artefact carries no preimage
   of the slug, the county or anything else a reader could guess and confirm.

Every string is whitespace-normalised at load, so one name has one spelling
and the blinding cannot be defeated by a double space.

Standard library only; no LLM, no labels, no scoring.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
from typing import Any, Mapping, Sequence

#: Keys that would name a place finer than a county. None of them exists in the
#: schema; this tuple is the tripwire that says so out loud when one appears in
#: a file a planner hands us.
FORBIDDEN_KEYS: tuple[str, ...] = (
    "address", "address_line1", "address_line2", "apn", "block", "block_group",
    "coordinates", "easting", "geocode", "geohash", "geometry", "gps",
    "lat", "lat_lon", "latitude", "latlon", "lon", "longitude", "northing",
    "parcel", "plus_code", "postal_code", "street", "tract", "zip", "zipcode",
)

#: Strings that name a place finer than a county even when the key does not.
#: A key scan alone is a fail-open: `"note": "412 Riverside Drive, 27834-1234"`
#: carries an address under a name no list could enumerate.
#:
#: A **bare five-digit number is deliberately absent**: that is a county FIPS,
#: which is the one geography this repository does publish. Only ZIP+4 (which
#: no county FIPS can be) and the literal token "ZIP" are matched.
FORBIDDEN_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "a street address",
        re.compile(
            r"\b\d{1,6}\s+\w+(\s+\w+)?\s+"
            r"(St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane|Blvd|Boulevard|Way|"
            r"Hwy|Highway)\b",
            re.IGNORECASE,
        ),
    ),
    ("a ZIP+4 postcode", re.compile(r"\b\d{5}-\d{4}\b")),
    (
        "a decimal-degree coordinate pair",
        re.compile(r"-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}"),
    ),
    ("the token 'ZIP'", re.compile(r"(?<!\w)ZIP(?!\w)", re.IGNORECASE)),
)

#: Why the refusal above is a design decision rather than an oversight.
FORBIDDEN_REASON = (
    "the facility record carries no address or coordinate field by construction "
    "(plan §4): address-level intensity would have to be pinned somewhere, and "
    "report §7 publishes county aggregates only. Supply the design intensity "
    "from your own Elevation Certificate or FIRM panel through "
    "design_intensity.flood_elevation_ft, cited as a facility document"
)

OCCUPANCY_TYPES: tuple[str, ...] = (
    "hospital", "nursing_home", "shelter", "school", "other",
)

#: The number of hex characters of a facility's `blind_id` a blinded report
#: shows. The label is a prefix of a random id, not a digest of anything a
#: reader could guess: a six-hex digest of a slug is a dictionary attack away
#: from the slug, and this is the identifier a blinded artefact carries.
BLIND_CHARS = 12

#: How a planner makes the `blind_id` a record needs.
BLIND_ID_COMMAND = 'python3 -c "import secrets; print(secrets.token_hex(16))"'

_BLIND_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: The shape of the label a blinded page and a review record carry.
BLIND_LABEL_RE = re.compile(r"^FACILITY-[0-9a-f]{12}$")


class FacilityError(ValueError):
    """A facility file we will not read. Every message names the field."""


# --------------------------------------------------------------------------- #
# The schema, as data
# --------------------------------------------------------------------------- #

#: A leaf spec is (type name, required?). Type names are checked by `_coerce`.
#: `number` accepts an int or a float; `int` refuses a float; a `+` suffix
#: refuses a negative (there is no such thing as -5 hours of fuel or -3 feet of
#: notice); `?` suffixes mark a leaf that may be null — which is not the same
#: as absent. The order is `kind` then `+` then `?`: `"number+?"`.
SCHEMA: dict[str, Any] = {
    "slug": "slug",
    "blind_id": "blind_id",
    "occupancy_type": "occupancy",
    "county_fips": "fips",
    "census": "int+",
    "staff_on_shift": "int+",
    "power": {
        "generator": "bool",
        "fuel_hours": "number+",
        "switchgear_elevation_ft": "number+?",
        "transfer_switch_elevation_ft": "number+?",
        "load_test_interval_days": "int+?",
    },
    "water": {"on_site_storage_hours": "number+?"},
    "design_intensity": {
        "flood_elevation_ft": "number+?",
        "flood_elevation_source": "str?",
        "design_wind_mph": "number+?",
    },
    "evacuation": {
        "trigger_written": "bool",
        "trigger_text": "str?",
        "authority": "str?",
        "transport_lead_hours": "number+?",
        "priority_order_written": "bool",
        "priority_decided_on": "str?",
    },
    "transfer_agreements": [{
        "name": "str",
        "county_fips": "fips",
        "signed": "bool",
        "same_floodplain": "bool?",
        "same_grid_feeder": "bool?",
    }],
    "co_located_operators": [{"name": "str", "occupants": "int+"}],
    "evidence": "evidence",
}

#: Fields that are this repository's own bookkeeping rather than a fact read
#: off a document, so no evidence entry names them and none may.
UNEVIDENCED: tuple[str, ...] = ("blind_id",)

#: An evidence entry's own keys.
EVIDENCE_KEYS: tuple[str, ...] = ("text", "source_doc", "page")


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Evidence:
    """Where one field's value was read from: a document, and a page in it."""

    text: str
    source_doc: str
    page: int | None = None

    def to_dict(self) -> dict:
        return {"text": self.text, "source_doc": self.source_doc, "page": self.page}


@dataclasses.dataclass(frozen=True)
class Power:
    generator: bool
    fuel_hours: float
    switchgear_elevation_ft: float | None = None
    transfer_switch_elevation_ft: float | None = None
    load_test_interval_days: int | None = None


@dataclasses.dataclass(frozen=True)
class Water:
    on_site_storage_hours: float | None = None


@dataclasses.dataclass(frozen=True)
class DesignIntensity:
    """What the planner's own elevation certificate or FIRM panel says.

    `flood_elevation_ft` is the one number inject 3 of the 96-hour scenario
    needs. When it is null the scenario cannot be run for the switchgear
    question, and no county-level probability is allowed to stand in for it.
    """

    flood_elevation_ft: float | None = None
    flood_elevation_source: str | None = None
    design_wind_mph: float | None = None


@dataclasses.dataclass(frozen=True)
class Evacuation:
    trigger_written: bool
    priority_order_written: bool
    trigger_text: str | None = None
    authority: str | None = None
    transport_lead_hours: float | None = None
    priority_decided_on: str | None = None


@dataclasses.dataclass(frozen=True)
class TransferAgreement:
    """A receiving facility, and the three ways it fails when we do."""

    name: str
    county_fips: str
    signed: bool
    same_floodplain: bool | None = None
    same_grid_feeder: bool | None = None


@dataclasses.dataclass(frozen=True)
class CoLocatedOperator:
    name: str
    occupants: int


@dataclasses.dataclass(frozen=True)
class Facility:
    """One facility as its planner recorded it, every populated field evidenced."""

    slug: str
    blind_id: str
    occupancy_type: str
    county_fips: str
    census: int
    staff_on_shift: int
    power: Power
    water: Water
    design_intensity: DesignIntensity
    evacuation: Evacuation
    transfer_agreements: tuple[TransferAgreement, ...]
    co_located_operators: tuple[CoLocatedOperator, ...]
    evidence: dict[str, Evidence]
    #: The record exactly as it was read, for the citation resolver: a field
    #: path such as `power.fuel_hours` resolves against this mapping.
    raw: dict = dataclasses.field(default_factory=dict, repr=False, compare=False)

    # -- identity ----------------------------------------------------------

    @property
    def blind_label(self) -> str:
        """`FACILITY-<12 hex>`: the name a blinded reviewer sees, and only that.

        The first `BLIND_CHARS` characters of the record's own random
        `blind_id`. It is not derived from the slug, the county or anything
        else a reader could guess and confirm by hashing: the blinded page is
        the artefact that leaves the building, so its identifier must carry no
        preimage at all.
        """
        return blind_label(self.blind_id)

    # -- reading fields ----------------------------------------------------

    def get(self, path: str) -> Any:
        """The value at a dotted field path, or None when the path is absent.

        This is the same grammar a `facility` citation uses, so a rule that
        cites `power.fuel_hours` and a reader who looks it up are reading the
        one place.
        """
        node: Any = self.raw
        for part in path.split("."):
            if isinstance(node, Mapping) and part in node:
                node = node[part]
            elif isinstance(node, Sequence) and not isinstance(node, str) and part.isdigit():
                index = int(part)
                if index >= len(node):
                    return None
                node = node[index]
            else:
                return None
        return node

    def evidence_for(self, path: str) -> Evidence | None:
        """The evidence entry naming this field, or the list it sits in."""
        if path in self.evidence:
            return self.evidence[path]
        head = path.split(".", 1)[0]
        return self.evidence.get(head)

    def partner_names(self) -> tuple[str, ...]:
        """Every name a report refers to positionally, partners then co-tenants."""
        names = [a.name for a in self.transfer_agreements]
        names += [o.name for o in self.co_located_operators]
        return tuple(dict.fromkeys(names))

    def partner_labels(self) -> dict[str, str]:
        """Name -> `PARTNER-n`, assigned once from the record, in record order.

        Transfer agreements first, then co-located operators, each named once.
        Prose never carries a name: a rule writes `PARTNER-2`, the name lives
        in the claim that sentence cites, and the blinded render drops the
        claim's name while keeping its label. The label is therefore a
        property of the record, not of whichever sentence mentioned it first.
        """
        return {name: f"PARTNER-{i}"
                for i, name in enumerate(self.partner_names(), start=1)}

    def partner_label(self, name: str) -> str:
        """The label for one recorded name, or the name itself if it is unknown."""
        return self.partner_labels().get(name, name)

    def county_labels(self) -> dict[str, str]:
        """County FIPS -> `COUNTY-A`, this facility's county first.

        A blinded page carries no county either: in a rural county with one
        critical-access hospital the FIPS is the name.
        """
        codes = [self.county_fips]
        codes += [a.county_fips for a in self.transfer_agreements]
        out: dict[str, str] = {}
        for index, fips in enumerate(dict.fromkeys(codes)):
            out[fips] = f"COUNTY-{_letters(index)}"
        return out

    def document_labels(self) -> dict[str, str]:
        """Evidence source document -> `DOCUMENT-n`, in evidence-key order."""
        docs = [self.evidence[key].source_doc for key in sorted(self.evidence)]
        source = self.design_intensity.flood_elevation_source
        if source:
            docs.append(source)
        return {doc: f"DOCUMENT-{i}"
                for i, doc in enumerate(dict.fromkeys(docs), start=1)}

    def to_dict(self) -> dict:
        return json.loads(json.dumps(self.raw, sort_keys=True))

    # -- construction ------------------------------------------------------

    @classmethod
    def from_json(cls, raw: Mapping, *, where: str = "<facility>") -> "Facility":
        """Validate a parsed record and build the frozen view of it."""
        if not isinstance(raw, Mapping):
            raise FacilityError(f"{where}: expected a JSON object, got {type(raw).__name__}")
        raw = normalise_strings(raw)
        _refuse_forbidden(raw, where)
        _check_keys(raw, SCHEMA, where, "")
        data = _read_object(raw, SCHEMA, where, "")
        evidence = data["evidence"]
        _check_evidence(data, evidence, where)
        return cls(
            slug=data["slug"],
            blind_id=data["blind_id"],
            occupancy_type=data["occupancy_type"],
            county_fips=data["county_fips"],
            census=data["census"],
            staff_on_shift=data["staff_on_shift"],
            power=Power(**data["power"]),
            water=Water(**data["water"]),
            design_intensity=DesignIntensity(**data["design_intensity"]),
            evacuation=Evacuation(**data["evacuation"]),
            transfer_agreements=tuple(
                TransferAgreement(**a) for a in data["transfer_agreements"]
            ),
            co_located_operators=tuple(
                CoLocatedOperator(**o) for o in data["co_located_operators"]
            ),
            evidence={k: Evidence(**v) for k, v in evidence.items()},
            raw=json.loads(json.dumps(raw, sort_keys=True)),
        )

    @classmethod
    def from_path(cls, path: pathlib.Path | str) -> "Facility":
        path = pathlib.Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise FacilityError(f"{path}: cannot be read ({exc})") from None
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise FacilityError(f"{path}: not valid JSON ({exc})") from None
        return cls.from_json(raw, where=str(path))


def blind_label(blind_id: str) -> str:
    """`FACILITY-<12 hex>` from a record's random `blind_id`."""
    return f"FACILITY-{blind_id[:BLIND_CHARS]}"


def _letters(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA: a short label with no digits in it."""
    out = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        out = chr(ord("A") + rest) + out
    return out


def normalise_strings(node: Any) -> Any:
    """The record with every string's whitespace collapsed, recursively.

    `"Mercy  Hospital"` and `"Mercy Hospital"` are one name. Prose collapses
    whitespace when it is written, so a record that does not would leave the
    blinding keyed on a string that never appears — which is how a partner's
    real name reaches a blinded page.
    """
    if isinstance(node, str):
        return " ".join(node.split())
    if isinstance(node, Mapping):
        return {key: normalise_strings(value) for key, value in node.items()}
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        return [normalise_strings(value) for value in node]
    return node


# --------------------------------------------------------------------------- #
# Reading and refusing
# --------------------------------------------------------------------------- #


def forbidden_in(node: Any, path: str = "") -> list[tuple[str, str]]:
    """Every key that would name a place, and every string value that does.

    Two scans, because either alone is a fail-open. Keys catch a schema that
    grew a `lat`; values catch `"note": "412 Riverside Drive"`, where no key
    list could have helped. Each finding is the dotted path to the offending
    node and what was seen there.

    A bare five-digit number is **not** a finding: that is a county FIPS, the
    one geography this repository publishes.
    """
    found: list[tuple[str, str]] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.append((here, "a key that names a place"))
            found.extend(forbidden_in(value, here))
    elif isinstance(node, str):
        for what, pattern in FORBIDDEN_VALUE_PATTERNS:
            match = pattern.search(node)
            if match:
                found.append((
                    path or "<root>",
                    f"a value holding {what} ({match.group(0)!r})",
                ))
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for i, value in enumerate(node):
            found.extend(forbidden_in(value, f"{path}.{i}" if path else str(i)))
    return found


def _refuse_forbidden(node: Any, where: str, path: str = "") -> None:
    """Any forbidden key or place-naming value, anywhere, refuses the file."""
    found = forbidden_in(node, path)
    if found:
        spot, what = found[0]
        raise FacilityError(
            f"{where}: field {spot!r} is refused, {what} — {FORBIDDEN_REASON}"
        )


def _check_keys(raw: Mapping, schema: Mapping, where: str, path: str) -> None:
    """Every key present is known, and every required key is present."""
    for key in raw:
        if key not in schema:
            here = f"{path}.{key}" if path else str(key)
            raise FacilityError(
                f"{where}: unknown field {here!r}; the schema knows "
                f"{sorted(schema)} here, and a field no rule reads must not be "
                "silently dropped"
            )
    for key in schema:
        if key not in raw:
            here = f"{path}.{key}" if path else str(key)
            raise FacilityError(f"{where}: field {here!r} is missing")


def _read_object(raw: Mapping, schema: Mapping, where: str, path: str) -> dict:
    out: dict[str, Any] = {}
    for key, spec in schema.items():
        here = f"{path}.{key}" if path else str(key)
        value = raw[key]
        if isinstance(spec, Mapping):
            if not isinstance(value, Mapping):
                raise FacilityError(f"{where}: field {here!r} must be an object")
            _check_keys(value, spec, where, here)
            out[key] = _read_object(value, spec, where, here)
        elif isinstance(spec, list):
            out[key] = _read_list(value, spec[0], where, here)
        elif spec == "evidence":
            out[key] = _read_evidence(value, where, here)
        else:
            out[key] = _coerce(value, spec, where, here)
    return out


def _read_list(value: Any, spec: Mapping, where: str, path: str) -> list[dict]:
    if not isinstance(value, list):
        raise FacilityError(f"{where}: field {path!r} must be a list")
    out = []
    for i, item in enumerate(value):
        here = f"{path}.{i}"
        if not isinstance(item, Mapping):
            raise FacilityError(f"{where}: {here!r} must be an object")
        _check_keys(item, spec, where, here)
        out.append(_read_object(item, spec, where, here))
    return out


def _read_evidence(value: Any, where: str, path: str) -> dict[str, dict]:
    if not isinstance(value, Mapping):
        raise FacilityError(f"{where}: field {path!r} must be an object keyed by field path")
    out: dict[str, dict] = {}
    for key, entry in value.items():
        here = f"{path}.{key}"
        if not isinstance(entry, Mapping):
            raise FacilityError(f"{where}: evidence entry {here!r} must be an object")
        for name in entry:
            if name not in EVIDENCE_KEYS:
                raise FacilityError(
                    f"{where}: unknown field {here}.{name!r}; an evidence entry has "
                    f"{list(EVIDENCE_KEYS)}"
                )
        for name in ("text", "source_doc"):
            if not isinstance(entry.get(name), str) or not entry[name].strip():
                raise FacilityError(
                    f"{where}: evidence entry {key!r} needs a non-empty {name!r}"
                )
        page = entry.get("page")
        if page is not None and (isinstance(page, bool) or not isinstance(page, int)):
            raise FacilityError(f"{where}: evidence entry {key!r} has a non-integer page")
        out[key] = {"text": entry["text"], "source_doc": entry["source_doc"], "page": page}
    return out


def _coerce(value: Any, spec: str, where: str, path: str) -> Any:
    optional = spec.endswith("?")
    kind = spec[:-1] if optional else spec
    non_negative = kind.endswith("+")
    kind = kind[:-1] if non_negative else kind
    if kind == "blind_id":
        # Before the null check, so an absent one says how to make one too.
        if not (isinstance(value, str) and _BLIND_ID_RE.match(value)):
            raise FacilityError(
                f"{where}: field {path!r} must be 32 lowercase hex characters, "
                f"got {value!r}. It is the record's own blinding identifier and it "
                f"must be random, not derived from anything a reader could guess. "
                f"Generate one once, keep it in this file, and never change it:\n"
                f"    {BLIND_ID_COMMAND}"
            )
        return value
    if value is None:
        if optional:
            return None
        raise FacilityError(f"{where}: field {path!r} must not be null")
    if kind == "slug":
        if not isinstance(value, str) or not value.strip():
            raise FacilityError(f"{where}: field {path!r} must be a non-empty string")
        if not all(c.isalnum() or c in "-_" for c in value):
            raise FacilityError(
                f"{where}: field {path!r} must be a slug (letters, digits, '-' and '_')"
            )
        return value
    if kind == "occupancy":
        if value not in OCCUPANCY_TYPES:
            raise FacilityError(
                f"{where}: field {path!r} must be one of {list(OCCUPANCY_TYPES)}, "
                f"got {value!r}"
            )
        return value
    if kind == "fips":
        if not (isinstance(value, str) and len(value) == 5 and value.isdigit()):
            raise FacilityError(
                f"{where}: field {path!r} must be a five-digit county FIPS string, "
                f"got {value!r}"
            )
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise FacilityError(f"{where}: field {path!r} must be true or false")
        return value
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise FacilityError(f"{where}: field {path!r} must be a whole number")
        return _check_sign(value, non_negative, where, path)
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FacilityError(f"{where}: field {path!r} must be a number")
        return _check_sign(value, non_negative, where, path)
    if kind == "str":
        if not isinstance(value, str) or not value.strip():
            raise FacilityError(f"{where}: field {path!r} must be a non-empty string")
        return value
    raise FacilityError(f"{where}: field {path!r} has an unknown schema type {spec!r}")


def _check_sign(value: Any, non_negative: bool, where: str, path: str) -> Any:
    """Hours, elevations, occupants and lead times are never negative.

    A record with -5 hours of transport notice does not describe a facility
    that needs five hours less warning; it describes a typo, and a rule that
    reasons over it would report a break at a negative hour.
    """
    if non_negative and value < 0:
        raise FacilityError(
            f"{where}: field {path!r} must not be negative, got {value!r}"
        )
    return value


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #


def populated_paths(data: Mapping, schema: Mapping = SCHEMA, path: str = "") -> list[str]:
    """Every populated leaf field path, in schema order. `evidence` is not one."""
    out: list[str] = []
    for key, spec in schema.items():
        if spec == "evidence" or (not path and key in UNEVIDENCED):
            continue
        here = f"{path}.{key}" if path else str(key)
        value = data[key]
        if isinstance(spec, Mapping):
            out.extend(populated_paths(value, spec, here))
        elif isinstance(spec, list):
            for i, item in enumerate(value):
                out.extend(populated_paths(item, spec[0], f"{here}.{i}"))
        elif value is not None:
            out.append(here)
    return out


def _check_evidence(data: Mapping, evidence: Mapping[str, dict], where: str) -> None:
    """Every populated leaf is named by an evidence key; every key names a leaf.

    A leaf inside a list may be named either by its indexed path
    (`transfer_agreements.0.signed`) or by the list itself
    (`transfer_agreements`), because one signed agreement is one document.
    """
    paths = populated_paths(data)
    known = set(paths) | {p.split(".", 1)[0] for p in paths}
    for path in paths:
        head = path.split(".", 1)[0]
        listed = isinstance(SCHEMA.get(head), list)
        if path in evidence or (listed and head in evidence):
            continue
        raise FacilityError(
            f"{where}: field {path!r} is populated but no evidence entry names it; "
            "every number a rule may cite must come from a document (add "
            f'"{path}": {{"text": ..., "source_doc": ..., "page": ...}} under "evidence")'
        )
    for key in evidence:
        if key not in known:
            raise FacilityError(
                f"{where}: evidence key {key!r} names no populated field; the key is "
                "a field path such as 'power.fuel_hours'"
            )


__all__ = [
    "BLIND_CHARS",
    "BLIND_ID_COMMAND",
    "BLIND_LABEL_RE",
    "EVIDENCE_KEYS",
    "FORBIDDEN_KEYS",
    "FORBIDDEN_REASON",
    "FORBIDDEN_VALUE_PATTERNS",
    "OCCUPANCY_TYPES",
    "SCHEMA",
    "UNEVIDENCED",
    "CoLocatedOperator",
    "DesignIntensity",
    "Evacuation",
    "Evidence",
    "Facility",
    "FacilityError",
    "Power",
    "TransferAgreement",
    "Water",
    "blind_label",
    "forbidden_in",
    "normalise_strings",
    "populated_paths",
]
