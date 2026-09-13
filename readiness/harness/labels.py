"""Region-period labels: the ground truth the loop backtests against.

The forecast unit (report §5) is:

    at least one damaging event of hazard H in region R during period T

where the hazard, the regions and the period are all set by the contract. For
the NOAA Storm Events ground truth, regions are US counties and the period is
a month, a quarter or a year.

Two things make this harder than it looks, and both are handled here:

* **The panel must be complete.** Storm Events only records events. If you build
  labels from the event table alone you get a dataset of nothing but positives
  and a base rate of 1.0. The region universe comes from the Census, and every
  (region, year, period) cell that saw no damaging event is an explicit zero.

* **"Damaging" must be fixed in advance.** The threshold lives in the contract,
  not here, so it cannot drift while someone is iterating on a model.

One thing is *not* handled here, and is reported rather than hidden: Storm
Events codes many hazards against NWS forecast zones rather than counties.
Zone-coded rows do not join to a county universe, and this builder drops them.
`diagnose()` counts exactly how many, so a contract for a zone-coded hazard is
told that its panel is empty for a reason, not silently handed a base rate of
zero.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence

from readiness.contracts import Contract

#: A single forecast unit. Ordered so panels sort deterministically.
Unit = tuple[str, int, int]  # (region_id, year, period)


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

    def period_index(self, periods_per_year: int) -> int:
        """1-based period of the year: month 8 is quarter 3, half 2, month 8, year 1."""
        return (self.month - 1) * periods_per_year // 12 + 1

    def unit_for(self, periods_per_year: int) -> Unit:
        return (self.county_fips, self.year, self.period_index(periods_per_year))

    @property
    def county_coded(self) -> bool:
        return self.cz_type == "C"


_MAGNITUDE = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
_DAMAGE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([KMBT]?)\s*$", re.IGNORECASE)
#: A magnitude suffix with no number at all. NOAA emits this occasionally for a
#: field that is simply empty (a lone "K" in DAMAGE_CROPS, roughly one row in
#: 25,000). No digits means no amount, so it is zero. Handled explicitly rather
#: than swept into the general parse so that genuinely malformed values keep
#: raising.
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


def is_damaging(event: StormEvent, contract: Contract) -> bool:
    """Apply the contract's pre-registered damage definition."""
    if event.damage_property_usd >= contract.damage_property_usd_min:
        return True
    if contract.damage_count_casualties and (event.injuries > 0 or event.deaths > 0):
        return True
    return False


def county_fips(state_fips: str, cz_fips: str) -> str:
    """Storm Events stores an unpadded within-state county code; join needs both."""
    return f"{int(state_fips):02d}{int(cz_fips):03d}"


@dataclass(frozen=True)
class Panel:
    """A complete, dense region x year x period panel with binary labels.

    `units` and `labels` are parallel and sorted, so the digest below is a
    stable fingerprint of exactly what a model was shown or scored against.
    """

    units: tuple[Unit, ...]
    labels: tuple[int, ...]
    hazard: str
    scope: str
    period: str

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
    def regions(self) -> tuple[str, ...]:
        return tuple(sorted({u[0] for u in self.units}))

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
            scope=self.scope,
            period=self.period,
        )

    def _header(self) -> bytes:
        return f"{self.hazard}|{self.scope}|{self.period}\n".encode()

    def digest(self) -> str:
        """Content hash over units *and* labels. Identifies an exact dataset."""
        h = hashlib.sha256()
        h.update(self._header())
        for (region, year, period), label in zip(self.units, self.labels):
            h.update(f"{region}|{year}|{period}|{label}\n".encode())
        return h.hexdigest()[:16]

    def units_digest(self) -> str:
        """Content hash over units only — what a model may see at predict time."""
        h = hashlib.sha256()
        h.update(self._header())
        for region, year, period in self.units:
            h.update(f"{region}|{year}|{period}\n".encode())
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
    regions: Sequence[str],
    years: Sequence[int],
    contract: Contract,
) -> Panel:
    """Cross regions x years x periods, then mark cells with a damaging event.

    `events` should already be filtered to the scope's states; hazard filtering
    happens here so the caller cannot accidentally pass a different event-type
    set than the contract declares.
    """
    event_types = set(contract.event_types)
    year_set = set(years)
    region_set = set(regions)
    ppy = contract.periods_per_year

    positive: set[Unit] = set()
    for event in events:
        if event.event_type not in event_types:
            continue
        if not event.county_coded:  # zone-coded rows do not join to regions
            continue
        if event.year not in year_set or event.county_fips not in region_set:
            continue
        if is_damaging(event, contract):
            positive.add(event.unit_for(ppy))

    units: list[Unit] = []
    labels: list[int] = []
    for region in sorted(region_set):
        for year in sorted(year_set):
            for period in range(1, ppy + 1):
                unit = (region, year, period)
                units.append(unit)
                labels.append(1 if unit in positive else 0)

    return Panel(
        tuple(units),
        tuple(labels),
        hazard=contract.hazard,
        scope=contract.scope_key,
        period=contract.period,
    )


@dataclass(frozen=True)
class Diagnostics:
    """Where the hazard's events went while the panel was being built."""

    hazard: str
    n_events: int            # rows of the contract's event types, any year, any coding
    n_in_years: int          # ... within the contract's years
    n_county_coded: int      # ... and county-coded
    n_zone_coded: int        # ... but zone-coded, and therefore dropped
    n_outside_universe: int  # county-coded but not in the region universe
    n_damaging: int          # county-coded, in universe, and damaging
    n_positive_units: int    # distinct units marked positive

    @property
    def zone_share(self) -> float:
        return self.n_zone_coded / self.n_in_years if self.n_in_years else 0.0

    def format(self) -> str:
        lines = [
            f"  {self.hazard}: {self.n_in_years:,} events in the contract's years",
            f"    county-coded      {self.n_county_coded:>8,}"
            f"   ({self.n_damaging:,} damaging -> "
            f"{self.n_positive_units:,} positive units)",
            f"    zone-coded        {self.n_zone_coded:>8,}"
            "   dropped: zones do not join to regions",
            f"    outside universe  {self.n_outside_universe:>8,}",
        ]
        if self.n_in_years and self.zone_share >= 0.5:
            lines.append(
                f"    WARNING: {self.zone_share:.0%} of this hazard's events are "
                "zone-coded and were dropped. The panel under-counts it badly; "
                "see the zone crosswalk in docs/contracts.md."
            )
        return "\n".join(lines)


def diagnose(
    events: Iterable[StormEvent],
    regions: Sequence[str],
    years: Sequence[int],
    contract: Contract,
) -> Diagnostics:
    """Count what `build_panel` keeps and drops, so a thin panel is explained."""
    event_types = set(contract.event_types)
    year_set = set(years)
    region_set = set(regions)
    ppy = contract.periods_per_year
    n_events = n_in_years = n_county = n_zone = n_outside = n_damaging = 0
    positive: set[Unit] = set()
    for event in events:
        if event.event_type not in event_types:
            continue
        n_events += 1
        if event.year not in year_set:
            continue
        n_in_years += 1
        if not event.county_coded:
            n_zone += 1
            continue
        if event.county_fips not in region_set:
            n_outside += 1
            continue
        n_county += 1
        if is_damaging(event, contract):
            n_damaging += 1
            positive.add(event.unit_for(ppy))
    return Diagnostics(
        hazard=contract.hazard,
        n_events=n_events,
        n_in_years=n_in_years,
        n_county_coded=n_county,
        n_zone_coded=n_zone,
        n_outside_universe=n_outside,
        n_damaging=n_damaging,
        n_positive_units=len(positive),
    )
