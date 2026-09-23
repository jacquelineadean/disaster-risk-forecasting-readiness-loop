"""Synthetic contracts, panels and events for tests that must not touch the network.

Everything here is deliberately placeless: region ids sit under a state FIPS
that does not exist (99) and the contract's scope is a state code that does
not exist (ZZ). The harness must work for any hazard anywhere, and the tests
should not quietly depend on one real place.

The panel generator is seeded and pure-stdlib, so the fixture is identical on
every machine — the same property the real harness insists on.
"""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
import random

from readiness import contracts as contracts_mod
from readiness.config import HAZARD_CATEGORIES
from readiness.connectors import national_records
from readiness.contracts import Contract
from readiness.harness.labels import Panel, StormEvent

STATE_FIPS = "99"


def make_contract(**overrides) -> Contract:
    """A valid contract with documented defaults; override any spec field."""
    spec: dict = {
        "name": "test-hazard",
        "version": "1.0.0",
        "description": "synthetic test contract",
        "hazard": "inland_flood",
        "scope": {"country": "US", "states": ["ZZ"]},
        "period": "quarter",
        "damaging": {"property_usd_min": 10_000.0, "count_casualties": True},
        "splits": {"train": [1996, 2015], "validate": [2016, 2020], "test": [2021, 2025]},
        "test_touch_budget": 1,
        "reference_model": "climatology-pooled",
        "thresholds": {
            "min_brier_skill_score": 0.0,
            "reliability_tolerance_pp": 0.05,
            "reliability_min_bin_count": 30,
            "min_auc": 0.70,
            "n_reliability_bins": 10,
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(spec.get(key), dict):
            spec[key] = {**spec[key], **value}
        else:
            spec[key] = value
    return Contract.from_spec(spec)


def region_id(i: int) -> str:
    return f"{STATE_FIPS}{i * 2 + 1:03d}"


def make_panel(
    *,
    contract: Contract | None = None,
    n_regions: int = 12,
    years: tuple[int, ...] | None = None,
    seed: int = 20260805,
    seasonal: bool = True,
    rare: bool = False,
) -> Panel:
    """A dense region x year x period panel with a seasonal, spatial signal.

    The middle of the year is wetter and low-numbered regions are wetter, so a
    seasonal climatology genuinely has something to find — otherwise tests of
    "does the harness detect skill" would be testing noise. `rare` scales the
    whole thing down to a base rate well under 1%, the regime of hazards such
    as tropical cyclones at monthly resolution.
    """
    contract = contract or make_contract()
    ppy = contract.periods_per_year
    if years is None:
        years = contract.all_years()
    rng = random.Random(seed)
    units: list[tuple[str, int, int]] = []
    labels: list[int] = []
    scale = 0.02 if rare else 1.0

    for i in range(n_regions):
        region = region_id(i)
        region_effect = (0.40 * 0.88**i if seasonal else 0.12) * scale
        for year in years:
            for period in range(1, ppy + 1):
                # A cosine annual cycle peaking mid-year, whatever the period
                # length: quarters see roughly 0.6x / 1.4x, months 0.4x / 1.6x.
                phase = (period - 0.5) / ppy  # 0..1 through the year
                season = (1.0 - 0.6 * math.cos(2 * math.pi * phase)) if seasonal else 1.0
                p = max(0.0002 if rare else 0.01, min(0.95, region_effect * season))
                units.append((region, year, period))
                labels.append(1 if rng.random() < p else 0)

    return Panel(
        tuple(units),
        tuple(labels),
        hazard=contract.hazard,
        scope=contract.scope_key,
        period=contract.period,
    )


def make_event(
    *,
    event_type: str = "Flood",
    year: int = 2010,
    month: int = 6,
    county: str = "99003",
    damage: float = 0.0,
    injuries: int = 0,
    deaths: int = 0,
    cz_type: str = "C",
) -> StormEvent:
    return StormEvent(
        event_id="1",
        year=year,
        month=month,
        event_type=event_type,
        state_fips=county[:2],
        cz_type=cz_type,
        cz_fips=county[2:],
        county_fips=county,
        injuries=injuries,
        deaths=deaths,
        damage_property_usd=damage,
        damage_crops_usd=0.0,
    )


def make_signal_panel(
    contract: Contract,
    series_source,
    n_regions: int = 12,
    seed: int = 20260916,
    *,
    intercept: float = -2.0,
    slope: float = 1.5,
    region_id_of=None,
) -> Panel:
    """A dense panel whose labels follow the source's antecedent precipitation.

    Each unit's log-odds rise with the trailing three-month `precip_mm` sum
    the harness itself would compute for that unit (same spec, same cutoff,
    same transform, via `readiness.harness.features`), standardised over the
    panel. A feature model then has real, harness-visible signal to find, and
    "does the model beat climatology" is a test of the model rather than of
    noise. Units with no window (NaN) get the intercept alone.
    """
    from readiness.harness import features as F

    ppy = contract.periods_per_year
    # `region_id_of` names the regions: US-style FIPS by default, geoBoundaries
    # shapeIDs for a pilot. The panel's arithmetic does not care which, which
    # is the property the pilot tests lean on.
    region_id_of = region_id_of or region_id
    units = tuple(
        (region_id_of(i), year, period)
        for i in range(n_regions)
        for year in contract.all_years()
        for period in range(1, ppy + 1)
    )
    spec = F.FeatureSpec("precip_3m", series_source.name, "precip_mm", "trailing_sum", 3)
    frame = F.build_frame((spec,), {series_source.name: series_source}, units, ppy)
    values = [frame.row(u)[0] for u in units]
    present = [v for v in values if not math.isnan(v)]
    # `math.fsum`, like the harness: the builtin `sum` over floats differs
    # between CPython 3.10 and 3.12, and a fixture whose labels depend on the
    # interpreter cannot back a cross-interpreter fingerprint.
    mean = math.fsum(present) / len(present)
    std = math.sqrt(math.fsum((v - mean) ** 2 for v in present) / len(present)) or 1.0

    rng = random.Random(seed)
    labels = []
    for v in values:
        z = intercept + (0.0 if math.isnan(v) else slope * (v - mean) / std)
        p = 1.0 / (1.0 + math.exp(-z))
        labels.append(1 if rng.random() < p else 0)
    return Panel(
        units, tuple(labels), hazard=contract.hazard, scope=contract.scope_key,
        period=contract.period,
    )


# ---------------------------------------------------------------------------
# Phase 4: the same fixtures, somewhere that is not the United States
# ---------------------------------------------------------------------------
# `ZZ` and `ZY` are user-assigned ISO 3166-1 codes, so they can never collide
# with a real country — the same trick as state FIPS 99 above. The harness must
# work for any hazard anywhere, and a pilot fixture that quietly borrowed a real
# country's boundaries would be testing that country, not the loop.

PILOT_COUNTRY = "ZZ"

#: The geoBoundaries release the pilot fixtures name. A literal: the point of
#: the field is that a panel is only reproducible against a named release.
PILOT_RELEASE = "gbOpen 6.0.0"

#: Splits that start after the fixture record does (see `make_pilot_contract`).
PILOT_SPLITS = {"train": [2005, 2014], "validate": [2015, 2019], "test": [2020, 2024]}


def shape_id(i: int, country: str = PILOT_COUNTRY, admin_level: str = "ADM1") -> str:
    """A geoBoundaries-shaped id: `ZZ-ADM1-003`."""
    return f"{country.upper()}-{admin_level.upper()}-{i + 1:03d}"


def shape_name(i: int) -> str:
    return f"Region {i + 1}"


def make_pilot_contract(
    *,
    name: str = "flood-zz",
    hazard: str = "inland_flood",
    country: str = PILOT_COUNTRY,
    source: str = "national_records",
    file: str = "zz_records.csv",
    sha256: str = "0" * 64,
    record_start_year: int = 2005,
    admin_level: str = "ADM1",
    release: str = PILOT_RELEASE,
    **overrides,
) -> Contract:
    """A valid contract scored outside the US, with everything a pilot must declare.

    `sha256` defaults to a placeholder because a records file cannot be hashed
    before it is written: write it with `make_records_csv`, which returns the
    hash, then build the contract again with it.
    """
    ground_truth, regions = contracts_mod.pilot_sources(
        source=source,
        file=file,
        sha256=sha256,
        record_start_year=record_start_year,
        admin_level=admin_level,
        release=release,
    )
    spec: dict = {
        "name": name,
        "version": "1.0.0",
        "description": "synthetic pilot contract",
        "hazard": hazard,
        "scope": {"country": country, "states": []},
        "period": "quarter",
        "damaging": {"property_usd_min": 10_000.0, "count_casualties": True},
        "splits": dict(PILOT_SPLITS),
        "test_touch_budget": 1,
        "reference_model": "climatology-pooled",
        "thresholds": {
            "min_brier_skill_score": 0.0,
            "reliability_tolerance_pp": 0.05,
            "reliability_min_bin_count": 30,
            "min_auc": 0.70,
            "n_reliability_bins": 10,
        },
        "ground_truth": ground_truth,
        "regions": regions,
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(spec.get(key), dict):
            spec[key] = {**spec[key], **value}
        else:
            spec[key] = value
    return Contract.from_spec(spec)


def make_geojson(
    country: str = PILOT_COUNTRY, admin_level: str = "ADM1", n_regions: int = 4
) -> bytes:
    """A gbOpen-shaped FeatureCollection of `n_regions` unit squares.

    Region *i* is the square with its south-west corner at
    `(lon = i, lat = i)`, so its vertex-mean centroid is exactly
    `(i + 0.5, i + 0.5)` and a test can assert the arithmetic rather than
    approximate it. The ring closes on its first vertex, as GeoJSON requires,
    which is the duplicate the centroid has to drop.
    """
    features = []
    for i in range(n_regions):
        lo = float(i)
        ring = [
            [lo, lo], [lo + 1.0, lo], [lo + 1.0, lo + 1.0], [lo, lo + 1.0], [lo, lo]
        ]
        features.append({
            "type": "Feature",
            "properties": {
                "shapeName": shape_name(i),
                "shapeISO": "",
                "shapeID": shape_id(i, country, admin_level),
                "shapeGroup": country.upper(),
                "shapeType": admin_level.upper(),
            },
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })
    return json.dumps(
        {"type": "FeatureCollection", "features": features}, indent=1
    ).encode()


def record_row(**over) -> dict:
    """One partner-records row, every column present."""
    row = {
        "event_id": "E1",
        "start_date": "2006-08-04",
        "region_id": shape_id(0),
        "hazard": "flood",
        "deaths": "0",
        "injured": "0",
        "damage_usd": "0",
        "source": "National Disaster Management Agency",
    }
    row.update({k: str(v) for k, v in over.items()})
    return row


def make_records_csv(
    path, contract: Contract, *events: dict, record_start_year: int | None = None
) -> str:
    """Write a partner records CSV for `contract` and return its sha256.

    The hash is what the contract pins, and it cannot be known before the file
    exists — so the fixture returns it and the caller builds the contract it
    actually means:

        draft = make_pilot_contract()
        sha = make_records_csv(path, draft, record_row(...))
        contract = make_pilot_contract(sha256=sha)

    Each event is a `record_row()`-shaped mapping; anything not given takes the
    row default, including a `hazard` value this contract's hazard accepts.
    """
    path = pathlib.Path(path)
    default_hazard = HAZARD_CATEGORIES[contract.hazard]["national_records"][0]
    start = (
        record_start_year
        if record_start_year is not None
        else contract.record_start_year
    )
    lines = [f"# record_start_year: {start}", ",".join(national_records.COLUMNS)]
    for event in events or (record_row(),):
        given = {k: str(v) for k, v in event.items()}
        row = {**record_row(hazard=default_hazard), **given}
        lines.append(",".join(row[c] for c in national_records.COLUMNS))
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = ("\n".join(lines) + "\n").encode()
    path.write_bytes(blob)
    return hashlib.sha256(blob).hexdigest()
