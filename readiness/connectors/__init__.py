"""The data plane.

Every source is fetched, checksummed and pinned before it is used, so an
experiment names a data version rather than "whatever was on the server that
day". Report §7 is the standing justification: NOAA retired the Billion-Dollar
Disasters product in May 2025, and core series can stop.

`CONNECTORS` is the registry, as data: one entry per connector with the
manifest-key prefix it pins under, its licence, and whether it is a
ground-truth source. The harness's label-origin rule and the licence manifest
(`DATA-LICENSES.md`) are both checked against it, so a connector cannot be
added without saying what it is.

The entries are literals, deliberately. The harness's label builder imports a
connector (the zone crosswalk type) and the feature connectors import the
harness's feature channel, so this package's `__init__` must not import its
own modules or the two subtrees deadlock on whichever is entered first.
`tests/test_connectors.py::TestRegistry` ties every literal to the constant
the connector actually pins with.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

__all__ = ["ConnectorInfo", "CONNECTORS", "connector_for_key"]


@dataclass(frozen=True)
class ConnectorInfo:
    """What a connector is, for the registry's readers.

    `key_prefix` is what its manifest keys start with (a full key for a
    connector that pins one file). `is_label_source` marks the ground truth:
    nothing pinned under such a prefix may be a feature.
    """

    key_prefix: str
    source: str
    license: str
    global_coverage: bool
    network: bool
    is_label_source: bool


CONNECTORS: dict[str, ConnectorInfo] = {
    "census": ConnectorInfo(
        key_prefix="census/national_county2020",
        source="US Census Bureau, national county file (2020)",
        license="US Government work — public domain (17 U.S.C. §105)",
        global_coverage=False,
        network=True,
        is_label_source=False,
    ),
    "storm_events": ConnectorInfo(
        key_prefix="noaa/storm_events/",
        source="NOAA NCEI Storm Events Database",
        license="US Government work — public domain (17 U.S.C. §105); cite NOAA NCEI",
        global_coverage=False,
        network=True,
        is_label_source=True,
    ),
    "nws_zones": ConnectorInfo(
        key_prefix="nws/zone_county",
        source="NOAA NWS zone-county correlation file",
        license="US Government work — public domain (17 U.S.C. §105); cite NOAA NWS",
        global_coverage=False,
        network=True,
        is_label_source=False,
    ),
    "gazetteer": ConnectorInfo(
        key_prefix="census/gazetteer_counties2020",
        source="US Census Bureau 2020 Gazetteer, counties",
        license=(
            "US Government work — public domain (17 U.S.C. §105); cite the Census Bureau"
        ),
        global_coverage=False,
        network=True,
        is_label_source=False,
    ),
    "era5": ConnectorInfo(
        key_prefix="open-meteo/era5/",
        source="Open-Meteo ERA5 archive",
        license="CC BY 4.0 (Open-Meteo; ERA5 by ECMWF/Copernicus)",
        global_coverage=True,
        network=True,
        is_label_source=False,
    ),
    "nri": ConnectorInfo(
        key_prefix="fema/nri_counties_",
        source="FEMA National Risk Index, county table",
        license="US Government work — public domain (17 U.S.C. §105); cite FEMA",
        global_coverage=False,
        network=True,
        is_label_source=False,
    ),
    "climada": ConnectorInfo(
        key_prefix="climada/",
        source="CLIMADA event set",
        license="GPL-3.0 tool output; layer values CC BY 4.0",
        global_coverage=True,
        network=False,
        is_label_source=False,
    ),
    "usa_structures": ConnectorInfo(
        key_prefix="fema/usa_structures/",
        source="FEMA / ORNL USA Structures, county counts by occupancy",
        license="US Government work, public domain (FEMA / ORNL USA Structures)",
        global_coverage=False,
        network=True,
        is_label_source=False,
    ),
}


def connector_for_key(
    manifest_key: str, registry: Mapping[str, ConnectorInfo] = CONNECTORS
) -> ConnectorInfo:
    """The connector that pins `manifest_key`: longest matching prefix wins."""
    best = None
    for info in registry.values():
        if manifest_key.startswith(info.key_prefix):
            if best is None or len(info.key_prefix) > len(best.key_prefix):
                best = info
    if best is None:
        raise KeyError(f"no registered connector pins manifest key {manifest_key!r}")
    return best
