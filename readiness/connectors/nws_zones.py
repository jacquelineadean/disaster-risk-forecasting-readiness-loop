"""NWS forecast-zone to county crosswalk.

Storm Events codes most broad-scale hazards — heat, tropical cyclones, winter
storms, wildfire, coastal flooding — against National Weather Service public
forecast zones rather than counties. A zone-coded row carries a state and a
zone number, not a county FIPS, so it cannot be joined to a county universe
without this file.

The National Weather Service publishes the correlation as a pipe-delimited
text file (one row per zone-county pair) alongside its zone shapefiles:

    STATE|ZONE|CWA|NAME|STATE_ZONE|COUNTY|FIPS|TIME_ZONE|FE_AREA|LAT|LON

The file is re-issued a few times a year under a name that encodes its
effective date (`bp16ap26.dbx` is 16 April 2026), and zones are occasionally
renumbered or merged. Two consequences this connector does not hide:

* the edition in use is discovered from the NWS index, pinned in the manifest
  like every other source, and named on the panel diagnostics; and
* a historical event whose zone is no longer in the current edition cannot be
  mapped. The label builder counts such events as *unmapped* rather than
  guessing, and `readiness panel` reports the count.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field

from readiness.connectors.base import (
    ConnectorError,
    Manifest,
    SourceRecord,
    fetch,
    pinned_bytes,
    sha256_bytes,
    utc_now,
)

INDEX_URL = "https://www.weather.gov/gis/ZoneCounty"
BASE_URL = "https://www.weather.gov"
LICENSE = "US Government work — public domain (17 U.S.C. §105); cite NOAA NWS"
MANIFEST_KEY = "nws/zone_county"

_EDITION_RE = re.compile(
    r"/source/gis/Shapefiles/County/(bp(\d{2})([a-z]{2})(\d{2})\.dbx)"
)
_MONTHS = {
    "ja": 1, "fe": 2, "mr": 3, "ap": 4, "my": 5, "jn": 6,
    "jl": 7, "au": 8, "se": 9, "oc": 10, "no": 11, "de": 12,
}


def edition_date(name: str) -> tuple[int, int, int]:
    """`bp16ap26.dbx` -> (2026, 4, 16). Unknown month codes sort first."""
    m = re.match(r"bp(\d{2})([a-z]{2})(\d{2})\.dbx$", name)
    if not m:
        return (0, 0, 0)
    day, mon, year = int(m.group(1)), _MONTHS.get(m.group(2), 0), 2000 + int(m.group(3))
    return (year, mon, day)


def discover_current() -> str:
    """Read the NWS index and return the URL of the most recent edition."""
    index = fetch(INDEX_URL).decode("utf-8", "replace")
    found = {m.group(1): m.group(0) for m in _EDITION_RE.finditer(index)}
    if not found:
        raise ConnectorError(
            f"no zone-county correlation file found at {INDEX_URL}; the NWS may "
            "have moved it. Record the new location and slot in a mirror."
        )
    latest = max(found, key=edition_date)
    return BASE_URL + found[latest]


@dataclass(frozen=True)
class Crosswalk:
    """Zone -> counties, keyed by (2-digit state FIPS, zone number)."""

    edition: str
    _map: dict[tuple[str, int], tuple[str, ...]] = field(default_factory=dict, repr=False)

    def counties_for(self, state_fips: str, zone: str) -> tuple[str, ...]:
        """Counties in a zone, or an empty tuple if the zone is not in this edition.

        Storm Events writes zone numbers unpadded ("7") while the NWS file pads
        them ("007"); both are compared numerically.
        """
        if not str(zone).strip().isdigit() or not str(state_fips).strip().isdigit():
            return ()
        return self._map.get((f"{int(state_fips):02d}", int(zone)), ())

    def __len__(self) -> int:
        return len(self._map)

    @property
    def n_states(self) -> int:
        return len({state for state, _zone in self._map})


def parse(data: bytes, edition: str = "unknown") -> Crosswalk:
    mapping: dict[tuple[str, int], list[str]] = {}
    for line in data.decode("utf-8", "replace").splitlines():
        parts = line.split("|")
        if len(parts) < 7:
            continue
        zone, fips = parts[1].strip(), parts[6].strip()
        if not zone.isdigit() or not (fips.isdigit() and len(fips) == 5):
            continue
        key = (fips[:2], int(zone))
        counties = mapping.setdefault(key, [])
        if fips not in counties:
            counties.append(fips)
    if not mapping:
        raise ConnectorError("zone-county file parsed to zero zone-county pairs")
    return Crosswalk(edition, {k: tuple(sorted(v)) for k, v in mapping.items()})


def load(
    snapshot_dir: pathlib.Path,
    manifest: Manifest,
    *,
    refresh: bool = False,
    allow_fetch: bool = True,
) -> Crosswalk:
    """Fetch (or reuse) the current correlation file and return the crosswalk.

    Same rule as every other source: the cached file is reused only when its
    bytes hash to the manifest record. Bytes with no record, or that no longer
    match it, are unpinned and re-fetched rather than trusted; with
    `allow_fetch=False` that is an error instead.
    """
    cache = snapshot_dir / "zone_county.dbx"
    record = manifest.records.get(MANIFEST_KEY)
    if refresh and not allow_fetch:
        raise ConnectorError("refresh requested for the zone crosswalk but fetching is not allowed")
    data = None
    if not refresh:
        data = pinned_bytes(cache, record, allow_fetch=allow_fetch)
    if data is None:
        url = discover_current()
        data = fetch(url)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
        record = SourceRecord(
            source="NOAA NWS zone-county correlation file",
            url=url,
            sha256=sha256_bytes(data),
            bytes=len(data),
            fetched_at=utc_now(),
            license=LICENSE,
            notes=f"edition {url.rsplit('/', 1)[1]}",
        )
        manifest.add(MANIFEST_KEY, record)
    return parse(data, edition=record.url.rsplit("/", 1)[1])
