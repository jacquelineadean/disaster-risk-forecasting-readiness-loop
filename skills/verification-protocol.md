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
| **May not** | edit anything under `readiness/harness/` (including its `features.py`, the harness's audited feature channel), `readiness/connectors/` or `snapshots/`, edit `readiness/contracts.py`, `readiness/config.py`, `readiness/data.py`, `readiness/verify.py`, `readiness/agent/guard.py`, or any file in `contracts/` (the integrity guard hashes every path above and every card records the digest it was scored under — see `readiness/agent/guard.py`'s `GUARDED_CODE`); read holdout labels; score `test` more than the contract's budget allows per model version |

`readiness/engine/features.py`, the proposable feature-set catalogue, is
deliberately **not** on the guarded list: the agent is meant to propose
feature sets from it by name, and the closed transform vocabulary it draws
from is what keeps a proposal safe, not a hash on the catalogue file. Every
row it produces is still built and audited by the guarded harness
(`readiness/harness/features.py`) before any fit, whatever the catalogue
says.

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

## The fleet, issuing and the brief (Phase 2)

The fleet is this protocol, per contract, nothing more. `readiness fleet
--national --queue phase2 --promote` runs the six national contracts in
turn, and each one owns its ledger and its test-touch budget: a pass on
`tornado-us` says nothing about `hail-us`, and a spent touch on one is not
a touch on another. `readiness fleet --status` reads the ledgers back —
cards, validate passes, the test card, whether Phase 1 is met — without
fitting anything; read it before proposing work on any contract.

Issuing needs a passing test card. `readiness issue MODEL -c <contract>
--period <label>` refuses (exit 2) unless the ledger holds a passing,
canary-clear test card for exactly that model, version and arguments under
the current digest; it refits through `TrainingView` and refuses if the
training or feature digest differs from the card; and it refuses a period
the pinned series do not reach. A refusal is information about the record,
never a reason to reissue from a different fit or relax a check. Nothing
you pass to `issue` can be a label, and nothing should try to be.

The brief is validated prose. `readiness brief` writes a county document
only when `readiness.cite.validate` returns no violation: every sentence
cites a claim, every claim resolves to a card, an issued file, a manifest
key or a registered guidance document, every number is a cited value, and
no warning language appears outside the fixed disclaimer. Nothing below
the county appears, and a probability of occurrence is never written as
what an event "would touch". The validator proves a number was not
invented; whether the citation supports the sentence is yours to read.

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

## The gap report and the reviews (Phase 3)

A gap report is not a forecast; it is a stress test of one facility's plan
against a scenario and the county's issued risk. The same discipline that
governs the loop governs it: never invent a number, never silently
substitute one fact for another, and let a person — here, the reviewer —
be the check nothing mechanical can be.

**Propose no rules; read the record.** `readiness/plans/rules.py` answers
each scenario question from the facility record and the risk layer alone.
There is no channel through which a hand-computed answer reaches a finding.
Every finding carries one of four statuses: `answered` (the record and the
risk layer together answer it, with a citation for each sentence),
`unanswered` (the record does not say — a finding, not a gap in the test),
`failed` (the record answers, and the answer fails the scenario's pass
condition), or `cannot_run` (a fact the rule depends on is missing).

**Fail-closed is not optional.** A facility record with no address or
coordinate field cannot fall back on a county-level number when its own
design flood elevation or wind speed is missing — the switchgear rule
returns `cannot_run`, naming the elevation-certificate guidance entry, and
there is no code path that substitutes a regional figure instead. If you
find yourself wanting to fill in a missing design intensity with something
"close enough" from the county's own risk numbers, that is exactly the
moment this rule exists for: report the `cannot_run` finding and move on.
A region-level answer to a switchgear question is worse than no answer,
because it looks like one.

**Prose still passes the five citation rules.** `readiness gap-report`
validates its output with the same `readiness.cite` rules the county brief
passes (`docs/brief.md`), over a claim set that additionally resolves
facility field paths and scenario questions. `--drafter claude` may rewrite
the wording; it may not add a fact. Every sentence the model writes must
still carry a citation marker into the same claim set, `cite.validate` runs
on its output exactly as it does on the local drafter's, and any sentence
that fails is dropped and counted on the report's provenance line rather
than shown. Numbers never come from the model.

**Blinding is not a courtesy; it is the review protocol.** A gap report is
reviewed only in its blinded form (`FACILITY-<hash6>`, `PARTNER-n` in place
of every name), and `readiness review record` binds a rating to the sha256
of that specific blinded file — change one byte of the report and every
review of the version before it is orphaned, on purpose. Real facility
files and real gap reports never enter git; there is no flag that commits
one by accident, and a test refuses any committed plan JSON that carries an
address, a latitude, a longitude, a tract, a block or a parcel key.

**A review record is an attestation, not proof.** `verify --phase 3` checks
that at least three reviews of distinct blinded facilities, rated useful or
better by a role naming "emergency manager", each name a report that still
validates with zero citation violations, that the committed case studies
still reproduce their expected findings, and that no coordinate key exists
anywhere under `plans/`. It cannot check that the facility is real or that
the reviewer is who they say — that is the one thing here that stays a
person's job, on purpose, and the check says so rather than implying more.

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
