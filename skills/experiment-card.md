# Skill: the experiment card

Report §4: the agent "reads the score report, writes an experiment card (what
changed, why, result), adjusts features or calibration, and reruns".

`readiness loop` writes cards, one per candidate it runs, appended to the
contract's own ledger at `experiments/<contract>/ledger.jsonl`; `readiness
promote` writes the one test card, through the same path. `readiness score`
runs the same scoring path for a single model but only prints the result —
it writes nothing. If you scored a model by hand and want it on the record,
it goes through the loop, not a manual append. The ledger is
hash-chained and anchored, so cards cannot be edited, reordered, or quietly
deleted after the fact.

## The three fields you actually write

Everything else on a card is generated. These three are yours, and they are the
reason the ledger is worth reading:

**`changed`** — what is different from the previous experiment. One concrete
thing. "Tried a better model" is not a change; "replaced the pooled rate with a
per-region, per-period frequency shrunk toward the scope-wide seasonal rate
with κ=10" is.

**`hypothesis`** — why you expected that change to help, stated so it could be
wrong. Include what result would falsify it. If you cannot say what would
falsify it, you are not running an experiment.

**`outcome`** — what actually happened, including when the hypothesis was wrong.
A ledger of only successes is a marketing document.

## Worked example

```json
{
  "changed": "Replaced the single pooled base rate with a per-region,
              per-period-of-year empirical frequency, shrunk toward the
              scope-wide seasonal rate with 10 pseudo-observations.",
  "hypothesis": "This hazard is strongly seasonal and strongly spatial in this
                 scope. Conditioning on region and period should add resolution
                 without hurting reliability. Falsified if resolution does not
                 improve, or if reliability degrades beyond the contract's
                 tolerance in any populated bin.",
  "outcome": "BSS +0.09 on validate; resolution up, reliability essentially
              unchanged. Hypothesis held."
}
```

And one that did not work — equally valuable:

```json
{
  "changed": "Issued 0.35 whenever the same region-period had an event last
              year, 0.03 otherwise.",
  "hypothesis": "Year-to-year persistence should carry signal. Falsified if
                 reliability fails, which would mean the two fixed levels are
                 simply the wrong numbers.",
  "outcome": "Failed the contract on reliability: the 0.3-0.4 bin observed 0.19.
              The signal is real (AUC 0.63) but the levels are wrong. Next:
              estimate the two levels from training data instead of fixing them."
}
```

## Generated fields

| field | meaning |
|---|---|
| `experiment_id` | `exp-NNNN`, sequential within the contract's ledger |
| `scorecard` | every metric and the reliability bins; and, for a Phase 1 card, `feature_digest`, `feature_columns` and `feature_audit` (below) |
| `verdict` | per-clause contract result, naming the contract |
| `canary` | leakage screen findings |
| `data_snapshot` | contract name and digest, data version, panel digest, hazard, scope, period, region count, year range, the manifest keys the panel was built from, `harness_digest`, `wall_clock_s` and `model_kwargs` (below); and, only when feature sources were loaded, `feature_version` and `feature_inputs` (below) |
| `contract_digest` | which contract this was judged under |
| `prev_hash` / `card_hash` | the chain |

## Kwargs and the feature audit on a Phase 1 card

Nothing was added to the card's fields — the hashes of the committed cards
are untouched — so the Phase 1 provenance lives inside `data_snapshot` and
the `scorecard`, split by which one already carries the matching shape:

- **`data_snapshot.model_kwargs`** — the constructor arguments the candidate
  was built with (`feature_sets`, `history`, `l2`, `rounds`, …). `readiness
  promote` matches the validate PASS it requires on exactly these, and
  `verify --phase 1 --replay` rebuilds the model from them.
- **`data_snapshot.feature_version`** and **`data_snapshot.feature_inputs`**
  — the manifest digest over the loaded sources' manifest keys, and the keys
  themselves. Present only when feature sources were loaded; a Phase 0 card,
  or a Phase 1 card for a model with no features, has neither.
- **`scorecard.feature_columns`** — the columns of the frame the harness
  actually handed the model. Empty when the run built no feature frame.
- **`scorecard.feature_audit`** — the harness's findings before the fit: each
  source's admission verdict, the poisoned-cutoff bound, coverage per
  column, and `clean`. A test card whose audit is not clean fails `verify
  --phase 1`, whatever its scores say.
- **`harness_digest`** — the guarded code as it stood at scoring time, so a
  guarded agent run can check every card it produced against the harness
  it began with.
- **`wall_clock_s`** — how long the fit and the screen took, in seconds, to
  three decimals. Provenance, not a criterion: the annex budgets a national
  card in minutes, and the only way to know what a fleet queue costs is to
  write down what each card cost. Like every other `data_snapshot` field it
  *is* inside `card_hash` — the card is sealed whole, so the cost is
  tamper-evident too — but no check compares it and no reproducibility
  fingerprint contains it (`verify.REPRO_FIELDS` is scorecard fields only).
  What follows is that two runs of the same queue on the same data no longer
  produce identical card hashes, because they did not take the same number
  of milliseconds. The ledgers committed here were written before this field
  existed and are unaffected; `tests/test_repro_guard.py` still re-checks
  every one of their hashes.

The scorecard also carries the frame's `feature_digest`, and the canary's
fifth finding records whether the model's declared digest matched it (that
check is skipped when the run built no feature frame at all).

## Reading the ledger

```bash
readiness ledger -c <contract>              # summary table + chain verification
readiness ledger -c <contract> --show       # full cards
readiness ledger -c <contract> --id exp-0002  # one card; --id implies --show
```

If `verify()` reports a broken chain, stop. Every score above the break is
untrustworthy, and the fix is to find out what rewrote history — not to
regenerate the ledger.
