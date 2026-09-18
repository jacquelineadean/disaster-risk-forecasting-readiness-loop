# Skill: the CLIMADA recipe

**Status: Phase 1 ships the pinned-layer seam; the subprocess model is
deferred.** CLIMADA itself is never imported by the package. A tool outside
it, [`tools/climada/run_event_set.py`](../tools/climada/run_event_set.py),
runs the event set with the `climada` extra and writes one file,
`snapshots/climada/<hazard>_<scope_key>.jsonl`; the connector
[`readiness/connectors/climada_layer.py`](../readiness/connectors/climada_layer.py)
reads it, pins its bytes in the manifest, and hands the harness a static
feature source. That file is the whole integration in Phase 1. The rest of
this runbook records the decisions that were expensive to reverse, and
[the seam](#the-pinned-layer-seam) says how the file is admitted.

Report §3E and takeaway 3:

> Don't invent physics — orchestrate it. CLIMADA (ETH Zurich, open source)
> already computes probabilistic risk as hazard × exposure × vulnerability,
> globally, on a laptop. The agent's edge is automating the
> calibrate-validate-iterate cycle around such engines, not replacing them.

## What CLIMADA is

Open-source (GPL-3.0, Python) probabilistic multi-hazard risk platform from ETH
Zurich. Event-based, globally consistent from 10 km down to ~100 m, efficient
enough to run on a laptop. Includes tropical-cyclone wind fields along synthetic
storm tracks and an adaptation cost-benefit module. EIOPA builds EU insurance
stress tools on it.

`github.com/CLIMADA-project/climada_python`

## The three decisions to make before writing code

**1. Licence boundary.** CLIMADA is GPL-3.0, and GPL is viral across a linked
work. This project's code is Apache-2.0. Keep the integration at arm's length —
a subprocess, or a separate GPL-licensed adapter package — or accept that the
integrating component becomes GPL. See [`DATA-LICENSES.md`](../DATA-LICENSES.md).
Deciding this before the first import is much cheaper than after.

**2. What CLIMADA is being asked for.** It computes *expected damage* from
hazard intensity, exposure and a vulnerability curve. The Phase 0/1 forecast
unit is *probability of at least one damaging event* in a region-period, for
whichever hazard the contract names. These are different
quantities and the conversion is a modelling choice, not a formatting one. State
which of these you are doing, on the experiment card:

- use CLIMADA's event set to derive an occurrence probability per
  region-period, or
- change the forecast unit to expected loss and register a new contract for it.

Do not blur them. A model that quietly answers a different question than the
harness is scoring will look miscalibrated for reasons no amount of tuning fixes.

**3. Where CLIMADA sits relative to the harness.** It is a *model*, and it lives
in `readiness/engine/`. It receives a `TrainingView` and returns probabilities
like anything else. It gets no special standing because it is a physical model —
it clears the same contract or it does not ship.

## The pinned-layer seam

```
tools/climada/run_event_set.py --hazard inland_flood --scope US:LA --years 1996 2015 --seed N --out snapshots/climada/
        │  (GPL-3.0 tool, `pip install climada`, never imported by the package)
        ▼
snapshots/climada/inland_flood_US:LA.jsonl
  {"event_set_years": [1996, 2015], "seed": N, "climada_version": "...", "hazard": "inland_flood"}
  {"region": "22001", "rp10": 0.8, "rp50": 1.6, "rp100": 2.1}
  ...
        │  readiness snapshot -c inland-flood-la --features climada   (pins the bytes)
        ▼
readiness.connectors.climada_layer.source(...)   static source "climada", derived_through = event_set_years[1]
        │  readiness features -c inland-flood-la --features climada   (admission verdict)
        ▼
feature set "climada-prior": climada_rp10, climada_rp50, climada_rp100
```

The layer is **admissible only when the event set ends before the
contract's first validate year**: `derived_through` is `event_set_years[1]`,
and the harness refuses a static source whose year is at or after the first
validate year (see [`docs/features.md`](../docs/features.md)). Under the
current contracts, which validate from 2016, a set built from tracks or
gauges through 2015 is admitted and one built through 2020 is refused — not
silently dropped, refused, with the reason printed by `readiness features`.
Build the set from inputs that end before the holdout, and record the seed,
the year range and the CLIMADA version in the header; the tool writes them
and the connector reads `derived_through` from the header, so the tool is
the reviewed constant.

The values in the file are the layer's own output and carry CC BY 4.0; the
tool that produced them is GPL-3.0, which is why it lives in `tools/` and
not in the package (`DATA-LICENSES.md`).

## The integration shape (deferred: the subprocess model)

```
readiness/engine/climada_glue.py
  class ClimadaFlood:
      name = "climada-flood"
      version = "0.1.0"
      def fit(self, view: TrainingView) -> None:
          # calibrate vulnerability / thresholds against TRAINING years only
      def predict(self, request: PredictionRequest) -> Sequence[float]:
          # run the event set, convert to P(>=1 damaging event | region, period)
```

Constraints that are not negotiable:

- `fit()` may read `view` and nothing else. If a CLIMADA workflow wants the full
  time series, slice it from the view — do not reach around to the panel.
- Any hazard file CLIMADA downloads goes through `readiness/connectors/`, gets a
  sha256, and lands in the manifest. An unpinned input makes the experiment
  irreproducible whether or not CLIMADA is deterministic.
- CLIMADA's own stochastic event generation must be seeded, and the seed
  recorded on the experiment card. A model whose score changes between identical
  runs cannot be compared to anything.

## What to calibrate, and against what

Calibrate the vulnerability curve and the damage threshold mapping on the
contract's **training years only**. Validate on its validate years. The test
years stay untouched until a candidate passes on validate.

The temptation specific to physical models is to justify a post-hoc parameter
change as "a better representation of the physics" rather than as a fit to the
validation set. It is both. Write it on the card as both.

## Where this is likely to disappoint

Worth writing down before anyone is invested: region-period *occurrence* may be
dominated by reporting practice rather than physics — Storm Events records what
someone reported, and a rigorous hydrodynamic model of what actually happened
does not predict what a NWS office wrote down. If CLIMADA underperforms a
seasonal climatology on this unit, that is a real result about the unit, and the
right response is to change the forecast unit (to depth, or to loss) rather than
to keep tuning the physics.
