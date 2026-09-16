# Skill: verification protocol

A runbook the agent reads before scoring anything. Report §5 defines what
"accurate enough" means here; this file is the operational version. Every
command below runs against one registered contract — pass it with
`-c <contract>` every time.

## The forecast unit

> At least one damaging event of the contract's hazard in region R during
> period T.

The hazard, the regions (a state, several, or the whole country), the period
(month, quarter or year) and what "damaging" means are all read from the
contract: `readiness contract -c <contract>`. Do not change any of them to make
a model look better. Changing a criterion changes the contract's hash, which
marks every prior experiment as incomparable — that is the intended cost.

## What you may and may not touch

| | |
|---|---|
| **May** | propose any model in `readiness/engine/`, choose feature *sets* from the catalogue (`readiness/engine/features.py`, `FEATURE_SETS`) by name, change hyperparameters, iterate on `validate` as often as you like |
| **May not** | edit anything under `readiness/harness/`, `readiness/connectors/` or `snapshots/`, edit `readiness/contracts.py`, `readiness/config.py`, `readiness/data.py`, `readiness/verify.py`, `readiness/agent/guard.py`, any module named `features` or any file in `contracts/` (the integrity guard hashes all of these and every card records the digest it was scored under), read holdout labels, score `test` more than the contract's budget allows per model version |

If you find yourself wanting to change a threshold because a model is close,
that is the moment the protocol exists for. Write the experiment card saying the
model missed, and move on.

## Features: audited by the harness before any fit

A Phase 1 model does not build features. It *declares* feature sets from the
catalogue, and the harness builds every row itself: it computes each unit's
cutoff (the period's first month minus the spec's lag, at least one month),
hands a closed vocabulary of transforms nothing later than that, admits or
refuses every source (nothing built from the ground truth; a static layer
must predate the first validate year; "timeless" is an allow-list), and
rebuilds the frame with the months past the cutoff poisoned to prove no
transform read them. All of that runs on every scoring call, before `fit()`,
and its findings are recorded on the card. The rules are in
[`docs/features.md`](../docs/features.md).

What that means for you:

- **Propose sets, never columns.** `--param feature_sets=era5-antecedent,terrain`
  is a proposal; a hand-built column is not, and there is no channel for one.
  You never author a source or a transform. If the catalogue lacks the
  feature you want, that is a finding for the card, not a reason to compute
  it yourself.
- **Read the refusals.** `readiness features -c <contract>` prints each
  source's admission verdict. FEMA's National Risk Index is refused under
  every current contract (its layer encodes data through 2023); the refusal
  is correct, and asking for the `nri` set is how a run makes it visible.
- **History is computed inside `fit()`,** leave-one-year-out for training
  rows, from the `TrainingView` only. Do not reach for the labels yourself.

## The metrics, and why each one is there

**Brier score** — mean squared error between the forecast probability and the
0/1 outcome. Strictly proper, so you cannot improve it by hedging toward the
base rate. Never report it alone: it is not comparable across hazards with
different base rates.

**Brier Skill Score** — `1 - BS/BS_ref`, where the reference is
`climatology-pooled` (the training base rate issued everywhere). This is the
number the contract is written in. BSS > 0 means you beat climatology. BSS = 0
means you *are* climatology.

**Reliability diagram** — forecast probability on x, observed frequency on y.
The diagonal is perfect calibration. The contract requires every populated bin
to sit within its tolerance of the diagonal (by default n ≥ 30 and ±5
percentage points). A bin thinner than that is reported but not judged, because
it cannot fail a 5-point tolerance meaningfully.

**Murphy decomposition** — `BS = reliability − resolution + uncertainty`.
Read it when a model fails: high reliability term means systematic
over/under-forecasting (fixable by recalibration); low resolution means the
model does not distinguish situations (needs a real feature, not a recalibration).

**Sharpness** — standard deviation of the forecasts. Reported because a model
that always issues the base rate is perfectly calibrated and perfectly useless.
If reliability is excellent and sharpness is near zero, you have reinvented
climatology.

**AUC** — probability a random positive outranks a random negative. Ties get
half credit, so a constant forecast scores exactly 0.5. The contract sets the
floor (0.70 by default).

## The splits

The years are the contract's. With the defaults:

```
train      1996-2015   fit here; also the 20-year climatology window
validate   2016-2020   iterate here, as often as you want
test       2021-2025   once per model version. Ever.
```

No contract may start before 1996: many Storm Events types only standardised
then (report §7), and earlier years are excluded rather than silently trusted.

## Order of operations

1. Read the contract. Then `readiness panel -c <contract>` and read the event
   coverage: if most of the hazard's events are zone-coded and dropped, the
   panel under-counts the hazard and no model will fix that.
2. Score on `validate`. `readiness score <model> -c <contract>` fits, scores
   and prints the full result — including the runs that failed — but writes
   nothing. Only `readiness loop` appends a card to the contract's ledger,
   one per candidate in its queue; that is what puts an experiment card on
   the record, including for the candidates that failed.
3. Read the reliability diagram, not just the headline.
4. Iterate.
5. When and only when a model passes on `validate` with a clear canary,
   promote it: `readiness promote <model> -c <contract> --spend-test-touch`,
   with the same arguments as the passing card. That is the **only** way to
   touch `test` — `score --split test` is refused — and it spends the touch
   and writes the test card in one step. Whatever it says is the result.
6. `readiness backtest -c <contract>` and `readiness verify -c <contract>
   --phase 1` publish and check it. A failed first touch is published too;
   the next move is a new contract, never a relaxed threshold.

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
- **A bin with a large deviation and n just under the populated threshold** —
  do not celebrate. Note it; it will become populated with more data.
- **A thin panel** — a base rate near zero usually means the hazard is
  zone-coded, not that it is rare. `readiness panel` says which.

## The canary

Every scored run is screened for leakage: implausible skill, implausible AUC,
near-binary forecasts that match the outcomes, a training-digest mismatch,
and — for a model that declares features — a feature-digest mismatch between
what the model claims it was fitted on and the frame the harness handed it.
It is a smoke alarm over the *output*, not a proof of isolation. The real
defence is structural — models receive a `TrainingView` over training years
only, and labels are fetched after `predict()` returns.

If the canary trips on a model you believe is honest, do not raise the ceiling.
Find out why it tripped.
