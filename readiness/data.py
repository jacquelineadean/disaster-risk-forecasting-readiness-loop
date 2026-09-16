"""Assemble a labelled panel for one contract from pinned snapshots.

This is the seam between the data plane and the eval plane. Connectors know how
to fetch and checksum; the harness knows how to score; this module turns the
former into the latter for the contract it is handed, and nothing else.

It also owns the per-contract layout on disk. Every contract gets its own
ledger, its own test-touch budget and its own blessed fingerprints, because an
experiment series against one hazard in one place says nothing about another.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from typing import Callable, Sequence

from readiness.connectors import (
    census,
    climada_layer,
    gazetteer,
    nri,
    nws_zones,
    open_meteo,
    storm_events,
)
from readiness.connectors.base import Manifest, sha256_bytes
from readiness.contracts import Contract
from readiness.harness.features import FeatureSource
from readiness.harness.labels import Diagnostics, Panel, panel_and_diagnostics

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = REPO_ROOT / "snapshots"
MANIFEST_PATH = SNAPSHOT_DIR / "manifest.json"
EXPERIMENTS_DIR = REPO_ROOT / "experiments"
EXPECTED_DIR = REPO_ROOT / "harness_expected"

#: Point every command at another experiments tree — for CI, or for a demo
#: that must not append to the committed ledgers.
EXPERIMENTS_DIR_ENV = "READINESS_EXPERIMENTS_DIR"

#: Manifest key of the Census county file. Must match what `census.load` pins.
CENSUS_KEY = "census/national_county2020"

#: The feature connectors `build(features=...)` knows how to load, by the name
#: a contract's run names them. `terrain` is the Gazetteer (plus the elevation
#: the ERA5 extract carries, when that extract is already pinned); `era5` is
#: the Open-Meteo monthly extract; `nri` and `climada` are the two static
#: layers that declare a vintage year and may be refused by the firewall.
FEATURE_CONNECTORS: tuple[str, ...] = ("era5", "terrain", "nri", "climada")


def storm_events_key(year: int) -> str:
    """Manifest key of one Storm Events year file, as `storm_events.snapshot` pins it."""
    return f"noaa/storm_events/{year}"


def input_keys(contract: Contract) -> list[str]:
    """The manifest keys a contract's panel is built from — its data version.

    The data version names exactly the pinned inputs the panel was built from,
    so pinning a source for one contract (another state's extract, the zone
    crosswalk) does not move another contract's fingerprints. This list is the
    single statement of which inputs those are; `build` hashes over it, the
    card records it, and `pinned` checks it.
    """
    keys = [CENSUS_KEY] + [storm_events_key(y) for y in contract.all_years()]
    if contract.zone_policy == "expand":
        keys.append(nws_zones.MANIFEST_KEY)
    return keys


def state_fips(snapshot_dir: pathlib.Path = SNAPSHOT_DIR) -> dict[str, str]:
    """Postal code -> 2-digit state FIPS from the pinned county file; empty if absent."""
    cache = snapshot_dir / "census" / "national_county2020.txt"
    if not cache.exists():
        return {}
    return {c.state: c.fips[:2] for c in census.parse(cache.read_bytes())}


def _extract_parts(contract: Contract, fips_of: dict[str, str]) -> list[str] | None:
    """Extract prefixes `build` reads; None when a state is not in the county file."""
    if not contract.states:
        return [storm_events.NATIONAL]
    parts = [fips_of.get(state) for state in contract.states]
    return None if None in parts else [p for p in parts if p is not None]


def era5_parts(contract: Contract, state_fips: Sequence[str]) -> list[str]:
    """The Open-Meteo extract labels a contract reads: its states, or `all`."""
    return sorted(state_fips) if contract.states else [storm_events.NATIONAL]


def _feature_files(
    contract: Contract,
    features: Sequence[str],
    parts: Sequence[str],
    snapshot_dir: pathlib.Path,
) -> list[tuple[str, pathlib.Path]]:
    """(manifest key, file) for every pinned artefact the named features need."""
    wanted: list[tuple[str, pathlib.Path]] = []
    census_dir = snapshot_dir / "census"
    if "terrain" in features or "era5" in features:
        wanted.append((gazetteer.MANIFEST_KEY, census_dir / gazetteer.CACHE_NAME))
    if "era5" in features:
        for part in parts:
            path = open_meteo.extract_path(snapshot_dir, part)
            wanted.append((open_meteo.manifest_key(part), path))
    if "nri" in features:
        wanted.append((nri.MANIFEST_KEY, snapshot_dir / "fema" / nri.CACHE_NAME))
    if "climada" in features:
        wanted.append((
            climada_layer.manifest_key(contract.hazard, contract.scope_key),
            climada_layer.layer_path(snapshot_dir, contract.hazard, contract.scope_key),
        ))
    return wanted


def pinned(
    contract: Contract,
    snapshot_dir: pathlib.Path = SNAPSHOT_DIR,
    features: Sequence[str] = (),
) -> bool:
    """Whether `build` can run for this contract without fetching anything.

    True only when every input named by `input_keys` is both recorded in the
    manifest *and* present on disk in the layout the connectors read: bytes
    with no record are unpinned and would be re-fetched, and a record with no
    bytes is a download waiting to happen. Either way the answer is no. With
    `features`, the same test is applied to every file those connectors read.
    """
    manifest = Manifest.load(snapshot_dir / "manifest.json")
    if any(key not in manifest.records for key in input_keys(contract)):
        return False
    fips_of = state_fips(snapshot_dir)
    if not fips_of:
        return False  # no county file, no region universe
    parts = _extract_parts(contract, fips_of)
    if parts is None:
        return False
    extracts = snapshot_dir / "storm_events"
    for year in contract.all_years():
        for part in parts:
            if not (extracts / f"{part}_{year}.jsonl").exists():
                return False
    if contract.zone_policy == "expand":
        if not (snapshot_dir / "nws" / "zone_county.dbx").exists():
            return False
    _check_feature_names(features)
    parts = era5_parts(contract, parts)
    for key, path in _feature_files(contract, features, parts, snapshot_dir):
        if key not in manifest.records or not path.exists():
            return False
    return True


def _pinned_file(manifest: Manifest, key: str, path: pathlib.Path) -> bool:
    """Whether `path` exists and hashes to the manifest's record for `key`."""
    record = manifest.records.get(key)
    if record is None or not path.exists():
        return False
    return sha256_bytes(path.read_bytes()) == record.sha256


def _check_feature_names(features: Sequence[str]) -> None:
    unknown = sorted(set(features) - set(FEATURE_CONNECTORS))
    if unknown:
        raise ValueError(
            f"unknown feature connector(s) {unknown}; known: {FEATURE_CONNECTORS}"
        )


def experiments_root() -> pathlib.Path:
    override = os.environ.get(EXPERIMENTS_DIR_ENV)
    return pathlib.Path(override) if override else EXPERIMENTS_DIR


@dataclass(frozen=True)
class Paths:
    """Where one contract's experimental record lives."""

    directory: pathlib.Path
    ledger: pathlib.Path
    touch_budget: pathlib.Path
    expected: pathlib.Path

    def relative(self, path: pathlib.Path) -> str:
        return relative(path)


def relative(path: pathlib.Path) -> str:
    """A repo-relative rendering of a path, or the path itself if it lies outside."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def paths(
    contract: Contract,
    *,
    experiments_dir: pathlib.Path | None = None,
    expected_dir: pathlib.Path = EXPECTED_DIR,
) -> Paths:
    directory = (experiments_dir or experiments_root()) / contract.name
    return Paths(
        directory=directory,
        ledger=directory / "ledger.jsonl",
        touch_budget=directory / "test_touches.json",
        expected=expected_dir / f"{contract.name}.json",
    )


@dataclass(frozen=True)
class Dataset:
    """A panel plus the provenance needed to say which bytes produced it."""

    contract: Contract
    panel: Panel
    regions: tuple[census.County, ...]
    data_version: str
    manifest: Manifest
    diagnostics: Diagnostics
    #: Feature sources loaded by name (`build(features=...)`), keyed by the
    #: source name a `FeatureSpec` refers to. Empty for a Phase 0 build.
    sources: dict[str, FeatureSource] = field(default_factory=dict)
    #: `Manifest.digest` over the sources' manifest keys; "" when none were
    #: loaded. Separate from `data_version` on purpose: features never move
    #: the version of the panel they are scored against.
    feature_version: str = ""
    feature_inputs: tuple[str, ...] = ()

    def provenance(self) -> dict:
        prov = {
            "contract": self.contract.name,
            "contract_digest": self.contract.digest(),
            "data_version": self.data_version,
            "panel_digest": self.panel.digest(),
            "hazard": self.contract.hazard,
            "scope": self.contract.scope_key,
            "period": self.contract.period,
            "n_regions": len(self.regions),
            "years": [self.panel.years[0], self.panel.years[-1]],
            # Which manifest keys `data_version` was hashed over. New cards
            # only: this lives inside `data_snapshot`, so committed cards keep
            # their hashes.
            "inputs": input_keys(self.contract),
        }
        if self.sources:
            # Only when features were loaded: a card for a Phase 0 run keeps
            # exactly the keys it always had.
            prov["feature_version"] = self.feature_version
            prov["feature_inputs"] = list(self.feature_inputs)
        return prov


def _load_features(
    contract: Contract,
    features: Sequence[str],
    regions: Sequence[census.County],
    snapshot_dir: pathlib.Path,
    manifest: Manifest,
    *,
    keep_raw: bool,
    refresh: bool,
    progress: Callable[[str], None],
) -> dict[str, FeatureSource]:
    """Load the named feature connectors. Regions and pinned files only.

    Nothing here sees an event or the panel: a feature source is built from
    the region list and the bytes the manifest pins, and the harness admits or
    refuses it later, at scoring time.
    """
    _check_feature_names(features)
    sources: dict[str, FeatureSource] = {}
    if not features:
        return sources
    parts = era5_parts(contract, sorted({c.fips[:2] for c in regions}))
    extracts = [open_meteo.extract_path(snapshot_dir, p) for p in parts]
    keys = [open_meteo.manifest_key(p) for p in parts]
    centroids = None
    if "terrain" in features or "era5" in features:
        progress("gazetteer: resolving county centroids")
        centroids = gazetteer.load(snapshot_dir / "census", manifest, refresh=refresh)
        manifest.save()
    if "terrain" in features:
        sources["gazetteer"] = gazetteer.source(centroids)
        # Elevation rides along only when every extract for the scope is
        # pinned, by the same test `pinned_bytes` applies: bytes that do not
        # hash to their record are data of unknown provenance.
        if all(_pinned_file(manifest, k, p) for k, p in zip(keys, extracts)):
            sources["elevation"] = open_meteo.sources(extracts, keys)["elevation"]
    if "era5" in features:
        progress(f"open-meteo: ERA5 monthly for {len(regions)} counties")
        located = gazetteer.points(centroids)
        points = {c.fips: located[c.fips] for c in regions if c.fips in located}
        open_meteo.snapshot(
            points,
            contract.all_years(),
            snapshot_dir,
            manifest,
            scope=None if contract.states else storm_events.NATIONAL,
            keep_raw=keep_raw,
            refresh=refresh,
            progress=progress,
        )
        manifest.save()
        sources.update(open_meteo.sources(extracts, keys))
    if "nri" in features:
        progress(f"nri: {nri.VINTAGE.citation}, through {nri.VINTAGE.derived_through}")
        table = nri.load(snapshot_dir / "fema", manifest, refresh=refresh)
        sources["nri"] = nri.source(table)
        manifest.save()
    if "climada" in features:
        key = climada_layer.manifest_key(contract.hazard, contract.scope_key)
        path = climada_layer.layer_path(snapshot_dir, contract.hazard, contract.scope_key)
        layer = climada_layer.load(path, manifest, key)
        manifest.save()
        progress(f"climada: layer {path.name}, event set through {layer.derived_through}")
        sources["climada"] = climada_layer.source(layer, key)
    return sources


def build(
    contract: Contract,
    *,
    snapshot_dir: pathlib.Path = SNAPSHOT_DIR,
    keep_raw: bool = False,
    refresh: bool = False,
    progress: Callable[[str], None] = lambda _m: None,
    features: Sequence[str] = (),
) -> Dataset:
    """Snapshot what is missing, then build the contract's labelled panel.

    `features` names connectors from `FEATURE_CONNECTORS` to load after the
    panel is built; they become `Dataset.sources`, and `feature_version`
    fingerprints their pinned inputs without touching `data_version`.
    """
    years = contract.all_years()
    manifest = Manifest.load(snapshot_dir / "manifest.json")

    progress("census: resolving region universe")
    regions = census.for_states(
        census.load(snapshot_dir / "census", manifest, refresh=refresh),
        contract.states,
    )
    state_fips = sorted({c.fips[:2] for c in regions})
    progress(
        f"census: {len(regions)} counties in {contract.scope_label} "
        f"(FIPS {', '.join(state_fips)})"
    )

    progress(f"storm events: {years[0]}-{years[-1]}")
    extract = storm_events.snapshot(
        years,
        state_fips if contract.states else None,
        snapshot_dir,
        manifest,
        keep_raw=keep_raw,
        refresh=refresh,
        progress=progress,
    )
    # Save provenance *before* parsing. A parse failure downstream must not
    # discard the record of a completed download — that is how a snapshot ends
    # up on disk with no manifest entry, i.e. unpinned.
    manifest.save()
    if not manifest:
        raise RuntimeError(
            "snapshot completed but the manifest is empty: the data is unpinned "
            "and no experiment run against it could be reproduced. "
            "Re-run with --refresh."
        )

    events = storm_events.load_events(extract)
    progress(f"storm events: {len(events):,} events loaded for {contract.scope_label}")

    crosswalk = None
    if contract.zone_policy == "expand":
        progress("nws zones: resolving zone-county crosswalk")
        crosswalk = nws_zones.load(snapshot_dir / "nws", manifest, refresh=refresh)
        manifest.save()
        progress(
            f"nws zones: {len(crosswalk):,} zones in {crosswalk.n_states} states "
            f"(edition {crosswalk.edition})"
        )

    region_ids = [c.fips for c in regions]
    panel, diagnostics = panel_and_diagnostics(
        events, region_ids, years, contract, crosswalk
    )
    sources = _load_features(
        contract, features, regions, snapshot_dir, manifest,
        keep_raw=keep_raw, refresh=refresh, progress=progress,
    )
    feature_inputs = tuple(sorted({k for s in sources.values() for k in s.manifest_keys}))
    return Dataset(
        contract=contract,
        panel=panel,
        regions=tuple(regions),
        data_version=manifest.digest(input_keys(contract)),
        manifest=manifest,
        diagnostics=diagnostics,
        sources=sources,
        feature_version=manifest.digest(feature_inputs) if feature_inputs else "",
        feature_inputs=feature_inputs,
    )
