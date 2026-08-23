"""The pre-registered contract.

Everything in this module is fixed *before* any model is fitted, and its hash
is recorded on every experiment card. If you change a threshold, a split, or the
definition of a damaging event, the contract hash changes and every prior
experiment becomes visibly incomparable. That is the point.

From the research report, §5:

    "Within an acceptable threshold" then becomes a contract, not a feeling —
    versioned in the repo before iteration begins.

Nothing here imports the engine or the agent. The harness must not depend on
what it is judging.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

CONTRACT_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# Forecast unit
# --------------------------------------------------------------------------
# Report §5: "The tractable MVP unit is binary and auditable: 'at least one
# damaging event of hazard H in county C within the next quarter,' with damage
# defined by a fixed Storm Events threshold."

#: Storm Events EVENT_TYPE values that constitute the Phase 0 hazard.
HAZARDS: dict[str, tuple[str, ...]] = {
    "inland_flood": ("Flood", "Flash Flood"),
    # Phase 2 hazards, declared here so the label builder is hazard-agnostic
    # from day one. Not scored until their own contracts are registered.
    "tornado": ("Tornado",),
    "hurricane_wind": ("Hurricane", "Hurricane (Typhoon)", "Tropical Storm"),
    "wildfire": ("Wildfire",),
    "heat": ("Heat", "Excessive Heat"),
}

PHASE0_HAZARD = "inland_flood"
PHASE0_STATE = "LA"  # Louisiana: 64 parishes, the Memorial case-study geography.

#: A Storm Events record counts as "damaging" if ANY of these hold. Fixed.
DAMAGE_PROPERTY_USD_MIN = 10_000.0
DAMAGE_COUNT_CASUALTIES = True  # any direct/indirect injury or death also counts

# --------------------------------------------------------------------------
# Locked splits
# --------------------------------------------------------------------------
# Report §4: "Lock test years (say, train <=2015, validate 2016-2020, final test
# 2021-2025, touched once)". Record start is 1996: report §7 warns that many
# event types only standardised then, so earlier records are excluded rather
# than silently trusted.

RECORD_START_YEAR = 1996

TRAIN_YEARS = tuple(range(1996, 2016))       # 20 years -> the climatology window
VALIDATE_YEARS = tuple(range(2016, 2021))    # 5 years, iterate freely
TEST_YEARS = tuple(range(2021, 2026))        # 5 years, touch once

#: How many times a given model version may be scored against TEST. Ever.
TEST_TOUCH_BUDGET = 1

# --------------------------------------------------------------------------
# Acceptance thresholds
# --------------------------------------------------------------------------
# Report §5: "Brier Skill Score > 0 against a 20-year climatological baseline
# for every hazard [...]; reliability within +/-5 percentage points in every
# populated bin; AUC >= 0.7."

#: The forecast every candidate is scored against. Skill is relative or it is
#: meaningless: raw Brier scores are not comparable across hazards.
REFERENCE_MODEL = "climatology-global"

MIN_BRIER_SKILL_SCORE = 0.0          # strictly greater than
RELIABILITY_TOLERANCE_PP = 0.05      # 5 percentage points
RELIABILITY_MIN_BIN_COUNT = 30       # bins thinner than this are not "populated"
MIN_AUC = 0.70
N_RELIABILITY_BINS = 10

# --------------------------------------------------------------------------
# Leakage canary
# --------------------------------------------------------------------------
# Report §7: "'Iterate until the score clears' is a recipe for memorizing the
# past unless holdouts are locked, thresholds pre-registered, and the test set
# touched once."
#
# The primary defence is structural — the agent cannot write to this package,
# and models never receive holdout labels. The canary is the tripwire for when
# that structure is breached by accident or design.

CANARY_MAX_PLAUSIBLE_BSS = 0.99   # above this, skill is not credible for this task
CANARY_MAX_PLAUSIBLE_AUC = 0.999
CANARY_MAX_AGREEMENT = 0.995      # fraction of near-binary predictions matching truth


@dataclass(frozen=True)
class Contract:
    """The frozen acceptance criteria, hashed onto every experiment card."""

    version: str = CONTRACT_VERSION
    hazard: str = PHASE0_HAZARD
    state: str = PHASE0_STATE
    event_types: tuple[str, ...] = field(
        default_factory=lambda: HAZARDS[PHASE0_HAZARD]
    )
    damage_property_usd_min: float = DAMAGE_PROPERTY_USD_MIN
    damage_count_casualties: bool = DAMAGE_COUNT_CASUALTIES
    train_years: tuple[int, ...] = TRAIN_YEARS
    validate_years: tuple[int, ...] = VALIDATE_YEARS
    test_years: tuple[int, ...] = TEST_YEARS
    test_touch_budget: int = TEST_TOUCH_BUDGET
    reference_model: str = REFERENCE_MODEL
    min_brier_skill_score: float = MIN_BRIER_SKILL_SCORE
    reliability_tolerance_pp: float = RELIABILITY_TOLERANCE_PP
    reliability_min_bin_count: int = RELIABILITY_MIN_BIN_COUNT
    min_auc: float = MIN_AUC
    n_reliability_bins: int = N_RELIABILITY_BINS

    def to_dict(self) -> dict:
        return asdict(self)

    def canonical_json(self) -> str:
        """Stable serialisation — the thing that actually gets hashed."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()[:16]


CONTRACT = Contract()


def describe() -> str:
    c = CONTRACT
    return "\n".join(
        [
            f"contract        {c.version}  (sha256:{c.digest()})",
            f"hazard          {c.hazard}  {list(c.event_types)}",
            f"geography       {c.state}, county x quarter",
            f"damaging event  property >= ${c.damage_property_usd_min:,.0f}"
            + (" or any casualty" if c.damage_count_casualties else ""),
            f"train           {c.train_years[0]}-{c.train_years[-1]}"
            f"  ({len(c.train_years)}y)",
            f"validate        {c.validate_years[0]}-{c.validate_years[-1]}"
            f"  ({len(c.validate_years)}y)",
            f"test            {c.test_years[0]}-{c.test_years[-1]}"
            f"  ({len(c.test_years)}y, {c.test_touch_budget} touch)",
            f"reference       {c.reference_model}",
            f"passes when     BSS > {c.min_brier_skill_score}"
            f"  |  reliability within +/-{c.reliability_tolerance_pp:.0%}"
            f" per populated bin (n >= {c.reliability_min_bin_count})"
            f"  |  AUC >= {c.min_auc}",
        ]
    )
