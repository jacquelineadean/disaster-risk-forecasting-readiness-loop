"""geoBoundaries: the region universe everywhere the Census is not.

Phase 4 swaps the US-specific layers for their global counterparts and reruns
the same loop; the architecture does not change, the connectors do. This is the
counterpart of `census.py`: without an authoritative list of ADM1 or ADM2 units
the panel is built from whatever regions happen to appear in the records, which
drops every district that never had a recorded event — deflating the
denominator and inflating the base rate. That failure mode is worse abroad than
at home, because under-reporting is exactly what a sparse national record does.

geoBoundaries (William & Mary geoLab) publishes gbOpen, a CC BY 4.0 release of
open administrative boundaries for every country, versioned as a whole: the
same district can change `shapeID` between releases, so the contract names the
release (`regions.release`, e.g. `"gbOpen 6.0.0"`) and it is a hashed criterion
like everything else.

**The centroid is a lookup point, not a legal centre.** `centroid_lat` and
`centroid_lon` are the arithmetic mean of the outer ring's vertices — not an
area centroid, not a point-on-surface, and emphatically not an administrative
seat. It exists to ask Open-Meteo for one ERA5 series per region, which is the
same thing the Gazetteer's internal point does in the US. For a long thin or
crescent-shaped district it can fall outside the polygon. Nothing in this
project ever publishes it as a location, and nothing should.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from readiness.connectors.base import (
    ConnectorError,
    Manifest,
    SourceRecord,
    fetch,
    pinned_bytes,
    sha256_bytes,
    utc_now,
)

SOURCE = "geoBoundaries gbOpen administrative boundaries"
LICENSE = "CC BY 4.0 (geoBoundaries, William & Mary geoLab)"
KEY_PREFIX = "geoboundaries/"

#: Releases are GitHub tags on wmgeolab/geoBoundaries, and the release data is
#: served from the tagged tree, so naming the tag is what pins the edition.
URL_TEMPLATE = (
    "https://raw.githubusercontent.com/wmgeolab/geoBoundaries/{ref}/"
    "releaseData/gbOpen/{country}/{level}/geoBoundaries-{country}-{level}.geojson"
)

#: Point the connector at a mirror without editing code — same shape as
#: `URL_TEMPLATE`, same fields, and `https` only. A release that is only
#: reachable through a mirror is still pinned by hash, so the substitution is
#: visible in the manifest rather than hidden in an environment; and a
#: contract that names `regions.sha256` refuses bytes that are not the ones it
#: was registered against, whoever served them.
URL_ENV = "READINESS_GEOBOUNDARIES_URL"

#: The largest a boundary file may be. A country's ADM2 gbOpen file is single
#: -digit megabytes; `parse` holds the whole GeoJSON and every vertex in
#: memory, so an unbounded one is a denial of service rather than a universe.
MAX_BYTES = 512 * 1024 * 1024

#: What `readiness register` writes when no release is named.
DEFAULT_RELEASE = "gbOpen 6.0.0"

ADMIN_LEVELS: tuple[str, ...] = ("ADM1", "ADM2")

#: `\Z`, not `$`: `$` also matches before a final newline, and a release with
#: one would build a URL with a newline in it. `contracts.RELEASE_RE` is the
#: same pattern, checked at registration; a test ties the two together.
_RELEASE_RE = re.compile(r"^gbOpen[ @](?P<ref>[0-9][0-9A-Za-z._-]*)\Z")


@dataclass(frozen=True)
class Region:
    """One administrative unit: its stable id, its name, and a lookup point.

    `id` and `name` are the two things the data plane requires of any region
    (`census.County` exposes the same pair), so a panel, a card and a brief
    read identically whichever universe produced them.
    """

    id: str
    name: str
    centroid_lat: float
    centroid_lon: float

    def __str__(self) -> str:
        return f"{self.name} ({self.id})"


def manifest_key(country: str, admin_level: str) -> str:
    return f"{KEY_PREFIX}{country.upper()}/{admin_level.upper()}"


def cache_path(
    snapshot_dir: pathlib.Path, country: str, admin_level: str
) -> pathlib.Path:
    return (
        snapshot_dir
        / "geoboundaries"
        / f"{country.upper()}_{admin_level.upper()}.geojson"
    )


def release_ref(release: str) -> str:
    """`"gbOpen 6.0.0"` -> `"6.0.0"`, the tag the release data is served from."""
    match = _RELEASE_RE.match(str(release).strip())
    if not match:
        raise ConnectorError(
            f"geoBoundaries release {release!r} is not readable; it must be "
            f'"gbOpen <version>", e.g. "{DEFAULT_RELEASE}". The release is a '
            "contract criterion because boundary ids change between releases."
        )
    return match.group("ref")


def url_for(country: str, admin_level: str, release: str) -> str:
    template = os.environ.get(URL_ENV) or URL_TEMPLATE
    if not template.lower().startswith("https://"):
        raise ConnectorError(
            f"{URL_ENV}={template!r} is not an https URL. The region universe "
            "defines which places exist in a pilot's panel; it is not fetched over "
            "a transport anybody on the path can rewrite."
        )
    return template.format(
        ref=release_ref(release),
        country=country.upper(),
        level=admin_level.upper(),
    )


def load(
    country: str,
    admin_level: str,
    release: str,
    snapshot_dir: pathlib.Path,
    manifest: Manifest,
    *,
    refresh: bool = False,
    allow_fetch: bool = True,
    expected_sha256: str | None = None,
) -> list[Region]:
    """Fetch (or reuse) one country's boundaries at one admin level.

    Same rule as `census.load` and `gazetteer.load`: a cached file is reused
    only when its bytes hash to the manifest record. Bytes with no record, or
    that no longer match one, are data of unknown provenance and are
    re-fetched; with `allow_fetch=False` that is an error naming the file.

    `expected_sha256` is the contract's optional `regions.sha256` criterion. It
    is checked exactly as `national_records.load` checks the record: the
    release string names an edition, and a mirror can serve anything under that
    name, so a pilot that pins the bytes gets the same refusal a partner record
    gets when the bytes are not the ones the contract was registered against.
    The parsed features are also checked against the level and the country that
    were asked for, so a wrong file is refused rather than admitted as the
    region universe.
    """
    level = admin_level.upper()
    if level not in ADMIN_LEVELS:
        raise ConnectorError(
            f"geoBoundaries admin level {admin_level!r} is not one of "
            f"{list(ADMIN_LEVELS)}"
        )
    cache = cache_path(snapshot_dir, country, level)
    key = manifest_key(country, level)
    url = url_for(country, level, release)
    if refresh and not allow_fetch:
        raise ConnectorError(
            f"refresh requested for {key} but fetching is not allowed"
        )
    data = None
    if not refresh:
        data = pinned_bytes(cache, manifest.records.get(key), allow_fetch=allow_fetch)
    if data is None:
        data = fetch(url)
        if len(data) > MAX_BYTES:
            raise ConnectorError(
                f"geoBoundaries file for {key} is {len(data):,} bytes, over the "
                f"{MAX_BYTES:,}-byte cap; it was not parsed"
            )
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
        manifest.add(
            key,
            SourceRecord(
                source=SOURCE,
                url=url,
                sha256=sha256_bytes(data),
                bytes=len(data),
                fetched_at=utc_now(),
                license=LICENSE,
                notes=f"{release}, {country.upper()} {level}",
            ),
        )
    if len(data) > MAX_BYTES:
        raise ConnectorError(
            f"geoBoundaries file for {key} is {len(data):,} bytes, over the "
            f"{MAX_BYTES:,}-byte cap; it was not parsed"
        )
    if expected_sha256:
        found = sha256_bytes(data)
        if found != expected_sha256:
            raise ConnectorError(
                f"the geoBoundaries file for {key} is not the one this contract "
                f"was registered against: on disk sha256:{found}, contract "
                f"sha256:{expected_sha256}. The release names an edition and a "
                "mirror can serve anything under it, so the hash is what decides."
            )
    return parse(data, expect_level=level, expect_group=country.upper())


def parse(
    data: bytes,
    *,
    expect_level: str | None = None,
    expect_group: str | None = None,
) -> list[Region]:
    """Pure: gbOpen GeoJSON bytes -> regions sorted by id.

    Every feature must carry `shapeID` and `shapeName`; a release that stops
    doing so is schema drift and an error, not a silently empty universe.

    `expect_level` and `expect_group` are what the caller *asked* for.
    `ADMIN_LEVELS` was only ever enforced on the request; gbOpen's features
    carry `shapeType` and `shapeGroup`, which say which level and which country
    the file actually holds, and they are checked here when present (an older
    release that omits them is not broken by this). Without the check an ADM3
    file admitted as a pilot's ADM1 universe publishes sub-county names in the
    site's region lists, and a mirror template with no `{level}` field serves
    one file for both levels with nothing to notice it.
    """
    try:
        blob = json.loads(data.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise ConnectorError(f"geoBoundaries file is not valid JSON ({exc})") from None
    if not isinstance(blob, Mapping) or "features" not in blob:
        found = sorted(blob) if isinstance(blob, Mapping) else type(blob).__name__
        raise ConnectorError(
            "geoBoundaries file is not a GeoJSON FeatureCollection "
            f"(top level: {found})"
        )
    regions: dict[str, Region] = {}
    for feature in blob.get("features") or []:
        props = feature.get("properties") or {}
        shape_id = str(props.get("shapeID") or "").strip()
        shape_name = str(props.get("shapeName") or "").strip()
        if not shape_id:
            raise ConnectorError(
                "geoBoundaries schema drift: a feature carries no shapeID, so its "
                f"region has no stable identity (properties: {sorted(props)})"
            )
        if shape_id in regions:
            raise ConnectorError(
                f"geoBoundaries file lists shapeID {shape_id!r} twice; the region "
                "universe must be one row per region"
            )
        if expect_level:
            found_level = str(props.get("shapeType") or "").strip().upper()
            if found_level and found_level != expect_level.upper():
                raise ConnectorError(
                    f"geoBoundaries file holds {found_level} shapes but "
                    f"{expect_level.upper()} was requested (feature {shape_id!r}). "
                    "The region universe defines what a panel's rows are; a level "
                    "below the county equivalent must not become one."
                )
        if expect_group:
            found_group = str(props.get("shapeGroup") or "").strip().upper()
            if found_group and found_group != expect_group.upper():
                raise ConnectorError(
                    f"geoBoundaries file holds shapes for {found_group} but "
                    f"{expect_group.upper()} was requested (feature {shape_id!r})"
                )
        lat, lon = _centroid(feature.get("geometry") or {})
        regions[shape_id] = Region(
            id=shape_id,
            name=shape_name or shape_id,
            centroid_lat=lat,
            centroid_lon=lon,
        )
    if not regions:
        raise ConnectorError("geoBoundaries file parsed to zero regions")
    return [regions[k] for k in sorted(regions)]


def _outer_rings(geometry: Mapping) -> list[Sequence]:
    """Every outer ring of a Polygon or MultiPolygon, holes excluded."""
    kind = str(geometry.get("type") or "")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return [coords[0]] if coords else []
    if kind == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    raise ConnectorError(
        f"geoBoundaries geometry type {kind!r} is not a Polygon or MultiPolygon"
    )


def _centroid(geometry: Mapping) -> tuple[float, float]:
    """The vertex mean of the outer ring(s) — a lookup point, not a legal centre.

    GeoJSON rings repeat their first vertex to close; the duplicate is dropped
    so one corner of a rectangle does not count twice. A MultiPolygon averages
    every outer ring's vertices together, which puts an archipelago's point
    roughly among its islands rather than on the largest one.
    """
    lat_sum = lon_sum = 0.0
    n = 0
    for ring in _outer_rings(geometry):
        vertices = list(ring)
        if len(vertices) > 1 and list(vertices[0])[:2] == list(vertices[-1])[:2]:
            vertices = vertices[:-1]
        for point in vertices:
            lon_sum += float(point[0])
            lat_sum += float(point[1])
            n += 1
    if not n:
        raise ConnectorError("geoBoundaries feature has an empty outer ring")
    return (lat_sum / n, lon_sum / n)


def points(regions: Iterable[Region]) -> dict[str, tuple[float, float]]:
    """`id -> (lat, lon)`, the shape `open_meteo.snapshot` asks for."""
    return {r.id: (r.centroid_lat, r.centroid_lon) for r in sorted(regions, key=_id)}


def _id(region: Region) -> str:
    return region.id
