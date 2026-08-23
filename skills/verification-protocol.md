# Skill: verification protocol

A runbook the agent reads before scoring anything. Report §5 defines what
"accurate enough" means here; this file is the operational version.

## The forecast unit

> At least one damaging `inland_flood` event in county C during quarter Q.

`damaging` is fixed in `readiness/config.py`: property damage ≥ $10,000, or any
injury or death. Do not change it to make a model look better. Changing it
changes the contract hash, which marks every prior experiment as incomparable —
that is the intended cost.

## What you may and may not touch

| | |
|---|---|
| **May** | propose any model in `readiness/engine/`, add features, change hyperparameters, iterate on `validate` as often as you like |
| **May not** | edit anything under `readiness/harness/`, edit `readiness/config.py`, read holdout labels, score `test` more than once per model version |

If you find yourself wanting to change a threshold because a model is close,
that is the moment the protocol exists for. Write the experiment card saying the
model missed, and move on.

## The metrics, and why each one is there

**Brier score** — mean squared error between the forecast probability and the
0/1 outcome. Strictly proper, so you cannot improve it by hedging toward the
base rate. Never report it alone: it is not comparable across hazards with
different base rates.

**Brier Skill Score** — `1 - BS/BS_ref`, where the reference is
`climatology-global` (the training base rate issued everywhere). This is the
number the contract is written in. BSS > 0 means you beat climatology. BSS = 0
means you *are* climatology.

**Reliability diagram** — forecast probability on x, observed frequency on y.
The diagonal is perfect calibration. The contract requires every populated bin
(n ≥ 30) to sit within ±5 percentage points of the diagonal. A bin thinner than
that is reported but not judged, because it cannot fail a 5-point tolerance
meaningfully.

**Murphy decomposition** — `BS = reliability − resolution + uncertainty`.
Read it when a model fails: high reliability term means systematic
over/under-forecasting (fixable by recalibration); low resolution means the
model does not distinguish situations (needs a real feature, not a recalibration).

**Sharpness** — standard deviation of the forecasts. Reported because a model
that always issues the base rate is perfectly calibrated and perfectly useless.
If reliability is excellent and sharpness is near zero, you have reinvented
climatology.

**AUC** — probability a random positive outranks a random negative. Ties get
half credit, so a constant forecast scores exactly 0.5. Contract floor: 0.70.

## The splits

```
train      1996-2015   fit here; also the 20-year climatology window
validate   2016-2020   iterate here, as often as you want
test       2021-2025   once per model version. Ever.
```

Record starts at 1996 because many Storm Events types only standardised then
(report §7). Earlier years are excluded rather than silently trusted.

## Order of operations

1. Score on `validate`. Read the reliability diagram, not just the headline.
2. Write an experiment card — including for the runs that failed.
3. Iterate.
4. When and only when a model passes on `validate`, score `test` once, with
   `--spend-test-touch`. Whatever it says is the result.

If the test score is much worse than validate, that is information about how
much you overfitted validate, and it belongs on a card. It is not an invitation
to iterate further and re-test under a new version number.

## Failure modes to watch for

- **Reliability good, sharpness ~0** — you have reinvented climatology. Check
  whether BSS is also ~0.
- **AUC good, reliability bad** — a real signal, wrongly scaled. Recalibrate
  rather than discard.
- **BSS improving fast across iterations** — check the canary findings. Confirm
  the `train provenance` check actually verified a digest rather than being
  skipped.
- **A bin with a large deviation and n just under 30** — do not celebrate. Note
  it; it will become populated with more data.

## The canary

Every scored run is screened for leakage: implausible skill, implausible AUC,
near-binary forecasts that match the outcomes, and a training-digest mismatch.
It is a smoke alarm over the *output*, not a proof of isolation. The real
defence is structural — models receive a `TrainingView` over training years
only, and labels are fetched after `predict()` returns.

If the canary trips on a model you believe is honest, do not raise the ceiling.
Find out why it tripped.
