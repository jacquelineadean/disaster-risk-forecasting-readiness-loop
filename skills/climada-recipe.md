# Skill: the CLIMADA recipe

**Status: Phase 1, not yet implemented.** `readiness/engine/` ships climatologies
only. This runbook exists so that when CLIMADA is wired in, the decisions that
are expensive to reverse have already been made.

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

## The integration shape

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
