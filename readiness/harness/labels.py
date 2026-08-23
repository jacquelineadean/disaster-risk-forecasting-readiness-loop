"""County-quarter labels: the ground truth the loop backtests against.

The forecast unit (report §5) is:

    at least one damaging event of hazard H in county C within quarter Q

Two things make this harder than it looks, and both are handled here:

* **The panel must be complete.** Storm Events only records events. If you build
  labels from the event table alone you get a dataset of nothing but positives
  and a base rate of 1.0. The county universe comes from the Census, and every
  (county, year, quarter) cell that saw no damaging event is an explicit zero.

* **"Damaging" must be fixed in advance.** The threshold lives in `config`, not
  here, so it cannot drift while someone is iterating on a model.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence

from readiness.config import CONTRACT, HAZARDS

#: A single forecast unit. Ordered so panels sort deterministically.
Unit = tuple[str, int, int]  # (county_fips, year, quarter)


@dataclass(frozen=True)
class StormEvent:
    """The subset of a Storm Events row this project depends on."""

    event_id: str
    year: int
    month: int
    event_type: str
    state_fips: str
    cz_type: str
    cz_fips: str
    county_fips: str
    injuries: int
    deaths: int
    damage_property_usd: float
    damage_crops_usd: float

    @property
    def quarter(self) -> int:
        return (self.month - 1) // 3 + 1

    @property
    def unit(self) -> Unit:
        return (self.county_fips, self.year, self.quarter)


_MAGNITUDE = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
_DAMAGE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([KMBT]?)\s*$", re.IGNORECASE)
#: A magnitude suffix with no number at all. NOAA emits this occasionally for a
#: field that is simply empty — one row in ~25,000 in the Louisiana extract
#: ("K" in DAMAGE_CROPS). No digits means no amount, so it is zero. Handled
#: explicitly rather than swept into the general parse so that genuinely
#: malformed values keep raising.
_BARE_MAGNITUDE_RE = re.compile(r"^\s*[KMBT]\s*$", re.IGNORECASE)


def parse_damage(raw: str | None) -> float:
    """Storm Events writes damage as `"10.00K"`, `"1.50M"`, `"0.00K"` or `""`.

    Returns dollars. An unparseable value raises rather than silently becoming
    zero — a silent zero is a label error, and label errors are the one class of
    bug the whole harness exists to avoid.
    """
    if raw is None:
        return 0.0
    s = raw.strip()
    if not s:
        return 0.0
    if _BARE_MAGNITUDE_RE.match(s):
        return 0.0
    m = _DAMAGE_RE.match(s)
    if not m:
        raise ValueError(f"unparseable Storm Events damage value: {raw!r}")
    return float(m.group(1)) * _MAGNITUDE[m.group(2).upper()]


def is_damaging(event: StormEvent) -> bool:
    """Apply the pre-registered damage threshold. Fixed in `config`."""
    if event.damage_property_usd >= CONTRACT.damage_property_usd_min:
        return True
    if CONTRACT.damage_count_casualties and (event.injuries > 0 or event.deaths > 0):
        return True
    return False


def county_fips(state_fips: str, cz_fips: str) -> str:
    """Storm Events stores an unpadded within-state county code; join needs both."""
    return f"{int(state_fips):02d}{int(cz_fips):03d}"


@dataclass(frozen=True)
class Panel:
    """A complete, dense county x year x quarter panel with binary labels.

    `units` and `labels` are parallel and sorted, so the digest below is a
    stable fingerprint of exactly what a model was shown or scored against.
    """

    units: tuple[Unit, ...]
    labels: tuple[int, ...]
    hazard: str
    state: str

    def __post_init__(self) -> None:
        if len(self.units) != len(self.labels):
            raise ValueError("units and labels must be the same length")

    def __len__(self) -> int:
        return len(self.units)

    def __iter__(self) -> Iterator[tuple[Unit, int]]:
        return iter(zip(self.units, self.labels))

    @property
    def years(self) -> tuple[int, ...]:
        return tuple(sorted({u[1] for u in self.units}))

    @property
    def base_rate(self) -> float:
        return sum(self.labels) / len(self.labels) if self.labels else 0.0

    def filter_years(self, years: Sequence[int]) -> "Panel":
        keep = set(years)
        pairs = [(u, y) for u, y in zip(self.units, self.labels) if u[1] in keep]
        return Panel(
            units=tuple(u for u, _ in pairs),
            labels=tuple(y for _, y in pairs),
            hazard=self.hazard,
            state=self.state,
        )

    def digest(self) -> str:
        """Content hash over units *and* labels. Identifies an exact dataset."""
        h = hashlib.sha256()
        h.update(f"{self.hazard}|{self.state}\n".encode())
        for (fips, year, quarter), label in zip(self.units, self.labels):
            h.update(f"{fips}|{year}|{quarter}|{label}\n".encode())
        return h.hexdigest()[:16]

    def units_digest(self) -> str:
        """Content hash over units only — what a model is allowed to see at predict time."""
        h = hashlib.sha256()
        h.update(f"{self.hazard}|{self.state}\n".encode())
        for fips, year, quarter in self.units:
            h.update(f"{fips}|{year}|{quarter}\n".encode())
        return h.hexdigest()[:16]

    def summary(self) -> str:
        pos = sum(self.labels)
        yrs = self.years
        return (
            f"{len(self):,} units  |  {pos:,} positive  "
            f"|  base rate {self.base_rate:.4f}  "
            f"|  {yrs[0]}-{yrs[-1]}  |  sha256:{self.digest()}"
        )


def build_panel(
    events: Iterable[StormEvent],
    counties: Sequence[str],
    years: Sequence[int],
    hazard: str = CONTRACT.hazard,
    state: str = CONTRACT.state,
) -> Panel:
    """Cross counties x years x quarters, then mark cells with a damaging event.

    `events` should already be filtered to the state; hazard filtering happens
    here so the caller cannot accidentally pass a different event-type set than
    the contract declares.
    """
    if hazard not in HAZARDS:
        raise KeyError(f"unknown hazard {hazard!r}; known: {sorted(HAZARDS)}")
    event_types = set(HAZARDS[hazard])
    year_set = set(years)
    county_set = set(counties)

    positive: set[Unit] = set()
    for event in events:
        if event.event_type not in event_types:
            continue
        if event.cz_type != "C":  # zone-coded rows do not join to counties
            continue
        if event.year not in year_set or event.county_fips not in county_set:
            continue
        if is_damaging(event):
            positive.add(event.unit)

    units: list[Unit] = []
    labels: list[int] = []
    for fips in sorted(county_set):
        for year in sorted(year_set):
            for quarter in (1, 2, 3, 4):
                unit = (fips, year, quarter)
                units.append(unit)
                labels.append(1 if unit in positive else 0)

    return Panel(tuple(units), tuple(labels), hazard=hazard, state=state)
