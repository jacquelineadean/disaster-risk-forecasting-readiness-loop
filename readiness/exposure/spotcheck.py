"""The exposure spot-check: our county counts against a person's assessor counts.

Plan §3 exit: "exposure joins spot-validated against county assessor counts in
ten sampled counties". The counts are collected by a person from county
assessor open-data pages and committed in `exposure_expected/assessor_counts.csv`
with the URL, the retrieval date and the *definition* of what was counted:
assessors publish improved parcels far more often than structures, and a
parcel is not a structure (see `readiness.config.EXPOSURE_SPOTCHECK_RATIO`).

The check is deliberately plain. Every row's ratio is computed and printed;
a row is "within" when the ratio sits inside the declared band; the summary
counts only in-band counties toward the ten and needs them from at least
three states, so one state's assessor convention cannot carry the exit. A
row outside the band is a finding to read, never a row to tune away.
"""

from __future__ import annotations

import csv
import datetime as _dt
import pathlib
from dataclasses import dataclass
from typing import Iterable, Sequence

from readiness import config
from readiness.exposure.table import ExposureTable

COLUMNS = ("fips", "assessor_count", "count_definition", "source_url", "retrieved_on", "notes")
DEFINITIONS = ("structures", "improved_parcels")

#: The exit needs this many in-band counties, from this many states.
MIN_COUNTIES = 10
MIN_STATES = 3


class SpotCheckError(ValueError):
    """A counts file the check refuses to read: wrong columns or a bad row."""


@dataclass(frozen=True)
class AssessorCount:
    """One person-collected count, with where it came from and what it counts."""

    fips: str
    assessor_count: int
    count_definition: str
    source_url: str
    retrieved_on: str
    notes: str = ""


@dataclass(frozen=True)
class SpotCheck:
    """One county's ratio. `ours` and `ratio` are None when we hold no row."""

    fips: str
    state: str
    ours: int | None
    assessor: int
    ratio: float | None
    within: bool
    definition: str

    def format(self) -> str:
        ratio = "   n/a" if self.ratio is None else f"{self.ratio:6.2f}"
        ours = "not pinned" if self.ours is None else f"{self.ours:>9,}"
        flag = "within" if self.within else "OUTSIDE"
        return (
            f"  {self.fips}  ours {ours:>10}  assessor {self.assessor:>9,}  "
            f"ratio {ratio}  {flag:<7}  ({self.definition})"
        )


@dataclass(frozen=True)
class SpotCheckSummary:
    n_counties: int
    n_within: int
    n_states_within: int
    within_bounds: bool

    def format(self) -> str:
        verdict = "PASS" if self.within_bounds else "NOT YET"
        return (
            f"spot-check -> {verdict}: {self.n_within}/{self.n_counties} counties within "
            f"the band from {self.n_states_within} state(s); the exit needs "
            f">= {MIN_COUNTIES} counties from >= {MIN_STATES} states"
        )


def _row(raw: dict, where: str) -> AssessorCount:
    fips = (raw.get("fips") or "").strip()
    if not (fips.isdigit() and len(fips) == 5):
        raise SpotCheckError(f"{where}: fips must be five digits, got {fips!r}")
    count = (raw.get("assessor_count") or "").strip().replace(",", "")
    if not count.isdigit() or int(count) <= 0:
        raise SpotCheckError(f"{where}: assessor_count must be a positive integer")
    definition = (raw.get("count_definition") or "").strip()
    if definition not in DEFINITIONS:
        raise SpotCheckError(
            f"{where}: count_definition must be one of {DEFINITIONS}, got {definition!r}"
        )
    url = (raw.get("source_url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise SpotCheckError(f"{where}: source_url must be an http(s) URL")
    retrieved = (raw.get("retrieved_on") or "").strip()
    try:
        _dt.date.fromisoformat(retrieved)
    except ValueError:
        raise SpotCheckError(f"{where}: retrieved_on must be YYYY-MM-DD") from None
    return AssessorCount(fips, int(count), definition, url, retrieved, (raw.get("notes") or "").strip())


def load(path: pathlib.Path) -> list[AssessorCount]:
    """The committed counts. Header-only is fine; a malformed row is not."""
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise SpotCheckError(
                f"{path}: columns must be exactly {list(COLUMNS)}, got {reader.fieldnames}"
            )
        rows = [_row(raw, f"{path}:{n}") for n, raw in enumerate(reader, start=2)]
    seen: set[str] = set()
    for row in rows:
        if row.fips in seen:
            raise SpotCheckError(f"{path}: county {row.fips} listed twice")
        seen.add(row.fips)
    return rows


def run(
    table: ExposureTable,
    counts: Iterable[AssessorCount],
    ratio_bounds: tuple[float, float] = config.EXPOSURE_SPOTCHECK_RATIO,
) -> list[SpotCheck]:
    """Our total over the assessor's count for every listed county."""
    low, high = ratio_bounds
    checks = []
    for count in counts:
        row = table.for_county(count.fips)
        ours = row.total if row is not None else None
        ratio = ours / count.assessor_count if ours is not None else None
        within = ratio is not None and low <= ratio <= high
        checks.append(
            SpotCheck(
                count.fips, count.fips[:2], ours, count.assessor_count, ratio, within,
                count.count_definition,
            )
        )
    return checks


def summary(checks: Sequence[SpotCheck]) -> SpotCheckSummary:
    """Only in-band counties count toward the ten, and their states toward the three."""
    within = [c for c in checks if c.within]
    n_within = len({c.fips for c in within})
    n_states = len({c.state for c in within})
    return SpotCheckSummary(
        n_counties=len({c.fips for c in checks}),
        n_within=n_within,
        n_states_within=n_states,
        within_bounds=n_within >= MIN_COUNTIES and n_states >= MIN_STATES,
    )


def format(checks: Sequence[SpotCheck]) -> str:  # noqa: A001 - the module's verb
    """The verdict, then the band, then every ratio: nothing is hidden behind it.

    The verdict leads because this text is a check's detail, and `verify`
    prints a failed check's *first* line as the failure — every other check
    there says what it decided on its line one, and this one used to say
    "exposure spot-check, ratio band [...]", which decides nothing.
    """
    low, high = config.EXPOSURE_SPOTCHECK_RATIO
    head = f"exposure spot-check, ratio band [{low}, {high}] (ours / assessor)"
    body = [c.format() for c in checks] or ["  (no assessor counts committed)"]
    return "\n".join([summary(checks).format(), head, *body])
