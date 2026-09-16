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
from dataclasses import dataclass
from typing import Callable

from readiness.connectors import census, nws_zones, storm_events
from readiness.connectors.base import Manifest
from readiness.contracts import Contract
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


def pinned(contract: Contract, snapshot_dir: pathlib.Path = SNAPSHOT_DIR) -> bool:
    """Whether `build` can run for this contract without fetching anything.

    True only when every input named by `input_keys` is both recorded in the
    manifest *and* present on disk in the layout the connectors read: bytes
    with no record are unpinned and would be re-fetched, and a record with no
    bytes is a download waiting to happen. Either way the answer is no.
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
        return (snapshot_dir / "nws" / "zone_county.dbx").exists()
    return True


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

    def provenance(self) -> dict:
        return {
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


def build(
    contract: Contract,
    *,
    snapshot_dir: pathlib.Path = SNAPSHOT_DIR,
    keep_raw: bool = False,
    refresh: bool = False,
    progress: Callable[[str], None] = lambda _m: None,
) -> Dataset:
    """Snapshot what is missing, then build the contract's labelled panel."""
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
    return Dataset(
        contract=contract,
        panel=panel,
        regions=tuple(regions),
        data_version=manifest.digest(input_keys(contract)),
        manifest=manifest,
        diagnostics=diagnostics,
    )
