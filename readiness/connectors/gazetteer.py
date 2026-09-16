"""Census 2020 Gazetteer: where each county is, and how much of it is water.

The first terrain features (report §6: "feature construction from NRI,
historical frequencies, terrain and precipitation reanalysis") need a point
per county to ask a reanalysis about, and a static description of the county
that no event can change. The Gazetteer gives both: an internal point
(latitude, longitude) and the land and water areas. It is physical geometry,
so its feature source declares no `derived_through` year and sits on the
harness's timeless allow-list (`TIMELESS_STATIC_SOURCES`).

The file is a zip holding one tab-delimited text file. Public domain.
"""

from __future__ import annotations

import csv
import io
import math
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
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2020_Gazetteer/"
    "2020_Gaz_counties_national.zip"
)
LICENSE = "US Government work — public domain (17 U.S.C. §105); cite the Census Bureau"
SOURCE = "US Census Bureau 2020 Gazetteer, counties"
MANIFEST_KEY = "census/gazetteer_counties2020"
CACHE_NAME = "gazetteer_counties2020.zip"

#: Columns the parser reads; the file has more (ANSICODE, NAME, the square-mile
#: areas), which are ignored so a reordering upstream is harmless.
_COLUMNS = ("GEOID", "ALAND", "AWATER", "INTPTLAT", "INTPTLONG")


@dataclass(frozen=True)
class Centroid:
    """One county's internal point and its land and water areas, in square metres."""

    fips: str
    lat: float
    lon: float
    land_m2: float
    water_m2: float

    @property
    def water_share(self) -> float:
        total = self.land_m2 + self.water_m2
        return self.water_m2 / total if total > 0 else NAN


def load(
    snapshot_dir: pathlib.Path,
    manifest: Manifest,
    *,
    refresh: bool = False,
    allow_fetch: bool = True,
) -> dict[str, Centroid]:
    """Fetch (or reuse) the Gazetteer zip and return a centroid per county FIPS.

    Same rule as `census.load`: the cached file is reused only when its bytes
    hash to the manifest record; anything else is unpinned and re-fetched, or
    an error when fetching is not allowed.
    """
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
            ),
        )
    return parse(data)


def _text_member(data: bytes) -> bytes:
    """The one text file inside the zip, or the bytes themselves if not zipped."""
    if not data.startswith(b"PK"):
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
        if len(names) != 1:
            raise ConnectorError(
                f"Gazetteer zip should hold one .txt file, found {names}"
            )
        return zf.read(names[0])


def parse(data: bytes) -> dict[str, Centroid]:
    """Pure: zip or tab-delimited bytes -> centroids keyed by 5-digit FIPS.

    The Census file pads its last header with trailing spaces, so every header
    is stripped before it is matched. A missing required column is schema
    drift and an error, never a silently empty table.
    """
    text = _text_member(data).decode("utf-8", "replace")
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        raise ConnectorError("Gazetteer file is empty") from None
    missing = [c for c in _COLUMNS if c not in header]
    if missing:
        raise ConnectorError(
            f"Gazetteer schema drift: expected columns {missing} are absent "
            f"(header: {header})"
        )
    index = {c: header.index(c) for c in _COLUMNS}
    out: dict[str, Centroid] = {}
    for row in reader:
        if len(row) < len(header):
            continue
        fips = row[index["GEOID"]].strip()
        if not (fips.isdigit() and len(fips) == 5):
            continue
        out[fips] = Centroid(
            fips=fips,
            lat=float(row[index["INTPTLAT"]]),
            lon=float(row[index["INTPTLONG"]]),
            land_m2=float(row[index["ALAND"]]),
            water_m2=float(row[index["AWATER"]]),
        )
    if not out:
        raise ConnectorError("Gazetteer file parsed to zero counties")
    return out


class GazetteerSource:
    """Static geometry per county: water share, position, land area."""

    name = "gazetteer"
    kind = "static"
    manifest_keys = (MANIFEST_KEY,)
    derived_through = None
    global_coverage = False

    def __init__(self, centroids: Mapping[str, Centroid]) -> None:
        self._centroids = dict(centroids)

    def series(self, region: str, variable: str) -> Series | None:
        return None

    def static(self, region: str) -> dict[str, float] | None:
        c = self._centroids.get(region)
        if c is None:
            return None
        return {
            "water_share": c.water_share,
            "lat": c.lat,
            "lon": c.lon,
            "land_km2": c.land_m2 / 1e6,
        }


def source(centroids: Mapping[str, Centroid]) -> GazetteerSource:
    return GazetteerSource(centroids)


def points(centroids: Mapping[str, Centroid]) -> dict[str, tuple[float, float]]:
    """`fips -> (lat, lon)`, the shape the reanalysis connector asks for."""
    return {
        fips: (c.lat, c.lon)
        for fips, c in sorted(centroids.items())
        if not (math.isnan(c.lat) or math.isnan(c.lon))
    }
