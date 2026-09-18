"""FEMA National Risk Index, county table: the benchmark the firewall refuses.

Report §6 names NRI among the Phase 1 features, and the licence manifest calls
its scores "relative rankings, not probabilities — usable as a prior and a
benchmark, never as a forecast". This connector ships it, pins it and hands the
harness a static source that declares the last year of data the index encodes.
Under every current contract (validate from 2016) `admit()` refuses it: an
index published in 2025 was built with the floods of 2016–2023 in it, and a
layer that has seen the holdout years cannot be a feature for them. The
refusal is the firewall's real-data demonstration (plan §2), so the backtest
report shows NRI as a benchmark row stamped INADMISSIBLE rather than quietly
leaving it out.

`VINTAGE` is a reviewed constant: the harness trusts `derived_through` (it
cannot derive a vintage from a CSV), so the number lives here, in one place,
and is pinned into the manifest record's notes.
"""

from __future__ import annotations

import csv
import io
import pathlib
import zipfile
from dataclasses import dataclass
from typing import Mapping

from readiness.connectors.base import (
    ConnectorError,
    Manifest,
    SourceRecord,
    fetch,
    pinned_bytes,
    sha256_bytes,
    utc_now,
)
from readiness.harness.features import NAN, Series

URL = (
    "https://hazards.fema.gov/nri/Content/StaticDocuments/DataDownload/"
    "NRI_Table_Counties/NRI_Table_Counties.zip"
)
LICENSE = "US Government work — public domain (17 U.S.C. §105); cite FEMA"
SOURCE = "FEMA National Risk Index, county table"
KEY_PREFIX = "fema/nri_counties_"


@dataclass(frozen=True)
class NriVintage:
    """Which release this is, and the last calendar year of data it encodes."""

    version: str
    derived_through: int
    citation: str


#: Reviewed when the pinned file changes. v1.20 (December 2025) builds its
#: expected annual loss from event histories running through 2023.
VINTAGE = NriVintage(
    version="1.20",
    derived_through=2023,
    citation="FEMA National Risk Index v1.20 (December 2025)",
)
MANIFEST_KEY = f"{KEY_PREFIX}{VINTAGE.version}"
CACHE_NAME = f"NRI_Table_Counties_{VINTAGE.version}.zip"

#: The scores handed to the harness. The table has hundreds of columns; these
#: four are the composite ones the roadmap names.
COLUMNS = ("EAL_SCORE", "RISK_SCORE", "SOVI_SCORE", "RESL_SCORE")
_FIPS = "STCOFIPS"

Table = dict[str, dict[str, float]]


def load(
    snapshot_dir: pathlib.Path,
    manifest: Manifest,
    *,
    refresh: bool = False,
    allow_fetch: bool = True,
) -> Table:
    """Fetch (or reuse) the county table and return the scores per county FIPS."""
    cache = snapshot_dir / CACHE_NAME
    data = None
    if not refresh:
        record = manifest.records.get(MANIFEST_KEY)
        data = pinned_bytes(cache, record, allow_fetch=allow_fetch)
    if data is None:
        data = fetch(URL)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
        manifest.add(
            MANIFEST_KEY,
            SourceRecord(
                source=SOURCE,
                url=URL,
                sha256=sha256_bytes(data),
                bytes=len(data),
                fetched_at=utc_now(),
                license=LICENSE,
                notes=f"derived_through={VINTAGE.derived_through}; {VINTAGE.citation}",
            ),
        )
    return parse(data)


def _csv_member(data: bytes) -> bytes:
    """The county CSV inside the zip, or the bytes themselves if not zipped."""
    if not data.startswith(b"PK"):
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith("nri_table_counties.csv")]
        if len(names) != 1:
            raise ConnectorError(
                f"NRI zip should hold NRI_Table_Counties.csv, found {zf.namelist()}"
            )
        return zf.read(names[0])


def _score(raw: str) -> float:
    raw = (raw or "").strip()
    try:
        return float(raw)
    except ValueError:
        return NAN  # blank or "Insufficient Data": missing, never zero


def parse(data: bytes) -> Table:
    """Pure: zip or CSV bytes -> `{fips: {score column: value}}`.

    Some releases write the FIPS unpadded; it is zero-padded to five digits
    so the join to the Census universe is by value, not by formatting.
    """
    text = _csv_member(data).decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    missing = [c for c in (_FIPS, *COLUMNS) if c not in header]
    if missing:
        raise ConnectorError(
            f"NRI schema drift: expected columns {missing} are absent. A data-steward "
            "review is required before this release can be used."
        )
    table: Table = {}
    for row in reader:
        raw = (row.get(_FIPS) or "").strip()
        if not raw.isdigit():
            continue
        table[f"{int(raw):05d}"] = {c: _score(row.get(c, "")) for c in COLUMNS}
    if not table:
        raise ConnectorError("NRI county table parsed to zero rows")
    return table


class NriSource:
    """Static composite scores per county, dated to the vintage's last year."""

    name = "nri"
    kind = "static"
    manifest_keys = (MANIFEST_KEY,)
    derived_through = VINTAGE.derived_through
    global_coverage = False

    def __init__(self, table: Mapping[str, Mapping[str, float]]) -> None:
        self._table = {k: dict(v) for k, v in table.items()}

    def series(self, region: str, variable: str) -> Series | None:
        return None

    def static(self, region: str) -> dict[str, float] | None:
        row = self._table.get(region)
        return dict(row) if row is not None else None


def source(table: Mapping[str, Mapping[str, float]]) -> NriSource:
    return NriSource(table)
