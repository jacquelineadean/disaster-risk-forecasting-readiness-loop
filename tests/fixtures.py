"""Synthetic panels for tests that must not touch the network.

The generator is seeded and pure-stdlib, so the fixture is identical on every
machine — the same property the real harness insists on.
"""

from __future__ import annotations

import random

from readiness.config import CONTRACT
from readiness.harness.labels import Panel, StormEvent


def make_panel(
    *,
    n_counties: int = 12,
    years: tuple[int, ...] | None = None,
    seed: int = 20260805,
    seasonal: bool = True,
) -> Panel:
    """A dense county x year x quarter panel with a seasonal, spatial signal.

    Q2/Q3 are wetter and low-numbered counties are wetter, so a county-quarter
    climatology genuinely has something to find — otherwise tests of "does the
    harness detect skill" would be testing noise.
    """
    if years is None:
        years = tuple(
            sorted(
                set(CONTRACT.train_years + CONTRACT.validate_years + CONTRACT.test_years)
            )
        )
    rng = random.Random(seed)
    units: list[tuple[str, int, int]] = []
    labels: list[int] = []

    for i in range(n_counties):
        fips = f"22{i * 2 + 1:03d}"
        county_effect = 0.30 - 0.02 * i if seasonal else 0.12
        for year in years:
            for quarter in (1, 2, 3, 4):
                season = {1: 0.6, 2: 1.4, 3: 1.5, 4: 0.7}[quarter] if seasonal else 1.0
                p = max(0.01, min(0.95, county_effect * season))
                units.append((fips, year, quarter))
                labels.append(1 if rng.random() < p else 0)

    return Panel(tuple(units), tuple(labels), hazard=CONTRACT.hazard, state=CONTRACT.state)


def make_event(
    *,
    event_type: str = "Flood",
    year: int = 2005,
    month: int = 8,
    county: str = "22071",
    damage: float = 0.0,
    injuries: int = 0,
    deaths: int = 0,
) -> StormEvent:
    return StormEvent(
        event_id="1",
        year=year,
        month=month,
        event_type=event_type,
        state_fips="22",
        cz_type="C",
        cz_fips=county[2:],
        county_fips=county,
        injuries=injuries,
        deaths=deaths,
        damage_property_usd=damage,
        damage_crops_usd=0.0,
    )
