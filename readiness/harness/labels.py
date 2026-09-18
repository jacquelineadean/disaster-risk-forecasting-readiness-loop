"""Region-period labels: the ground truth the loop backtests against.

The forecast unit (report §5) is:

    at least one damaging event of hazard H in region R during period T

where the hazard, the regions and the period are all set by the contract. For
the NOAA Storm Events ground truth, regions are US counties and the period is
a month, a quarter or a year. For a pilot outside the US the same three come
from a partner's national record or an EM-DAT export over geoBoundaries ADM1 or
ADM2 units — a `RecordEvent` rather than a `StormEvent`, walked by the same
code, judged by the same pre-registered damage definition.

Two things make this harder than it looks, and both are handled here:

* **The panel must be complete.** Storm Events only records events. If you build
  labels from the event table alone you get a dataset of nothing but positives
  and a base rate of 1.0. The region universe comes from the Census, and every
  (region, year, period) cell that saw no damaging event is an explicit zero.

* **"Damaging" must be fixed in advance.** The threshold lives in the contract,
  not here, so it cannot drift while someone is iterating on a model.

A third thing is handled explicitly because Storm Events forces the choice:
many hazards are coded against NWS forecast zones rather than counties, and a
zone-coded row does not join to a county universe on its own. The contract's
`zone_policy` decides. Under `drop` such rows are discarded; under `expand`
each is mapped, through the NWS zone-county crosswalk, to every county in its
zone. Either way `diagnose()` counts exactly what happened — dropped, expanded,
or unmappable because the zone is not in the current crosswalk edition — so a
thin panel is explained, not silently handed a base rate of zero.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence

from readiness.connectors.nws_zones import Crosswalk
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


@dataclass(frozen=True)
class RecordEvent:
    """One event from a ground truth that names its regions directly.

    The generic counterpart of `StormEvent`, and what the Phase 4 connectors
    produce: a partner's national record (`connectors.national_records`) or an
    EM-DAT export (`connectors.emdat`). Two differences from a Storm Events
    row, both of which make the panel walk *simpler* rather than special:

    * the hazard was already matched to the contract by the connector, through
      `config.HAZARD_CATEGORIES`, which also counted what it could not match —
      so there is no event-type filter left to apply here; and
    * `region_ids` are geoBoundaries shapeIDs the record itself names (an
      EM-DAT row can list several admin units for one flood), so nothing is
      zone-coded and `zone_policy` has nothing to do. The contract validator
      insists it is `drop` outside the US for exactly that reason.

    The damage fields are deliberately the same names `is_damaging` reads on a
    `StormEvent`: one pre-registered damage definition, applied identically
    wherever the record came from. There is no "damaging by inclusion"
    convention — an event listed in a national archive still has to clear the
    contract's threshold.
    """

    event_id: str
    year: int
    month: int
    hazard: str
    region_ids: tuple[str, ...]
    injuries: int
    deaths: int
    damage_property_usd: float
    damage_crops_usd: float = 0.0

    def __post_init__(self) -> None:
        """The three things `_walk_events` assumes and cannot check cheaply.

        `_walk_events` turns a month into `period_index`, and `_dense_panel`
        only materialises periods in `1..periods_per_year` — so a month outside
        1..12 would be counted as a positive unit that is not in the panel,
        contradicting the walk's own invariant that the positive-unit count is
        the count of ones in the panel by construction. Both connectors already
        satisfy all three; this is where the next one finds out.
        """
        if not 1 <= self.month <= 12:
            raise ValueError(
                f"record event {self.event_id!r}: month {self.month} is not 1-12; "
                "a period outside the year would be counted as positive and then "
                "not appear in the panel"
            )
        if self.year <= 0:
            raise ValueError(
                f"record event {self.event_id!r}: year {self.year} is not a year"
            )
        if not self.region_ids:
            raise ValueError(
                f"record event {self.event_id!r} names no region; a record event "
                "carries its own region ids, and an event in no region is a row "
                "the connector should have counted and dropped"
            )

    def period_index(self, periods_per_year: int) -> int:
        """1-based period of the year: month 8 is quarter 3, half 2, month 8, year 1."""
        return (self.month - 1) * periods_per_year // 12 + 1

    def unit_for(self, periods_per_year: int) -> Unit:
        """The first region's unit. An event naming several marks each; see
        `_regions_hit`, which is what the panel walk uses."""
        return (self.region_ids[0], self.year, self.period_index(periods_per_year))

    @property
    def county_coded(self) -> bool:
        """True: a record event names its regions, so it is never zone-coded."""
        return True


#: Either ground truth's event. `is_damaging`, `_regions_hit` and the panel
#: walk take both; nothing else in the harness distinguishes them.
Event = StormEvent | RecordEvent


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


def is_damaging(event: Event, contract: Contract) -> bool:
    """Apply the contract's pre-registered damage definition, to either record.

    The fields it reads — property damage, injuries, deaths — are named the
    same on a `StormEvent` and a `RecordEvent`, so the definition a contract
    pre-registered in the US is the definition a pilot is judged by.
    """
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


def _require_crosswalk(contract: Contract, crosswalk: Crosswalk | None) -> None:
    if contract.zone_policy == "expand" and crosswalk is None:
        raise ValueError(
            f"contract {contract.name!r} expands zone-coded events but no zone "
            "crosswalk was supplied; refusing to build a panel that silently "
            "drops them instead"
        )


def _regions_hit(
    event: Event, contract: Contract, crosswalk: Crosswalk | None
) -> tuple[str, ...] | None:
    """Which regions an event lands in, or None if it is zone-coded and dropped.

    Returns an empty tuple for a zone-coded event whose zone the crosswalk does
    not know (unmapped) — distinct from None so the diagnostics can count it.

    A `RecordEvent` names its regions, so it short-circuits: a partner's row
    or an EM-DAT admin unit is already a geoBoundaries id (the connector did
    the crosswalk, and counted what it could not map).
    """
    if isinstance(event, RecordEvent):
        return event.region_ids
    if event.county_coded:
        return (event.county_fips,)
    if contract.zone_policy != "expand":
        return None
    assert crosswalk is not None
    return crosswalk.counties_for(event.state_fips, event.cz_fips)


@dataclass(frozen=True)
class Diagnostics:
    """Where the hazard's events went while the panel was being built."""

    hazard: str
    zone_policy: str
    n_events: int            # rows of the contract's event types, any year, any coding
    n_in_years: int          # ... within the contract's years
    n_county_coded: int      # ... and county-coded, in the region universe
    n_zone_coded: int        # ... but zone-coded
    n_zone_expanded: int     # zone-coded rows mapped to >= 1 region (expand only)
    n_zone_unmapped: int     # zone-coded rows whose zone the crosswalk lacks (expand)
    n_outside_universe: int  # mapped to a county outside the region universe
    n_damaging: int          # in universe and damaging (a zone row counts once)
    n_positive_units: int    # distinct units marked positive
    crosswalk_edition: str = ""
    #: Rows of a pilot's ground truth whose hazard value is not one of this
    #: hazard's (`config.HAZARD_CATEGORIES`) — counted by the connector that
    #: read them and carried here so `readiness panel` can print it. Zero on
    #: every US path, which is what keeps that path byte-identical.
    n_skipped_hazard: int = 0
    #: Rows of a pilot's ground truth whose admin units the committed crosswalk
    #: could not place in the region universe (`emdat.Records.n_rows_unmapped`;
    #: a partner file names shapeIDs directly and passes 0). A large count is a
    #: finding about the crosswalk, not a rounding error, so it is printed
    #: rather than left in a transient progress line. Zero on every US path.
    n_skipped_region: int = 0
    #: Regions an event named that are outside the universe, on events that
    #: still had at least one region inside it — those rows are *not* counted
    #: by `n_outside_universe`, which only counts rows that lost every region.
    #: A crosswalk written against a different geoBoundaries release loses
    #: regions this way, one at a time, and used to do it silently.
    n_regions_outside: int = 0
    #: What the directly-coded rows are coded against, for the printed line
    #: only: "county" for Storm Events, "ADM1"/"ADM2" for a pilot. The default
    #: keeps every US rendering byte-for-byte what it was.
    region_coding: str = "county"

    @property
    def zone_share(self) -> float:
        return self.n_zone_coded / self.n_in_years if self.n_in_years else 0.0

    @property
    def unmapped_share(self) -> float:
        return self.n_zone_unmapped / self.n_zone_coded if self.n_zone_coded else 0.0

    def format(self) -> str:
        lines = [
            f"  {self.hazard}: {self.n_in_years:,} events in the contract's years",
            f"    {self.region_coding + '-coded':<18}{self.n_county_coded:>8,}",
        ]
        if self.zone_policy == "expand":
            lines.append(
                f"    zone-coded        {self.n_zone_coded:>8,}"
                f"   {self.n_zone_expanded:,} expanded via NWS crosswalk "
                f"{self.crosswalk_edition or ''}".rstrip()
                + f", {self.n_zone_unmapped:,} unmapped (zone not in this edition)"
            )
        elif self.region_coding == "county":
            lines.append(
                f"    zone-coded        {self.n_zone_coded:>8,}"
                "   dropped (zone_policy: drop)"
            )
        if self.n_skipped_hazard:
            lines.append(
                f"    other hazards     {self.n_skipped_hazard:>8,}"
                "   rows of another hazard in the same records file"
            )
        if self.n_skipped_region:
            lines.append(
                f"    unplaced rows     {self.n_skipped_region:>8,}"
                "   rows whose admin units the crosswalk could not place"
            )
        lines.append(f"    outside universe  {self.n_outside_universe:>8,}")
        if self.n_regions_outside:
            lines.append(
                f"    regions dropped   {self.n_regions_outside:>8,}"
                "   named regions outside the universe on rows that had one inside"
            )
        lines.append(
            f"    damaging          {self.n_damaging:>8,}"
            f"   -> {self.n_positive_units:,} positive units"
        )
        if self.n_in_years and self.zone_share >= 0.5 and self.zone_policy != "expand":
            lines.append(
                f"    WARNING: {self.zone_share:.0%} of this hazard's events are "
                "zone-coded and were dropped. The panel under-counts it badly; "
                "register the contract with zone_policy 'expand' (docs/contracts.md)."
            )
        if self.n_zone_coded and self.unmapped_share >= 0.25:
            lines.append(
                f"    WARNING: {self.unmapped_share:.0%} of zone-coded events name a "
                "zone the current crosswalk edition does not list. Zones were "
                "renumbered; those events are lost, not mislabelled."
            )
        return "\n".join(lines)


def _hazard_matches(event: Event, event_types: set[str]) -> bool:
    """Whether an event is one of the contract's.

    A `StormEvent` carries a Storm Events EVENT_TYPE and has to be one the
    contract lists. A `RecordEvent` reached the walk through a connector that
    already matched it to the contract's hazard through
    `config.HAZARD_CATEGORIES` and counted the rows it rejected, so it is in by
    construction — the filter is applied once, where the vocabulary lives.
    """
    if isinstance(event, RecordEvent):
        return True
    return event.event_type in event_types


def _walk_events(
    events: Iterable[Event],
    regions: Sequence[str],
    years: Sequence[int],
    contract: Contract,
    crosswalk: Crosswalk | None,
    n_skipped_hazard: int = 0,
    n_skipped_region: int = 0,
) -> tuple[set[Unit], Diagnostics]:
    """One pass over the events: the units to mark positive, and where every row went.

    `build_panel` and `diagnose` are projections of this walk. They used to be
    two copies of the same filter chain, which is how a panel and its
    explanation drift apart — and how an iterator of events, walked once by the
    first, arrives empty at the second. One walk means the positive-unit count
    on the diagnostics is the count of ones in the panel by construction.
    """
    _require_crosswalk(contract, crosswalk)
    event_types = set(contract.event_types)
    year_set = set(years)
    region_set = set(regions)
    ppy = contract.periods_per_year
    n_events = n_in_years = n_county = n_zone = n_expanded = n_unmapped = 0
    n_outside = n_damaging = n_regions_outside = 0
    positive: set[Unit] = set()
    for event in events:
        if not _hazard_matches(event, event_types):
            continue
        n_events += 1
        if event.year not in year_set:
            continue
        n_in_years += 1
        hit = _regions_hit(event, contract, crosswalk)
        if not event.county_coded:
            n_zone += 1
            if hit is None:
                continue  # dropped by policy
            if not hit:
                n_unmapped += 1
                continue
            n_expanded += 1
        in_universe = [r for r in hit if r in region_set]
        if not in_universe:
            n_outside += 1
            continue
        # Regions the event named that this universe does not hold, on a row
        # that still landed somewhere. `n_outside_universe` counts rows that
        # lost *every* region; these lose some, which is what a crosswalk
        # written against another boundary release does.
        n_regions_outside += len(hit) - len(in_universe)
        if event.county_coded:
            n_county += 1
        if is_damaging(event, contract):
            n_damaging += 1
            period = event.period_index(ppy)
            for region in in_universe:
                positive.add((region, event.year, period))
    diagnostics = Diagnostics(
        hazard=contract.hazard,
        zone_policy=contract.zone_policy,
        n_events=n_events,
        n_in_years=n_in_years,
        n_county_coded=n_county,
        n_zone_coded=n_zone,
        n_zone_expanded=n_expanded,
        n_zone_unmapped=n_unmapped,
        n_outside_universe=n_outside,
        n_damaging=n_damaging,
        n_positive_units=len(positive),
        crosswalk_edition=crosswalk.edition if crosswalk is not None else "",
        n_skipped_hazard=n_skipped_hazard,
        n_skipped_region=n_skipped_region,
        n_regions_outside=n_regions_outside,
        region_coding=(
            contract.admin_level if contract.regions_source != "census" else "county"
        ),
    )
    return positive, diagnostics


def _dense_panel(
    positive: set[Unit], regions: Sequence[str], years: Sequence[int], contract: Contract
) -> Panel:
    """Cross regions x years x periods; a cell not in `positive` is an explicit zero."""
    ppy = contract.periods_per_year
    units: list[Unit] = []
    labels: list[int] = []
    for region in sorted(set(regions)):
        for year in sorted(set(years)):
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


def panel_and_diagnostics(
    events: Iterable[Event],
    regions: Sequence[str],
    years: Sequence[int],
    contract: Contract,
    crosswalk: Crosswalk | None = None,
    *,
    n_skipped_hazard: int = 0,
    n_skipped_region: int = 0,
) -> tuple[Panel, Diagnostics]:
    """The labelled panel and the account of how it was built, from one pass.

    This is what `data.build` calls: the panel a model is scored against and
    the diagnostics `readiness panel` prints come from the same walk over the
    same events, so they cannot disagree.
    """
    positive, diagnostics = _walk_events(
        events, regions, years, contract, crosswalk,
        n_skipped_hazard, n_skipped_region,
    )
    return _dense_panel(positive, regions, years, contract), diagnostics


def build_panel(
    events: Iterable[Event],
    regions: Sequence[str],
    years: Sequence[int],
    contract: Contract,
    crosswalk: Crosswalk | None = None,
) -> Panel:
    """Cross regions x years x periods, then mark cells with a damaging event.

    `events` should already be filtered to the scope's states; hazard filtering
    happens here so the caller cannot accidentally pass a different event-type
    set than the contract declares. A contract whose `zone_policy` is `expand`
    must be given the crosswalk; this refuses to guess.
    """
    return panel_and_diagnostics(events, regions, years, contract, crosswalk)[0]


def diagnose(
    events: Iterable[Event],
    regions: Sequence[str],
    years: Sequence[int],
    contract: Contract,
    crosswalk: Crosswalk | None = None,
) -> Diagnostics:
    """Count what `build_panel` keeps and drops, so a thin panel is explained."""
    return _walk_events(events, regions, years, contract, crosswalk)[1]
