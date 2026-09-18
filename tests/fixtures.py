"""Synthetic contracts, panels and events for tests that must not touch the network.

Everything here is deliberately placeless: region ids sit under a state FIPS
that does not exist (99) and the contract's scope is a state code that does
not exist (ZZ). The harness must work for any hazard anywhere, and the tests
should not quietly depend on one real place.

The panel generator is seeded and pure-stdlib, so the fixture is identical on
every machine — the same property the real harness insists on.
"""

from __future__ import annotations

import math
import random

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
    units = tuple(
        (region_id(i), year, period)
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
