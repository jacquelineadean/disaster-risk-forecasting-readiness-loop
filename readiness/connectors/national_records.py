"""A partner's national disaster record: ground truth that never leaves their hands.

Report §6, Phase 4: "EM-DAT and national records for ground truth". A national
disaster management agency's own archive is almost always better than EM-DAT
for a single country — more events, finer geography, real damage figures — and
it is almost always shared under terms that forbid redistribution.

So this connector is built around one rule: **the bytes are never committed and
never copied.** What enters the repository is the sha256 in the contract
(`ground_truth.sha256`, a hashed criterion) and the matching record in
`snapshots/manifest.json`. The file itself stays where the partner put it,
under `snapshots/records/`, which is git-ignored and which the site packer is
forbidden to pack (`tools/build_site.py`). Anyone with the same file can
reproduce the panel exactly; nobody without it can obtain the file from here.

The schema is deliberately the smallest thing that supports the forecast unit:

    event_id,start_date,region_id,hazard,deaths,injured,damage_usd,source

with `start_date` as `YYYY-MM-DD`, `region_id` a geoBoundaries `shapeID` at the
contract's admin level, and `hazard` a value this hazard's
`config.HAZARD_CATEGORIES[...]["national_records"]` lists (case, spaces and
hyphens are normalised; anything else is skipped and counted, never guessed).
An optional first comment line

    # record_start_year: 2005

records where the archive itself begins, which `readiness register` reads into
the contract so that no split can start before the record does.
"""

from __future__ import annotations

import csv
import io
import pathlib
import re
from dataclasses import dataclass, field

from readiness.config import HAZARD_CATEGORIES, normalise_hazard_value
from readiness.connectors.base import (
    ConnectorError,
    Manifest,
    SourceRecord,
    sha256_bytes,
    utc_now,
)
from readiness.contracts import Contract
from readiness.harness.labels import RecordEvent

SOURCE = "Partner national disaster records (supplied per contract)"
LICENSE = "partner data; not redistributed"
KEY_PREFIX = "records/"

COLUMNS: tuple[str, ...] = (
    "event_id",
    "start_date",
    "region_id",
    "hazard",
    "deaths",
    "injured",
    "damage_usd",
    "source",
)

_START_YEAR_RE = re.compile(r"^\s*#\s*record_start_year\s*:\s*(\d{4})\s*$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def manifest_key(country: str, path: pathlib.Path | str) -> str:
    """`records/<CC>/<basename>` — the key the file's hash is pinned under."""
    return f"{KEY_PREFIX}{country.upper()}/{pathlib.Path(path).name}"


@dataclass(frozen=True)
class Records:
    """What one records file yielded, and what it did not.

    `n_skipped_hazard` is the honest half: a partner file covers every hazard
    they record, so most rows belong to another contract. Counting them is how
    a thin panel is explained rather than mistaken for an absence of events.
    """

    events: list[RecordEvent] = field(default_factory=list)
    n_rows: int = 0
    n_skipped_hazard: int = 0
    skipped_hazards: tuple[str, ...] = ()
    record_start_year: int | None = None

    def summary(self) -> str:
        skipped = (
            f"; {self.n_skipped_hazard:,} rows of other hazards "
            f"({', '.join(self.skipped_hazards[:6])})"
            if self.n_skipped_hazard
            else ""
        )
        return f"{len(self.events):,} events of {self.n_rows:,} rows{skipped}"


def read_start_year(data: bytes) -> int | None:
    """The `# record_start_year: YYYY` header, if the file carries one."""
    for line in data.decode("utf-8-sig", "replace").splitlines():
        if not line.strip():
            continue
        if not line.lstrip().startswith("#"):
            return None
        match = _START_YEAR_RE.match(line)
        if match:
            return int(match.group(1))
    return None


def parse(data: bytes, contract: Contract) -> Records:
    """Pure: partner CSV bytes -> the contract's events, and what was skipped.

    Every row is read; a row of another hazard is counted and dropped, and a
    malformed date, region or number raises rather than becoming a zero. A
    silent zero here is a mislabelled region-period, which is the one class of
    bug the whole harness exists to avoid.
    """
    wanted = set(HAZARD_CATEGORIES.get(contract.hazard, {}).get("national_records", ()))
    if not wanted:
        raise ConnectorError(
            f"hazard {contract.hazard!r} has no national-records mapping in "
            "config.HAZARD_CATEGORIES, so no partner value can be matched to it"
        )
    text = data.decode("utf-8-sig", "replace")
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    reader = csv.DictReader(io.StringIO(body))
    missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ConnectorError(
            f"partner records schema drift: expected column(s) {missing} are absent "
            f"(header: {reader.fieldnames}). The agreed schema is {list(COLUMNS)}."
        )
    events: list[RecordEvent] = []
    skipped: dict[str, int] = {}
    n_rows = 0
    for i, row in enumerate(reader, start=2):
        if not any((row.get(c) or "").strip() for c in COLUMNS):
            continue
        n_rows += 1
        hazard = normalise_hazard_value(row.get("hazard") or "")
        if hazard not in wanted:
            skipped[hazard or "(blank)"] = skipped.get(hazard or "(blank)", 0) + 1
            continue
        year, month = _year_month(row.get("start_date"), i)
        region = (row.get("region_id") or "").strip()
        if not region:
            raise ConnectorError(f"partner records row {i}: region_id is empty")
        events.append(
            RecordEvent(
                event_id=(row.get("event_id") or "").strip() or f"row{i}",
                year=year,
                month=month,
                hazard=hazard,
                region_ids=(region,),
                injuries=_number(row.get("injured"), "injured", i, cast=int),
                deaths=_number(row.get("deaths"), "deaths", i, cast=int),
                damage_property_usd=_number(row.get("damage_usd"), "damage_usd", i),
            )
        )
    return Records(
        events=events,
        n_rows=n_rows,
        n_skipped_hazard=sum(skipped.values()),
        skipped_hazards=tuple(sorted(skipped)),
        record_start_year=read_start_year(data),
    )


def load(
    path: pathlib.Path,
    contract: Contract,
    manifest: Manifest | None = None,
) -> Records:
    """Read the records file the contract pins, and pin it again for this run.

    Refuses, naming both hashes, when the file on disk is not the file the
    contract was registered against. That refusal is the whole point of the
    hash being a criterion: a partner sending a corrected export is a new
    contract with a new digest and a fresh ledger, not the same experiment
    series quietly rebuilt on different labels.

    Nothing is copied. The record added to the manifest carries the hash, the
    basename and the licence, and no bytes.
    """
    path = pathlib.Path(path)
    if not path.exists():
        raise ConnectorError(
            f"contract {contract.name!r} is scored against {path.name}, which is not "
            f"at {path}. Partner records are never committed; place the file the "
            f"contract was registered against (sha256:"
            f"{contract.ground_truth.get('sha256', '')[:16]}...) there and re-run."
        )
    data = path.read_bytes()
    found = sha256_bytes(data)
    expected = str(contract.ground_truth.get("sha256", ""))
    if found != expected:
        raise ConnectorError(
            f"{path} is not the file contract {contract.name!r} was registered "
            f"against: on disk sha256:{found}, contract sha256:{expected}. The hash "
            "is a hashed criterion, so different bytes are a different contract — "
            "register a new one rather than rebuilding this one's panel."
        )
    records = parse(data, contract)
    if manifest is not None:
        manifest.add(
            manifest_key(contract.country, path),
            SourceRecord(
                source=SOURCE,
                url="",  # supplied by the partner; there is nothing to fetch
                sha256=found,
                bytes=len(data),
                fetched_at=utc_now(),
                license=LICENSE,
                notes=(
                    f"{records.summary()}; bytes never committed or redistributed"
                ),
            ),
        )
    return records


def _year_month(raw: str | None, line: int) -> tuple[int, int]:
    match = _DATE_RE.match((raw or "").strip())
    if not match:
        raise ConnectorError(
            f"partner records row {line}: start_date {raw!r} is not YYYY-MM-DD"
        )
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise ConnectorError(f"partner records row {line}: month {month} is not 1-12")
    return year, month


def _number(raw: str | None, column: str, line: int, cast=float) -> float:
    text = (raw or "").strip().replace(",", "")
    if not text:
        return cast(0)
    try:
        value = cast(float(text))
    except ValueError:
        raise ConnectorError(
            f"partner records row {line}: {column} {raw!r} is not a number"
        ) from None
    if value < 0:
        raise ConnectorError(
            f"partner records row {line}: {column} is negative ({raw!r})"
        )
    return value
