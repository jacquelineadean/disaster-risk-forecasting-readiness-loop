"""Region universe, from the Census national county file.

Without an authoritative county list the panel is built from whatever counties
happen to appear in the event table, which silently drops every county that
never had a recorded event — deflating the denominator and inflating the base
rate. This connector supplies the denominator, for any state or for the whole
country.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Sequence

from readiness.connectors.base import (
    Manifest,
    SourceRecord,
    fetch,
    pinned_bytes,
    sha256_bytes,
    utc_now,
)

COUNTY_URL = (
    "https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt"
)
LICENSE = "US Government work — public domain (17 U.S.C. §105)"


@dataclass(frozen=True)
class County:
    fips: str          # 5-digit state+county
    state: str         # 2-letter postal
    name: str

    def __str__(self) -> str:
        return f"{self.name} ({self.fips})"


def load(
    snapshot_dir: pathlib.Path,
    manifest: Manifest,
    *,
    refresh: bool = False,
    allow_fetch: bool = True,
) -> list[County]:
    """Fetch (or reuse) the county file and return every US county.

    The cached file is reused only when its bytes hash to the manifest record
    — the same rule `storm_events.snapshot` applies to kept raw files. Bytes
    with no record, or that no longer match it, are unpinned and re-fetched
    rather than used as data of unknown provenance; with `allow_fetch=False`
    that is an error instead.
    """
    cache = snapshot_dir / "national_county2020.txt"
    key = "census/national_county2020"
    data = None
    if not refresh:
        data = pinned_bytes(cache, manifest.records.get(key), allow_fetch=allow_fetch)
    if data is None:
        data = fetch(COUNTY_URL)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
        manifest.add(
            key,
            SourceRecord(
                source="US Census Bureau",
                url=COUNTY_URL,
                sha256=sha256_bytes(data),
                bytes=len(data),
                fetched_at=utc_now(),
                license=LICENSE,
            ),
        )
    return parse(data)


def parse(data: bytes) -> list[County]:
    counties: list[County] = []
    for i, line in enumerate(data.decode("utf-8", "replace").splitlines()):
        if i == 0 or not line.strip():
            continue  # header
        parts = line.split("|")
        if len(parts) < 5:
            continue
        state, statefp, countyfp, _ns, name = parts[:5]
        counties.append(
            County(fips=f"{statefp}{countyfp}", state=state, name=name)
        )
    if not counties:
        raise ValueError("census county file parsed to zero rows")
    return counties


def for_states(counties: list[County], states: Sequence[str]) -> list[County]:
    """Filter to the requested states, sorted by FIPS for deterministic panels.

    An empty `states` means every region in the file — the national scope.
    An unknown state is an error, not an empty panel.
    """
    wanted = {s.upper() for s in states}
    known = {c.state for c in counties}
    unknown = sorted(wanted - known)
    if unknown:
        raise KeyError(
            f"no counties found for state(s) {unknown}; known: {sorted(known)}"
        )
    subset = sorted(
        (c for c in counties if not wanted or c.state in wanted), key=lambda c: c.fips
    )
    if not subset:
        raise KeyError("the county universe is empty")
    return subset
