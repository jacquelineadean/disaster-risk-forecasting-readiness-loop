"""A pinned CLIMADA layer: return-period intensities produced outside the package.

Report §3E wants the agent to "drive, extend, and calibrate" CLIMADA rather than
reinvent it, and the licence manifest says why the integration stays at arm's
length: CLIMADA is GPL-3.0 and viral across a linked work. So the whole Phase 1
seam is a file. `tools/climada/run_event_set.py` (never imported by this
package) runs the event set with the `climada` extra and writes

    snapshots/climada/<hazard>_<scope_key>.jsonl

whose first line is a header
`{"event_set_years": [a, b], "seed": int, "climada_version": str, "hazard": str}`
and every following line `{"region": fips, "rp10": x, "rp50": x, "rp100": x}`.
This connector reads it, pins its bytes in the manifest, and hands the harness
a static source whose `derived_through` is the last year of the event set:
a set built from tracks and gauges through 2015 is admissible under the
current contracts, one through 2020 is refused. The year is read from the
header the tool wrote, and the tool is the reviewed constant.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Mapping

from readiness.connectors.base import (
    ConnectorError,
    Manifest,
    SourceRecord,
    sha256_bytes,
    utc_now,
)
from readiness.harness.features import NAN, Series

LICENSE = "GPL-3.0 tool output; layer values CC BY 4.0"
SOURCE = "CLIMADA event set"
KEY_PREFIX = "climada/"
TOOL = "tools/climada/run_event_set.py"

_HEADER_KEYS = ("event_set_years", "seed", "climada_version", "hazard")
_ROW_KEYS = ("region", "rp10", "rp50", "rp100")
RETURN_PERIODS = ("rp10", "rp50", "rp100")


@dataclass(frozen=True)
class LayerHeader:
    event_set_years: tuple[int, int]
    seed: int
    climada_version: str
    hazard: str


@dataclass(frozen=True)
class ClimadaLayer:
    header: LayerHeader
    rows: Mapping[str, Mapping[str, float]]

    @property
    def derived_through(self) -> int:
        """The last year the event set was built from: the layer's vintage."""
        return self.header.event_set_years[1]


def layer_name(hazard: str, scope_key: str) -> str:
    return f"{hazard}_{scope_key}"


def layer_path(snapshot_dir: pathlib.Path, hazard: str, scope_key: str) -> pathlib.Path:
    return snapshot_dir / "climada" / f"{layer_name(hazard, scope_key)}.jsonl"


def manifest_key(hazard: str, scope_key: str) -> str:
    return f"{KEY_PREFIX}{layer_name(hazard, scope_key)}"


def _header(raw: dict) -> LayerHeader:
    missing = [k for k in _HEADER_KEYS if k not in raw]
    if missing:
        raise ConnectorError(f"CLIMADA layer header lacks {missing}: {raw}")
    years = raw["event_set_years"]
    if (
        not isinstance(years, list)
        or len(years) != 2
        or not all(isinstance(y, int) for y in years)
        or years[0] > years[1]
    ):
        raise ConnectorError(
            f"event_set_years must be [first, last] years, got {years!r}"
        )
    if not isinstance(raw["seed"], int):
        raise ConnectorError(f"seed must be an integer, got {raw['seed']!r}")
    return LayerHeader(
        event_set_years=(years[0], years[1]),
        seed=raw["seed"],
        climada_version=str(raw["climada_version"]),
        hazard=str(raw["hazard"]),
    )


def _row(raw: dict, line_no: int) -> tuple[str, dict[str, float]]:
    missing = [k for k in _ROW_KEYS if k not in raw]
    if missing:
        raise ConnectorError(f"CLIMADA layer line {line_no} lacks {missing}: {raw}")
    values: dict[str, float] = {}
    for rp in RETURN_PERIODS:
        v = raw[rp]
        if v is None:
            values[rp] = NAN
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            values[rp] = float(v)
        else:
            raise ConnectorError(
                f"CLIMADA layer line {line_no}: {rp} is not a number: {v!r}"
            )
    return str(raw["region"]), values


def parse(data: bytes) -> ClimadaLayer:
    """Pure: the JSONL bytes -> header and rows. Any drift is an error."""
    lines = [ln for ln in data.decode("utf-8").splitlines() if ln.strip()]
    if not lines:
        raise ConnectorError("CLIMADA layer file is empty; it needs a header line")
    try:
        records = [json.loads(ln) for ln in lines]
    except json.JSONDecodeError as exc:
        raise ConnectorError(f"CLIMADA layer is not JSONL: {exc}") from None
    if not all(isinstance(r, dict) for r in records):
        raise ConnectorError("every CLIMADA layer line must be a JSON object")
    header = _header(records[0])
    rows: dict[str, dict[str, float]] = {}
    for n, raw in enumerate(records[1:], start=2):
        region, values = _row(raw, n)
        rows[region] = values
    if not rows:
        raise ConnectorError("CLIMADA layer has a header but no regions")
    return ClimadaLayer(header, rows)


def load(path: pathlib.Path, manifest: Manifest, key: str) -> ClimadaLayer:
    """Read and pin a layer file. Absent file -> an error naming the tool.

    The record is only (re)written when the bytes changed, so reading a layer
    that is already pinned leaves the manifest untouched.
    """
    if not path.exists():
        raise ConnectorError(
            f"no CLIMADA layer at {path}; produce it with `{TOOL}` (needs the "
            "optional climada extra, outside the sandbox) and re-run"
        )
    data = path.read_bytes()
    layer = parse(data)
    sha = sha256_bytes(data)
    prior = manifest.records.get(key)
    if prior is None or prior.sha256 != sha:
        h = layer.header
        manifest.add(
            key,
            SourceRecord(
                source=SOURCE,
                url=TOOL,
                sha256=sha,
                bytes=len(data),
                fetched_at=utc_now(),
                license=LICENSE,
                notes=(
                    f"event set {h.event_set_years[0]}-{h.event_set_years[1]}, "
                    f"seed {h.seed}, climada {h.climada_version}; "
                    f"derived_through={layer.derived_through}"
                ),
            ),
        )
    return layer


class ClimadaSource:
    """Static return-period intensities, dated to the event set's last year."""

    name = "climada"
    kind = "static"
    global_coverage = True

    def __init__(self, layer: ClimadaLayer, key: str) -> None:
        self._rows = layer.rows
        self.derived_through: int | None = layer.derived_through
        self.manifest_keys = (key,)

    def series(self, region: str, variable: str) -> Series | None:
        return None

    def static(self, region: str) -> dict[str, float] | None:
        row = self._rows.get(region)
        return dict(row) if row is not None else None


def source(layer: ClimadaLayer, key: str) -> ClimadaSource:
    return ClimadaSource(layer, key)
