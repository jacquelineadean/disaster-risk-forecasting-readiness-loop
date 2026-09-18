"""Harness-wide policy — the few things that apply to *every* contract.

Everything that defines one experiment series — the hazard, the geography, the
forecast period, what counts as a damaging event, the locked splits, and the
thresholds a model must clear — lives in a registered contract
(`readiness/contracts.py`, one JSON file per contract under `contracts/`).
Nothing hazard- or place-specific belongs here.

What does belong here is policy the harness applies regardless of contract:

* the catalogue mapping a hazard name to the NOAA Storm Events event types that
  constitute it, with a note on how each is coded in the record;
* the first year the record can be trusted;
* the leakage canary's ceilings.

Nothing in this module imports the engine or the agent. The harness must not
depend on what it is judging.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------
# Report §7 warns that many Storm Events event types only standardised in
# 1996, so earlier records are excluded rather than silently trusted. A
# contract may start later than this; it may not start earlier.

RECORD_START_YEAR = 1996

# --------------------------------------------------------------------------
# Hazard catalogue
# --------------------------------------------------------------------------
# Storm Events codes each event against either a county (CZ_TYPE "C") or an
# NWS forecast zone (CZ_TYPE "Z"). Convective hazards are county-coded; most
# broad-scale hazards — heat, tropical cyclones, winter storms, wildfire — are
# zone-coded. The `coding` field records which, so that a contract for a
# zone-coded hazard can be told, loudly, that the county-only label builder
# will drop most of its events unless it opts into the zone crosswalk.


@dataclass(frozen=True)
class Hazard:
    """One named hazard: the event types that constitute it, and how they are coded."""

    event_types: tuple[str, ...]
    coding: str  # "county" | "zone" | "mixed"
    summary: str


HAZARDS: dict[str, Hazard] = {
    "inland_flood": Hazard(
        ("Flood", "Flash Flood"),
        "mixed",
        "riverine and flash flooding; Flash Flood is county-coded, Flood is mostly so",
    ),
    "tornado": Hazard(("Tornado",), "county", "tornadoes, county-coded"),
    "hail": Hazard(("Hail",), "county", "hail, county-coded"),
    "severe_wind": Hazard(
        ("Thunderstorm Wind", "High Wind", "Strong Wind"),
        "mixed",
        "convective and synoptic wind; Thunderstorm Wind is county-coded, "
        "the rest zone-coded",
    ),
    "lightning": Hazard(("Lightning",), "county", "lightning, county-coded"),
    "tropical_cyclone": Hazard(
        ("Hurricane", "Hurricane (Typhoon)", "Tropical Storm", "Tropical Depression"),
        "zone",
        "tropical cyclones of any strength, zone-coded",
    ),
    "storm_surge": Hazard(
        ("Storm Surge/Tide",), "zone", "storm surge and tide, zone-coded"
    ),
    "coastal_flood": Hazard(("Coastal Flood",), "zone", "coastal flooding, zone-coded"),
    "heat": Hazard(
        ("Heat", "Excessive Heat"), "zone", "heat and excessive heat, zone-coded"
    ),
    "extreme_cold": Hazard(
        ("Cold/Wind Chill", "Extreme Cold/Wind Chill", "Frost/Freeze"),
        "zone",
        "cold, wind chill and freeze, zone-coded",
    ),
    "winter_storm": Hazard(
        (
            "Winter Storm",
            "Blizzard",
            "Ice Storm",
            "Heavy Snow",
            "Lake-Effect Snow",
            "Sleet",
        ),
        "zone",
        "winter storms of every kind, zone-coded",
    ),
    "drought": Hazard(("Drought",), "zone", "drought, zone-coded"),
    "wildfire": Hazard(("Wildfire",), "zone", "wildfire, zone-coded"),
    "debris_flow": Hazard(
        ("Debris Flow",), "county", "debris flows and landslides, county-coded"
    ),
    "tsunami": Hazard(("Tsunami",), "zone", "tsunami, zone-coded"),
    "avalanche": Hazard(("Avalanche",), "zone", "avalanche, zone-coded"),
    "dust_storm": Hazard(("Dust Storm",), "zone", "dust storms, zone-coded"),
}


def describe_hazards() -> str:
    width = max(len(h) for h in HAZARDS) + 2
    lines = []
    for name, hazard in HAZARDS.items():
        lines.append(
            f"  {name:<{width}}{hazard.coding:<8}{', '.join(hazard.event_types)}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Leakage canary
# --------------------------------------------------------------------------
# Report §7: "'Iterate until the score clears' is a recipe for memorizing the
# past unless holdouts are locked, thresholds pre-registered, and the test set
# touched once."
#
# The primary defence is structural — the agent cannot write to this package,
# and models never receive holdout labels. The canary is the tripwire for when
# that structure is breached by accident or design. Its ceilings are harness
# policy rather than contract terms: no contract may raise them.

CANARY_MAX_PLAUSIBLE_BSS = 0.99   # above this, skill is not credible on any hazard
CANARY_MAX_PLAUSIBLE_AUC = 0.999
CANARY_MAX_AGREEMENT = 0.995      # fraction of near-binary predictions matching truth

# --------------------------------------------------------------------------
# Exposure spot-check
# --------------------------------------------------------------------------
# Plan §3: the USA Structures join is spot-validated against county assessor
# counts in ten sampled counties. The band is wide on purpose. Assessors
# mostly publish *improved parcels*, and a parcel is not a structure: one
# parcel carries a house plus a garage, a barn and two sheds (USA Structures
# counts each footprint), while one apartment parcel carries a dozen
# buildings and a condominium tower is many parcels on one footprint. So a
# ratio of 1.5 or 0.67 between an honest structure count and an honest parcel
# count is ordinary, and a band tight enough to flatter one convention would
# fail the other. It is a declared judgement, printed beside every row's
# ratio by `readiness.exposure.spotcheck`; it is never tuned to make a row
# pass, and a row outside it does not count toward the ten.

EXPOSURE_SPOTCHECK_RATIO = (0.67, 1.5)   # (ours / assessor) low, high


# --------------------------------------------------------------------------
# Ground truth outside the United States (Phase 4)
# --------------------------------------------------------------------------
# Plan §5: the architecture does not change, the connectors do. Storm Events is
# US-only, so a pilot contract names one of two globally available records
# instead: a partner's national record (a CSV, pinned by hash, never
# redistributed) or an EM-DAT export.
#
# The value is the earliest year that source can be trusted, or `None` when
# only the contract can say — a partner's archive starts where their archive
# starts, and only they know where. `Contract.record_start_year` reads this
# table, and no split may begin before the year it returns.
#
# EM-DAT's 2000 is not a licence date: CRED's own guidance is that
# pre-2000 entries are sparse and inconsistently geocoded, so an ADM1 panel
# built from them under-counts rather than records absence.

GROUND_TRUTH_SOURCES: dict[str, int | None] = {
    "storm_events": RECORD_START_YEAR,
    "national_records": None,   # per contract, from the partner's own archive
    "emdat": 2000,
}

# --------------------------------------------------------------------------
# Hazard catalogue, globally
# --------------------------------------------------------------------------
# `HAZARDS` above maps a hazard to NOAA Storm Events event types. Outside the
# United States the same hazard names have to reach two other vocabularies:
#
# * **emdat** — strings as they appear in an EM-DAT export's `Disaster Type`
#   *or* `Disaster Subtype` column. A row matches when either column's value is
#   in the tuple, so a mapping can be written at whichever level is
#   unambiguous: `("Drought",)` is a type, `("Tropical cyclone",)` a subtype,
#   because the type `Storm` also carries tornadoes and blizzards.
# * **national_records** — the exact values a partner's `hazard` column may
#   carry, normalised (lowercased, spaces and hyphens folded to underscores)
#   before matching, so `"Flash Flood"` and `"flash-flood"` both reach
#   `flash_flood`. A partner whose vocabulary is not here does not need a code
#   change: the values are data, and the honest move is to agree a mapping with
#   them and add it in the open rather than to guess row by row.
#
# A hazard with no entry here cannot be registered outside the US at all.
# `dust_storm` and `lightning` are absent on purpose: EM-DAT records neither at
# a level that supports a region x period panel.

HAZARD_CATEGORIES: dict[str, dict[str, tuple[str, ...]]] = {
    "inland_flood": {
        "emdat": (
            "Riverine flood",
            "Flash flood",
            "Ice jam flood",
            "Flood (General)",
            "Glacial lake outburst flood",
        ),
        "national_records": (
            "inland_flood", "flood", "flash_flood", "riverine_flood", "river_flood",
        ),
    },
    "tropical_cyclone": {
        "emdat": ("Tropical cyclone",),
        "national_records": (
            "tropical_cyclone", "cyclone", "hurricane", "typhoon", "tropical_storm",
        ),
    },
    "drought": {
        "emdat": ("Drought",),
        "national_records": ("drought",),
    },
    "wildfire": {
        "emdat": (
            "Wildfire", "Forest fire", "Land fire (Brush, Bush, Pasture)",
        ),
        "national_records": ("wildfire", "forest_fire", "bushfire", "land_fire"),
    },
    "heat": {
        "emdat": ("Heat wave",),
        "national_records": ("heat", "heat_wave", "heatwave", "extreme_heat"),
    },
    "extreme_cold": {
        "emdat": ("Cold wave",),
        "national_records": ("extreme_cold", "cold_wave", "cold_snap", "frost"),
    },
    "winter_storm": {
        "emdat": ("Blizzard/Winter storm", "Severe winter conditions", "Snow/Ice"),
        "national_records": (
            "winter_storm", "blizzard", "snowstorm", "ice_storm", "heavy_snow",
        ),
    },
    "tornado": {
        "emdat": ("Tornado",),
        "national_records": ("tornado",),
    },
    "hail": {
        "emdat": ("Hail",),
        "national_records": ("hail", "hailstorm"),
    },
    "severe_wind": {
        "emdat": ("Severe weather", "Derecho", "Extra-tropical storm"),
        "national_records": (
            "severe_wind", "high_wind", "strong_wind", "windstorm", "gale",
        ),
    },
    "storm_surge": {
        "emdat": ("Storm surge",),
        "national_records": ("storm_surge", "surge", "tidal_surge"),
    },
    "coastal_flood": {
        "emdat": ("Coastal flood",),
        "national_records": ("coastal_flood", "tidal_flood", "sea_flood"),
    },
    "tsunami": {
        "emdat": ("Tsunami",),
        "national_records": ("tsunami",),
    },
    "avalanche": {
        "emdat": ("Avalanche (wet)", "Avalanche (dry)", "Avalanche", "Snow avalanche"),
        "national_records": ("avalanche", "snow_avalanche"),
    },
    "debris_flow": {
        "emdat": (
            "Debris flow", "Mudslide", "Landslide (wet)", "Lahar", "Landslide (dry)",
        ),
        "national_records": ("debris_flow", "landslide", "mudslide", "mudflow"),
    },
}


def normalise_hazard_value(raw: str) -> str:
    """A partner's `hazard` cell, as `HAZARD_CATEGORIES` spells it.

    Lowercased, trimmed, and every run of spaces, hyphens and slashes folded to
    a single underscore. Nothing else: a value this does not reach is skipped
    and counted, never guessed at.
    """
    text = str(raw).strip().lower()
    out: list[str] = []
    for ch in text:
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "_":
            out.append("_")
    return "".join(out).strip("_")


def global_hazards() -> tuple[str, ...]:
    """Catalogue hazards that have a global counterpart, in catalogue order."""
    return tuple(h for h in HAZARDS if h in HAZARD_CATEGORIES)
