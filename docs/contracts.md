# Contracts

A contract is everything that defines one experiment series, written down
*before* any model is fitted. It is a JSON file in `contracts/`, named after
the contract, and it is the answer to "what does 'accurate enough' mean
here?" for one hazard in one place.

```
readiness register <name> --hazard <hazard> [--state XX ...] [options]
readiness contract -c <name>          # read it back, with its hash
readiness contracts                   # everything registered
```

The same harness, the same models and the same loop run against any
registered contract. Nothing about a hazard or a place lives in code.

## The forecast unit

Every contract defines the same *kind* of forecast:

> **P**(at least one damaging event of hazard *H* in region *R* during period *T*)

- *H* is a named hazard mapped to NOAA Storm Events event types.
- *R* is a US county, and the contract's **scope** says which counties: one
  state, several, or all of them.
- *T* is a month, a quarter or a year.
- "Damaging" is a property-damage threshold and/or any injury or death.

## The file

```json
{
  "name": "heat-xx",
  "version": "1.0.0",
  "description": "Damaging heat, one state, monthly, zone events expanded.",
  "hazard": "heat",
  "event_types": ["Heat", "Excessive Heat"],
  "scope": {"country": "US", "states": ["XX"]},
  "period": "month",
  "damaging": {"property_usd_min": 10000, "count_casualties": true},
  "zone_policy": "expand",
  "splits": {"train": [1996, 2015], "validate": [2016, 2020], "test": [2021, 2025]},
  "test_touch_budget": 1,
  "reference_model": "climatology-pooled",
  "thresholds": {
    "min_brier_skill_score": 0.0,
    "reliability_tolerance_pp": 0.05,
    "reliability_min_bin_count": 30,
    "min_auc": 0.7,
    "n_reliability_bins": 10
  }
}
```

| field | meaning | default |
|---|---|---|
| `name` | lowercase letters, digits, hyphens; names the file and the ledger directory | required |
| `version`, `description` | labels for humans; **not hashed** | `1.0.0`, empty |
| `hazard` | a catalogue hazard (`readiness hazards`) or a new name | required |
| `event_types` | Storm Events `EVENT_TYPE` values that constitute the hazard | the catalogue's, if the hazard is catalogued |
| `scope.country` | only `US` has connectors today | `US` |
| `scope.states` | two-letter codes; empty means every county in the country | `[]` |
| `period` | `month`, `quarter` or `year` | `quarter` |
| `damaging.property_usd_min` | property damage at or above this is damaging | `10000` |
| `damaging.count_casualties` | any injury or death is damaging | `true` |
| `zone_policy` | `drop` or `expand` — see below | `drop` |
| `splits.train/validate/test` | inclusive year ranges, contiguous, disjoint, ordered; none before 1996 | `1996-2015`, `2016-2020`, `2021-2025` |
| `test_touch_budget` | how often a model version may be scored on `test`, ever | `1` |
| `reference_model` | the forecast BSS is measured against; only `climatology-pooled` is implemented | `climatology-pooled` |
| `thresholds.min_brier_skill_score` | strictly greater than | `0.0` |
| `thresholds.reliability_tolerance_pp` | max deviation from the diagonal in any populated bin | `0.05` |
| `thresholds.reliability_min_bin_count` | a bin thinner than this is reported, not judged | `30` |
| `thresholds.min_auc` | at least | `0.7` |
| `thresholds.n_reliability_bins` | | `10` |

Validation is strict on purpose. A contract with overlapping splits, a split
that starts before the record is trustworthy, a hazard nobody can look up, or
a damage definition under which every event is "damaging" is refused at
registration, not discovered in a ledger later.

## The digest

Every field except `name`, `version` and `description` is a *criterion*, and
the criteria are hashed (`sha256`, first 16 hex digits of the canonical JSON).
The digest is stamped on every scorecard and every experiment card, and the
verdict's `contract provenance` check fails for any card whose digest is not
the current one.

That is the whole enforcement mechanism for "pre-registered". Nothing stops
you editing a contract file; but every experiment run before the edit becomes
visibly incomparable, and the ledger — hash-chained and anchored — cannot be
rewritten to hide that. Register a new name instead.

## Scope

`states: ["XX"]` gives one state's counties; `["XX", "YY"]` the union;
`[]` every county in the Census national file. The Storm Events year files
are national, so a larger scope costs no extra downloads — only a larger
panel. A national quarterly panel is roughly 3,200 counties × 30 years × 4,
about 380,000 units, and the pure-Python scorer handles it in seconds.

## Zone-coded hazards and `zone_policy`

Storm Events codes each event against either a **county** (`CZ_TYPE = C`) or
an NWS public **forecast zone** (`CZ_TYPE = Z`). Convective hazards —
tornadoes, hail, thunderstorm wind, flash floods, lightning — are county-coded.
Most broad-scale hazards are zone-coded: heat, cold, tropical cyclones, storm
surge, coastal flooding, winter storms, drought, wildfire, high wind. The
catalogue records which is which (`readiness hazards`).

A zone-coded row carries a state and a zone number, not a county, so it
cannot be joined to the county universe on its own. The contract has to
choose:

- **`drop`** (default): zone-coded rows are discarded. Exact for county-coded
  hazards; for a zone-coded hazard the panel under-counts severely, and
  `readiness panel` says so with a warning.
- **`expand`**: each zone-coded row is mapped, through the NWS zone-county
  correlation file, to *every* county in its zone, and each of those counties
  gets a positive label for that period if the event was damaging. The
  crosswalk is fetched from the NWS index, pinned in the manifest with its
  edition name, and re-used from then on.

`expand` is an approximation and is documented as one: damage reported once
for a zone is attributed to all of its counties. It is the honest choice for
zone-coded hazards, and it is hashed into the contract because it changes the
labels.

Two limits the panel diagnostics make visible rather than hide:

- **Unmapped zones.** The crosswalk is the *current* edition; zones are
  renumbered and merged over the years, so some historical events name a zone
  that no longer exists. Those events are counted as unmapped and dropped. A
  contract whose unmapped share is large gets a warning.
- **Multi-county zones.** In some states a single zone spans many counties,
  so one damaging zone event marks several region-periods positive at once.
  The `damaging -> positive units` line in the diagnostics shows the
  multiplication.

## Data version

Each contract's dataset records a data version: a hash over the pinned files
it was actually built from — the Census county file, the Storm Events year
files for its years, and the crosswalk if it expands zones. Pinning a source
for one contract therefore does not move another contract's fingerprints.

## What a contract gets

```
contracts/<name>.json                       the contract
experiments/<name>/ledger.jsonl             its append-only ledger (+ .anchor.json)
experiments/<name>/test_touches.json        its test-touch budget
harness_expected/<name>.json                its blessed baseline fingerprints
snapshots/storm_events/<fips>_<year>.jsonl  per-state extracts (shared with other contracts)
```

## Registering by hand

`readiness register` covers the common cases. For anything else — a custom
hazard, unusual splits, a different tolerance — write the JSON directly (start
from `readiness register ... --dry-run`), keep the file name equal to the
`name` field, and check it with `readiness contract -c <name>`.
