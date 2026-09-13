"""NOAA Storm Events Database connector — the ground truth (report §3A, source [10]).

The official NWS record: 48 event types at county/zone level from January 1950,
with injuries, fatalities and property/crop damage per event. This project reads
it from 1996 onward only; report §7 warns that collection practice changed over
the decades and that many event types standardised in 1996.

The raw year files are ~10 MB gzipped each and a full 1996–2025 pull is a few
hundred megabytes. Each year file is national, so one download serves every
state and every hazard: it is downloaded, checksummed, split into one compact
extract per state (or one national extract), and the raw bytes are discarded
unless `keep_raw` is set. The checksum in the manifest is what pins the
experiment. A kept raw file whose checksum still matches the manifest is
re-used to cut a new state's extract without another download — report §7
argues you should keep them, given the Billion-Dollar Disasters retirement.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import pathlib
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Sequence

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

#: The scope label used for a national (every state) extract.
NATIONAL = "all"

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


def parse_year(data: bytes, states: Iterable[str] | None) -> dict[str, list[dict]]:
    """Decompress one national year file and group rows by 2-digit state FIPS.

    `states` limits the output to those states; `None` keeps every row. Rows
    are returned under their zero-padded state FIPS.
    """
    wanted = None if states is None else {int(s) for s in states}
    rows: dict[str, list[dict]] = defaultdict(list)
    with gzip.open(io.BytesIO(data), "rt", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        missing = [f for f in _FIELDS if f not in (reader.fieldnames or [])]
        if missing:
            raise ConnectorError(
                f"Storm Events schema drift: expected columns {missing} are "
                "absent. A data-steward review is required before this year "
                "can be used (report §5, data steward subagent)."
            )
        for row in reader:
            raw = (row.get("STATE_FIPS") or "").strip()
            # Older files are inconsistent about zero-padding ("1" vs "01"),
            # so compare numerically rather than as strings.
            if not raw.isdigit():
                continue
            fips = int(raw)
            if wanted is not None and fips not in wanted:
                continue
            rows[f"{fips:02d}"].append({k: (row.get(k) or "").strip() for k in _FIELDS})
    return dict(rows)


def scope_label(states: Sequence[str] | None) -> str:
    """`"22"`, `"22+28"`, or `"all"` — the prefix extracts are filed under."""
    if states is None:
        return NATIONAL
    return "+".join(sorted({f"{int(s):02d}" for s in states}))


def _write_extract(path: pathlib.Path, rows: Iterable[dict]) -> int:
    n = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            n += 1
    return n


def snapshot(
    years: Iterable[int],
    states: Sequence[str] | None,
    snapshot_dir: pathlib.Path,
    manifest: Manifest,
    *,
    keep_raw: bool = False,
    refresh: bool = False,
    workers: int = 1,
    progress: Callable[[str], None] = lambda _msg: None,
) -> pathlib.Path:
    """Pull the requested years, pin their checksums, and write the scope's extract.

    `states` is a sequence of 2-digit state FIPS codes, or `None` for every
    state. Returns the path to the combined extract (JSONL, one row per event).
    """
    years = sorted(set(years))
    extract_dir = snapshot_dir / "storm_events"
    raw_dir = snapshot_dir / "storm_events_raw"
    extract_dir.mkdir(parents=True, exist_ok=True)
    parts = [NATIONAL] if states is None else sorted({f"{int(s):02d}" for s in states})

    def part_path(part: str, year: int) -> pathlib.Path:
        return extract_dir / f"{part}_{year}.jsonl"

    # A cached extract with no manifest record is *unpinned*: the bytes are on
    # disk but nothing records which upstream file they came from or what it
    # hashed to, so any experiment run against it could not be reproduced. Treat
    # that as missing and re-fetch, rather than quietly scoring against data of
    # unknown provenance. (This is the state left behind when a download
    # completes but a later parsing step raises before the manifest is saved.)
    needed: dict[int, list[str]] = {}
    for year in years:
        pinned = f"noaa/storm_events/{year}" in manifest.records
        missing = [
            p for p in parts if refresh or not pinned or not part_path(p, year).exists()
        ]
        if missing:
            needed[year] = missing

    if needed:
        urls: dict[int, str] = {}

        def obtain(year: int) -> tuple[bytes, str | None]:
            """Raw bytes for one year: a kept, still-pinned raw file, else a download."""
            raw = raw_dir / f"{year}.csv.gz"
            record = manifest.records.get(f"noaa/storm_events/{year}")
            if not refresh and raw.exists() and record is not None:
                data = raw.read_bytes()
                if sha256_bytes(data) == record.sha256:
                    return data, None
            if year not in urls:
                urls.update(discover_files([y for y in needed if y not in urls]))
            return fetch(urls[year]), urls[year]

        def pull(year: int) -> tuple[int, SourceRecord | None, dict[str, int]]:
            data, url = obtain(year)
            # A fresh download may carry reprocessed bytes. Every extract that
            # already exists for this year was cut from the *previous* bytes
            # and would otherwise stay stale under a manifest record that now
            # says otherwise — so re-cut all of them, not just the ones asked
            # for. One extra pass over data that is already in memory.
            parts_to_write = list(needed[year])
            if url is not None:
                for existing in extract_dir.glob(f"*_{year}.jsonl"):
                    part = existing.name[: -len(f"_{year}.jsonl")]
                    if part not in parts_to_write:
                        parts_to_write.append(part)
            wanted = None if NATIONAL in parts_to_write else parts_to_write
            grouped = parse_year(data, wanted)
            counts: dict[str, int] = {}
            for part in parts_to_write:
                if part == NATIONAL:
                    rows = (r for fips in sorted(grouped) for r in grouped[fips])
                else:
                    rows = iter(grouped.get(part, []))
                counts[part] = _write_extract(part_path(part, year), rows)
            if keep_raw:
                raw_dir.mkdir(parents=True, exist_ok=True)
                (raw_dir / f"{year}.csv.gz").write_bytes(data)
            record = None
            if url is not None:
                total = sum(len(v) for v in grouped.values()) if states is None else None
                record = SourceRecord(
                    source="NOAA NCEI Storm Events Database",
                    url=url,
                    sha256=sha256_bytes(data),
                    bytes=len(data),
                    fetched_at=utc_now(),
                    license=LICENSE,
                    notes=f"{total:,} events nationally" if total is not None else "",
                )
            return year, record, counts

        progress(f"resolving {len(needed)} year file(s)")
        # Default is sequential, and deliberately so: every file comes from the
        # same host, so one keep-alive connection beats N parallel cold ones —
        # connection setup, not transfer, is the expensive part of this pull.
        # Raise `workers` only if you are fetching from several hosts at once.
        if workers <= 1:
            results = (pull(year) for year in sorted(needed))
        else:
            pool = ThreadPoolExecutor(max_workers=workers)
            results = pool.map(pull, sorted(needed))

        for year, record, counts in results:
            if record is not None:
                manifest.add(f"noaa/storm_events/{year}", record)
                size = f"{record.bytes / 1e6:>5.1f} MB"
            else:
                size = "  (raw)"
            summary = ", ".join(f"{p}: {n}" for p, n in counts.items())
            progress(f"  {year}  {size}  rows {summary}")
        if workers > 1:
            pool.shutdown()

    combined = extract_dir / f"{scope_label(states)}_extract.jsonl"
    with combined.open("w", encoding="utf-8") as out:
        for year in years:
            for part in parts:
                path = part_path(part, year)
                if not path.exists():
                    raise ConnectorError(f"missing extract for {year}: {path}")
                out.write(path.read_text(encoding="utf-8"))
    return combined


def load_events(extract: pathlib.Path) -> list[StormEvent]:
    """Read an extract into typed events."""
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
