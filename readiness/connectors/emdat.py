"""EM-DAT: the global ground truth of last resort, read with the standard library.

Report §6, Phase 4: "EM-DAT and national records for ground truth". Where no
partner archive exists, CRED's International Disaster Database is the only
record that covers every country on comparable terms. It is also the weaker of
the two, and this connector is built to make that visible rather than to hide
it:

* **It is sparse.** EM-DAT's entry criteria (ten deaths, a hundred affected, a
  declaration, an appeal) mean a district can have real flooding for twenty
  years and no row. A dense panel built from it records *unreported* as zero,
  which is the same failure Storm Events has in the US, one order of magnitude
  larger. `config.GROUND_TRUTH_SOURCES` refuses splits before 2000 for that
  reason, and the diagnostics count every row that does not land.
* **Its geography is names, not codes.** The `Admin Units` column carries
  free-text admin names (with EM-DAT's own numeric codes, which are not
  geoBoundaries ids). Mapping them to `shapeID`s is a judgement about places,
  so it is a *committed crosswalk* a person wrote —
  `snapshots/records/<cc>_emdat_regions.csv`, `emdat_name,shape_id` — never a
  fuzzy match made at run time. Unmapped units are counted, and a large count
  is a finding about the crosswalk, not a rounding error.
* **Its bytes are not ours to publish.** EM-DAT is free for research with
  registration and may not be redistributed, so the export is pinned by hash
  exactly like a partner file: `emdat/<CC>/<basename>`, bytes never committed,
  never packed into the browser sandbox.

The export is an `.xlsx`, which is a zip of XML — so it is read with `zipfile`
and `xml.etree`, and the project keeps its standard-library-only rule (it also
has to run in Pyodide). Columns are found by header name, so a reordering
upstream is harmless and a *renaming* is loud.
"""

from __future__ import annotations

import ast
import csv
import io
import json
import pathlib
import zipfile
from dataclasses import dataclass, field
from typing import Iterator, Mapping
from xml.etree import ElementTree

from readiness.config import HAZARD_CATEGORIES
from readiness.connectors.base import (
    ConnectorError,
    Manifest,
    SourceRecord,
    sha256_bytes,
    utc_now,
)
from readiness.contracts import Contract
from readiness.harness.labels import RecordEvent

SOURCE = "EM-DAT, CRED / UCLouvain (registered export)"
LICENSE = "free for research, registration required; redistribution not permitted"
KEY_PREFIX = "emdat/"

#: Columns read from the export, by header name.
COLUMNS: tuple[str, ...] = (
    "DisNo.",
    "Disaster Type",
    "Disaster Subtype",
    "Start Year",
    "Start Month",
    "Country",
    "Admin Units",
    "Total Deaths",
    "No. Injured",
    "Total Damage ('000 US$)",
)

#: The crosswalk's two columns: an EM-DAT admin name, and the geoBoundaries
#: shapeID a person decided it means.
CROSSWALK_COLUMNS: tuple[str, ...] = ("emdat_name", "shape_id")

#: EM-DAT reports damage in thousands of US dollars.
DAMAGE_SCALE = 1_000.0

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def manifest_key(country: str, path: pathlib.Path | str) -> str:
    """`emdat/<CC>/<basename>` — the key the export's hash is pinned under."""
    return f"{KEY_PREFIX}{country.upper()}/{pathlib.Path(path).name}"


def crosswalk_path(snapshot_dir: pathlib.Path, country: str) -> pathlib.Path:
    """Where the committed admin-name crosswalk for one country lives."""
    return snapshot_dir / "records" / f"{country.lower()}_emdat_regions.csv"


@dataclass(frozen=True)
class Records:
    """What one export yielded, and everything it did not."""

    events: list[RecordEvent] = field(default_factory=list)
    n_rows: int = 0
    n_skipped_hazard: int = 0
    n_unmapped_units: int = 0
    n_rows_unmapped: int = 0
    unmapped_names: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()

    def summary(self) -> str:
        parts = [f"{len(self.events):,} events of {self.n_rows:,} rows"]
        if self.n_skipped_hazard:
            parts.append(f"{self.n_skipped_hazard:,} rows of other hazards")
        if self.n_unmapped_units:
            parts.append(
                f"{self.n_unmapped_units:,} admin units not in the crosswalk "
                f"({', '.join(self.unmapped_names[:6])})"
            )
        if self.n_rows_unmapped:
            parts.append(f"{self.n_rows_unmapped:,} rows landed nowhere")
        return "; ".join(parts)


# ---------------------------------------------------------------------------
# xlsx, with zipfile and xml.etree
# ---------------------------------------------------------------------------


def _column_index(ref: str) -> int:
    """`"AB12"` -> 27: the zero-based column of a cell reference."""
    index = 0
    for ch in ref:
        if not ch.isalpha():
            break
        index = index * 26 + (ord(ch.upper()) - 64)
    return index - 1


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    """`xl/sharedStrings.xml` as a list; absent when every cell is inline."""
    try:
        blob = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(blob)
    return ["".join(t.text or "" for t in si.iter(f"{_NS}t")) for si in root]


def _sheet_name(zf: zipfile.ZipFile) -> str:
    if "xl/worksheets/sheet1.xml" in zf.namelist():
        return "xl/worksheets/sheet1.xml"
    sheets = sorted(
        n for n in zf.namelist() if n.startswith("xl/worksheets/") and n.endswith(".xml")
    )
    if not sheets:
        raise ConnectorError("EM-DAT export holds no worksheet XML")
    return sheets[0]


def rows(data: bytes) -> Iterator[list[str]]:
    """Every row of the export's first sheet, as strings, gaps filled with "".

    Shared strings, inline strings and numbers are all handled; anything else
    (a formula cache, a boolean) comes through as the raw stored value, which
    is what a header-name lookup needs and all this reads.
    """
    if not data.startswith(b"PK"):
        raise ConnectorError(
            "EM-DAT export is not an .xlsx (no zip header). Export the table as "
            "Excel; the .csv export omits the Admin Units column."
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ConnectorError(f"EM-DAT export is not a readable zip ({exc})") from None
    with zf:
        shared = _shared_strings(zf)
        sheet = ElementTree.fromstring(zf.read(_sheet_name(zf)))
        for row in sheet.iter(f"{_NS}row"):
            cells: dict[int, str] = {}
            for i, cell in enumerate(row.findall(f"{_NS}c")):
                ref = cell.get("r") or ""
                index = _column_index(ref) if ref and ref[0].isalpha() else i
                cells[index] = _cell_text(cell, shared)
            if not cells:
                yield []
                continue
            yield [cells.get(i, "") for i in range(max(cells) + 1)]


def _cell_text(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "s":
        node = cell.find(f"{_NS}v")
        if node is None or not (node.text or "").strip():
            return ""
        try:
            return shared[int(node.text)]
        except (ValueError, IndexError):
            raise ConnectorError(
                f"EM-DAT export references shared string {node.text!r}, which the "
                "sharedStrings table does not hold"
            ) from None
    if kind == "inlineStr":
        node = cell.find(f"{_NS}is")
        if node is None:
            return ""
        return "".join(t.text or "" for t in node.iter(f"{_NS}t"))
    node = cell.find(f"{_NS}v")
    return (node.text or "") if node is not None else ""


# ---------------------------------------------------------------------------
# The crosswalk
# ---------------------------------------------------------------------------


def parse_crosswalk(data: bytes) -> dict[str, str]:
    """`emdat_name,shape_id` bytes -> a lookup keyed by normalised admin name."""
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig", "replace")))
    missing = [c for c in CROSSWALK_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ConnectorError(
            f"EM-DAT region crosswalk is missing column(s) {missing}; it must be "
            f"{list(CROSSWALK_COLUMNS)} — an EM-DAT admin name, and the "
            "geoBoundaries shapeID a person decided it means"
        )
    out: dict[str, str] = {}
    for row in reader:
        name = _norm_name(row.get("emdat_name"))
        shape = (row.get("shape_id") or "").strip()
        if not name or not shape:
            continue
        if name in out and out[name] != shape:
            raise ConnectorError(
                f"EM-DAT region crosswalk maps {row['emdat_name']!r} to both "
                f"{out[name]!r} and {shape!r}; one name, one region"
            )
        out[name] = shape
    if not out:
        raise ConnectorError("EM-DAT region crosswalk parsed to zero rows")
    return out


def load_crosswalk(path: pathlib.Path) -> dict[str, str]:
    if not path.exists():
        raise ConnectorError(
            f"no EM-DAT region crosswalk at {path}. EM-DAT names admin units in "
            "prose; mapping them to geoBoundaries ids is a judgement about places "
            "and is written down by a person, not guessed at run time."
        )
    return parse_crosswalk(path.read_bytes())


def _norm_name(raw: object) -> str:
    return " ".join(str(raw or "").strip().lower().split())


def admin_names(raw: str, admin_level: str) -> tuple[list[str], int]:
    """The admin names one `Admin Units` cell carries at a level, and how many it had.

    EM-DAT writes the cell as a JSON-ish list of objects
    (`[{"adm1_code":..,"adm1_name":".."}, ..]`); some exports use single
    quotes, which `json` will not read and `ast.literal_eval` will. A cell that
    is neither is reported as zero units, which the caller counts.
    """
    text = (raw or "").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return [], 0
    parsed = None
    for reader in (json.loads, ast.literal_eval):
        try:
            parsed = reader(text)
            break
        except (ValueError, SyntaxError):
            continue
    if not isinstance(parsed, list):
        return [], 0
    key = f"{admin_level.lower()}_name"
    names: list[str] = []
    for unit in parsed:
        if not isinstance(unit, Mapping):
            continue
        value = str(unit.get(key) or "").strip()
        if value:
            names.append(value)
    return names, len(parsed)


# ---------------------------------------------------------------------------
# Reading the export
# ---------------------------------------------------------------------------


def parse(data: bytes, contract: Contract, crosswalk: Mapping[str, str]) -> Records:
    """Pure: export bytes plus a crosswalk -> the contract's events and the misses."""
    wanted = set(HAZARD_CATEGORIES.get(contract.hazard, {}).get("emdat", ()))
    if not wanted:
        raise ConnectorError(
            f"hazard {contract.hazard!r} has no EM-DAT mapping in "
            "config.HAZARD_CATEGORIES, so no disaster type can be matched to it"
        )
    level = contract.admin_level
    stream = rows(data)
    header = next((r for r in stream if any(c.strip() for c in r)), None)
    if header is None:
        raise ConnectorError("EM-DAT export has no header row")
    index = {name.strip(): i for i, name in enumerate(header)}
    missing = [c for c in COLUMNS if c not in index]
    if missing:
        raise ConnectorError(
            f"EM-DAT schema drift: expected column(s) {missing} are absent "
            f"(header: {[h for h in header if h.strip()]})"
        )

    events: list[RecordEvent] = []
    unmapped: dict[str, int] = {}
    countries: dict[str, int] = {}
    n_rows = n_skipped = n_rows_unmapped = 0
    for row in stream:
        def cell(name: str) -> str:
            i = index[name]
            return row[i].strip() if i < len(row) else ""

        if not any(c.strip() for c in row):
            continue
        n_rows += 1
        country = cell("Country")
        if country:
            countries[country] = countries.get(country, 0) + 1
        if cell("Disaster Type") not in wanted and cell("Disaster Subtype") not in wanted:
            n_skipped += 1
            continue
        names, _n_units = admin_names(cell("Admin Units"), level)
        region_ids: list[str] = []
        for name in names:
            shape = crosswalk.get(_norm_name(name))
            if shape is None:
                unmapped[name] = unmapped.get(name, 0) + 1
            elif shape not in region_ids:
                region_ids.append(shape)
        if not region_ids:
            # No admin unit this contract's level can place. Counted, never
            # spread over the whole country: a national row marked everywhere
            # would manufacture positives in districts that saw nothing.
            n_rows_unmapped += 1
            continue
        year = _int(cell("Start Year"), "Start Year", cell("DisNo."))
        month = _int(cell("Start Month"), "Start Month", cell("DisNo."), default=0)
        if not 1 <= month <= 12:
            # EM-DAT leaves the month empty for slow-onset events (drought,
            # most of all). January is the convention CRED's own annual
            # aggregates use; it is recorded here rather than assumed silently.
            month = 1
        events.append(
            RecordEvent(
                event_id=cell("DisNo.") or f"row{n_rows}",
                year=year,
                month=month,
                hazard=contract.hazard,
                region_ids=tuple(region_ids),
                injuries=_int(cell("No. Injured"), "No. Injured", cell("DisNo."), 0),
                deaths=_int(cell("Total Deaths"), "Total Deaths", cell("DisNo."), 0),
                damage_property_usd=_float(
                    cell("Total Damage ('000 US$)"), cell("DisNo.")
                )
                * DAMAGE_SCALE,
            )
        )
    return Records(
        events=events,
        n_rows=n_rows,
        n_skipped_hazard=n_skipped,
        n_unmapped_units=sum(unmapped.values()),
        n_rows_unmapped=n_rows_unmapped,
        unmapped_names=tuple(sorted(unmapped)),
        countries=tuple(sorted(countries)),
    )


def load(
    path: pathlib.Path,
    contract: Contract,
    manifest: Manifest | None = None,
    *,
    crosswalk: Mapping[str, str] | None = None,
    snapshot_dir: pathlib.Path | None = None,
) -> Records:
    """Read the export the contract pins, with the country's committed crosswalk.

    Refuses, naming both hashes, when the file on disk is not the file the
    contract was registered against — the same rule as the partner records, for
    the same reason. Only the hash is pinned; the export's bytes are never
    copied, committed or packed.
    """
    path = pathlib.Path(path)
    if not path.exists():
        raise ConnectorError(
            f"contract {contract.name!r} is scored against {path.name}, which is not "
            f"at {path}. EM-DAT exports are not redistributable and are never "
            "committed; download the export the contract was registered against "
            f"(sha256:{str(contract.ground_truth.get('sha256', ''))[:16]}...) and "
            "place it there."
        )
    data = path.read_bytes()
    found = sha256_bytes(data)
    expected = str(contract.ground_truth.get("sha256", ""))
    if found != expected:
        raise ConnectorError(
            f"{path} is not the export contract {contract.name!r} was registered "
            f"against: on disk sha256:{found}, contract sha256:{expected}. EM-DAT "
            "revises entries between releases, so different bytes are a different "
            "contract — register a new one rather than rebuilding this one's panel."
        )
    if crosswalk is None:
        root = snapshot_dir if snapshot_dir is not None else path.parent.parent
        crosswalk = load_crosswalk(crosswalk_path(root, contract.country))
    records = parse(data, contract, crosswalk)
    if manifest is not None:
        manifest.add(
            manifest_key(contract.country, path),
            SourceRecord(
                source=SOURCE,
                url="https://public.emdat.be/",
                sha256=found,
                bytes=len(data),
                fetched_at=utc_now(),
                license=LICENSE,
                notes=f"{records.summary()}; export never committed or redistributed",
            ),
        )
    return records


def _int(raw: str, column: str, where: str, default: int | None = None) -> int:
    text = (raw or "").strip().replace(",", "")
    if not text:
        if default is not None:
            return default
        raise ConnectorError(f"EM-DAT row {where or '?'}: {column} is empty")
    try:
        return int(float(text))
    except ValueError:
        raise ConnectorError(
            f"EM-DAT row {where or '?'}: {column} {raw!r} is not a number"
        ) from None


def _float(raw: str, where: str) -> float:
    text = (raw or "").strip().replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        raise ConnectorError(
            f"EM-DAT row {where or '?'}: damage {raw!r} is not a number"
        ) from None
