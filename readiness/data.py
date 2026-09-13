"""Assemble a labelled panel for one contract from pinned snapshots.

This is the seam between the data plane and the eval plane. Connectors know how
to fetch and checksum; the harness knows how to score; this module turns the
former into the latter for the contract it is handed, and nothing else.

It also owns the per-contract layout on disk. Every contract gets its own
ledger, its own test-touch budget and its own blessed fingerprints, because an
experiment series against one hazard in one place says nothing about another.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Callable

from readiness.connectors import census, storm_events
from readiness.connectors.base import Manifest
from readiness.contracts import Contract
from readiness.harness.labels import Diagnostics, Panel, build_panel, diagnose

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = REPO_ROOT / "snapshots"
MANIFEST_PATH = SNAPSHOT_DIR / "manifest.json"
EXPERIMENTS_DIR = REPO_ROOT / "experiments"
EXPECTED_DIR = REPO_ROOT / "harness_expected"


@dataclass(frozen=True)
class Paths:
    """Where one contract's experimental record lives."""

    directory: pathlib.Path
    ledger: pathlib.Path
    touch_budget: pathlib.Path
    expected: pathlib.Path

    def relative(self, path: pathlib.Path) -> str:
        try:
            return str(path.relative_to(REPO_ROOT))
        except ValueError:
            return str(path)


def paths(
    contract: Contract,
    *,
    experiments_dir: pathlib.Path = EXPERIMENTS_DIR,
    expected_dir: pathlib.Path = EXPECTED_DIR,
) -> Paths:
    directory = experiments_dir / contract.name
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

    region_ids = [c.fips for c in regions]
    panel = build_panel(events, region_ids, years, contract)
    diagnostics = diagnose(events, region_ids, years, contract)
    return Dataset(
        contract=contract,
        panel=panel,
        regions=tuple(regions),
        data_version=manifest.digest(),
        manifest=manifest,
        diagnostics=diagnostics,
    )
