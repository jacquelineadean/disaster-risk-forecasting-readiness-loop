"""Assemble a labelled panel from pinned snapshots.

This is the seam between the data plane and the eval plane. Connectors know how
to fetch and checksum; the harness knows how to score; this module turns the
former into the latter and nothing else.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Callable, Sequence

from readiness.config import CONTRACT
from readiness.connectors import census, storm_events
from readiness.connectors.base import Manifest
from readiness.harness.labels import Panel, build_panel

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = REPO_ROOT / "snapshots"
MANIFEST_PATH = SNAPSHOT_DIR / "manifest.json"
EXPERIMENTS_DIR = REPO_ROOT / "experiments"
LEDGER_PATH = EXPERIMENTS_DIR / "ledger.jsonl"
TOUCH_BUDGET_PATH = EXPERIMENTS_DIR / "test_touches.json"
EXPECTED_DIR = REPO_ROOT / "harness_expected"


@dataclass(frozen=True)
class Dataset:
    """A panel plus the provenance needed to say which bytes produced it."""

    panel: Panel
    counties: tuple[census.County, ...]
    data_version: str
    manifest: Manifest

    def provenance(self) -> dict:
        return {
            "data_version": self.data_version,
            "panel_digest": self.panel.digest(),
            "n_counties": len(self.counties),
            "years": [self.panel.years[0], self.panel.years[-1]],
            "state": self.panel.state,
            "hazard": self.panel.hazard,
        }


def all_years() -> tuple[int, ...]:
    return tuple(
        sorted(set(CONTRACT.train_years + CONTRACT.validate_years + CONTRACT.test_years))
    )


def build(
    *,
    state: str = CONTRACT.state,
    hazard: str = CONTRACT.hazard,
    years: Sequence[int] | None = None,
    snapshot_dir: pathlib.Path = SNAPSHOT_DIR,
    keep_raw: bool = False,
    refresh: bool = False,
    progress: Callable[[str], None] = lambda _m: None,
) -> Dataset:
    """Snapshot what is missing, then build the labelled county-quarter panel."""
    years = tuple(years) if years is not None else all_years()
    manifest = Manifest.load(MANIFEST_PATH)

    progress("census: resolving county universe")
    counties = census.for_state(
        census.load(snapshot_dir / "census", manifest, refresh=refresh), state
    )
    state_fips = counties[0].fips[:2]
    progress(f"census: {len(counties)} counties in {state} (FIPS {state_fips})")

    progress(f"storm events: {years[0]}-{years[-1]}")
    extract = storm_events.snapshot(
        years,
        state_fips,
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
    progress(f"storm events: {len(events):,} events loaded for {state}")

    panel = build_panel(
        events,
        counties=[c.fips for c in counties],
        years=years,
        hazard=hazard,
        state=state,
    )
    return Dataset(
        panel=panel,
        counties=tuple(counties),
        data_version=manifest.digest(),
        manifest=manifest,
    )
