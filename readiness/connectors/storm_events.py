"""NOAA Storm Events Database connector — the ground truth (report §3A, source [10]).

The official NWS record: 48 event types at county/zone level from January 1950,
with injuries, fatalities and property/crop damage per event. This project reads
it from 1996 onward only; report §7 warns that collection practice changed over
the decades and that many event types standardised in 1996.

The raw year files are ~10 MB gzipped each and the full 1996-2025 pull is a few
hundred megabytes. By default each year is downloaded, checksummed, filtered to
the state of interest, written out as a compact extract, and the raw bytes are
discarded. The checksum in the manifest is what pins the experiment; pass
`keep_raw=True` if you want to mirror the originals (report §7 argues you
should, given the Billion-Dollar Disasters retirement).
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import pathlib
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

from readiness.connectors.base import (
    ConnectorError,
    Manifest,
    SourceRecord,
    fetch,
    sha256_bytes,
    utc_now,
)
from readiness.harness.labels import StormEvent, county_fips, parse_damage

INDEX_URL = "https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/"
LICENSE = "US Government work — public domain (17 U.S.C. §105); cite NOAA NCEI"

#: Files are named with both a data year and a *creation* date that changes when
#: NCEI reprocesses. The index has to be read to learn the current creation date.
_FILE_RE = re.compile(
    r"StormEvents_details-ftp_v1\.0_d(?P<year>\d{4})_c(?P<created>\d{8})\.csv\.gz"
)

_FIELDS = (
    "EVENT_ID",
    "YEAR",
    "BEGIN_YEARMONTH",
    "EVENT_TYPE",
    "STATE",
    "STATE_FIPS",
    "CZ_TYPE",
    "CZ_FIPS",
    "INJURIES_DIRECT",
    "INJURIES_INDIRECT",
    "DEATHS_DIRECT",
    "DEATHS_INDIRECT",
    "DAMAGE_PROPERTY",
    "DAMAGE_CROPS",
)


def discover_files(years: Iterable[int]) -> dict[int, str]:
    """Read the NCEI directory index and resolve one URL per requested year.

    If a year is reprocessed upstream the creation stamp changes; resolving from
    the index rather than hardcoding URLs means the manifest records that the
    bytes moved instead of silently 404-ing.
    """
    wanted = set(years)
    # Use the shared default session rather than a bespoke one: the connection
    # opened here is the same one the year-file pulls reuse, so the whole
    # snapshot costs a single connection setup.
    index = fetch(INDEX_URL).decode("utf-8", "replace")
    found: dict[int, tuple[str, str]] = {}
    for m in _FILE_RE.finditer(index):
        year = int(m.group("year"))
        if year not in wanted:
            continue
        created = m.group("created")
        # Several creation stamps can coexist; take the most recent.
        if year not in found or created > found[year][0]:
            found[year] = (created, m.group(0))

    missing = sorted(wanted - set(found))
    if missing:
        raise ConnectorError(
            f"NCEI index has no details file for year(s) {missing}. "
            "Storm Events runs ~120 days behind; a very recent year may not "
            "exist yet."
        )
    return {y: INDEX_URL + name for y, (_, name) in sorted(found.items())}


def _parse_year(data: bytes, state: str) -> list[dict]:
    """Decompress one year file and keep only rows for `state`."""
    rows: list[dict] = []
    with gzip.open(io.BytesIO(data), "rt", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        missing = [f for f in _FIELDS if f not in (reader.fieldnames or [])]
        if missing:
            raise ConnectorError(
                f"Storm Events schema drift: expected columns {missing} are "
                "absent. A data-steward review is required before this year "
                "can be used (report §5, data steward subagent)."
            )
        want = int(state)
        for row in reader:
            raw = (row.get("STATE_FIPS") or "").strip()
            # Older files are inconsistent about zero-padding ("1" vs "01"),
            # so compare numerically rather than as strings.
            if not raw.isdigit() or int(raw) != want:
                continue
            rows.append({k: (row.get(k) or "").strip() for k in _FIELDS})
    return rows


def snapshot(
    years: Iterable[int],
    state_fips: str,
    snapshot_dir: pathlib.Path,
    manifest: Manifest,
    *,
    keep_raw: bool = False,
    refresh: bool = False,
    workers: int = 1,
    progress: Callable[[str], None] = lambda _msg: None,
) -> pathlib.Path:
    """Pull the requested years, pin their checksums, and write a state extract.

    Returns the path to the extract (JSONL, one row per retained event).
    """
    years = sorted(set(years))
    extract_dir = snapshot_dir / "storm_events"
    extract_dir.mkdir(parents=True, exist_ok=True)

    # A cached extract with no manifest record is *unpinned*: the bytes are on
    # disk but nothing records which upstream file they came from or what it
    # hashed to, so any experiment run against it could not be reproduced. Treat
    # that as missing and re-fetch, rather than quietly scoring against data of
    # unknown provenance. (This is the state left behind when a download
    # completes but a later parsing step raises before the manifest is saved.)
    needed = [
        y
        for y in years
        if refresh
        or not (extract_dir / f"{state_fips}_{y}.jsonl").exists()
        or f"noaa/storm_events/{y}" not in manifest.records
    ]

    if needed:
        progress(f"resolving {len(needed)} year file(s) from the NCEI index")
        urls = discover_files(needed)

        def pull(year: int) -> tuple[int, SourceRecord, int]:
            url = urls[year]
            data = fetch(url)
            rows = _parse_year(data, state_fips)
            out = extract_dir / f"{state_fips}_{year}.jsonl"
            with out.open("w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            if keep_raw:
                (snapshot_dir / "storm_events_raw").mkdir(parents=True, exist_ok=True)
                (snapshot_dir / "storm_events_raw" / f"{year}.csv.gz").write_bytes(data)
            return (
                year,
                SourceRecord(
                    source="NOAA NCEI Storm Events Database",
                    url=url,
                    sha256=sha256_bytes(data),
                    bytes=len(data),
                    fetched_at=utc_now(),
                    license=LICENSE,
                    notes=f"{len(rows)} rows retained for state FIPS {state_fips}",
                ),
                len(rows),
            )

        # Default is sequential, and deliberately so: every file comes from the
        # same host, so one keep-alive connection beats N parallel cold ones —
        # connection setup, not transfer, is the expensive part of this pull.
        # Raise `workers` only if you are fetching from several hosts at once.
        if workers <= 1:
            results = (pull(year) for year in needed)
        else:
            pool = ThreadPoolExecutor(max_workers=workers)
            results = pool.map(pull, needed)

        for year, record, n in results:
            manifest.add(f"noaa/storm_events/{year}", record)
            progress(f"  {year}  {record.bytes / 1e6:>5.1f} MB  {n:>5} state rows")
        if workers > 1:
            pool.shutdown()

    combined = extract_dir / f"{state_fips}_extract.jsonl"
    with combined.open("w", encoding="utf-8") as out:
        for year in years:
            part = extract_dir / f"{state_fips}_{year}.jsonl"
            if not part.exists():
                raise ConnectorError(f"missing extract for {year}: {part}")
            out.write(part.read_text(encoding="utf-8"))
    return combined


def load_events(extract: pathlib.Path) -> list[StormEvent]:
    """Read a state extract into typed events."""
    events: list[StormEvent] = []
    with extract.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            cz_fips = r["CZ_FIPS"]
            if not cz_fips or not cz_fips.isdigit():
                continue
            ym = r["BEGIN_YEARMONTH"]
            events.append(
                StormEvent(
                    event_id=r["EVENT_ID"],
                    year=int(r["YEAR"]) if r["YEAR"] else int(ym[:4]),
                    month=int(ym[4:6]),
                    event_type=r["EVENT_TYPE"],
                    state_fips=r["STATE_FIPS"],
                    cz_type=r["CZ_TYPE"],
                    cz_fips=cz_fips,
                    county_fips=county_fips(r["STATE_FIPS"], cz_fips),
                    injuries=_int(r["INJURIES_DIRECT"]) + _int(r["INJURIES_INDIRECT"]),
                    deaths=_int(r["DEATHS_DIRECT"]) + _int(r["DEATHS_INDIRECT"]),
                    damage_property_usd=parse_damage(r["DAMAGE_PROPERTY"]),
                    damage_crops_usd=parse_damage(r["DAMAGE_CROPS"]),
                )
            )
    return events


def _int(raw: str) -> int:
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else 0
