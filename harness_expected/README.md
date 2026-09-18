# harness_expected/

Blessed baseline fingerprints, one file per registered contract:

```
harness_expected/<contract>.json
```

Written by `readiness verify -c <contract> --bless` and checked by
`readiness verify -c <contract>`. Both go through `readiness/verify.py`, which
is the single definition of what a fingerprint holds, so the file, the CLI
check and the browser sandbox's own recomputation cannot drift from each
other. For each of the two climatology baselines (`climatology-pooled`,
`climatology-seasonal`), scored on the contract's validate split, it records:

* the `REPRO_FIELDS` numbers — `n_units`, `n_positive`, `base_rate`,
  `brier_score`, `brier_score_reference`, `brier_skill_score`, `auc`,
  `sharpness`, `reliability`, `resolution`, `uncertainty`, `panel_digest`,
  `train_digest` and `contract_digest` — as they are, not rounded;
* a 16-character sha256 of the canonical (sorted, compact) JSON of the
  scorecard's reliability bins, so a bin-level difference cannot hide behind
  matching aggregates.

Once per file, not per model, it also records `_data_version` (the data
snapshot's digest) and `_contract` (the contract's own digest again, at the
top level), so the comparison also notices when the inputs or the criteria
moved, not only the scores.

If any of it moves, the Phase 0 reproducibility criterion fails for that
contract — which is the point.

The fingerprints are blessed, and checked bit-for-bit, on **CPython 3.12**,
where `sum()` uses Neumaier-compensated summation. The package still supports
3.10 and 3.11; there, the same arithmetic can differ from the blessed values
by a few ulps (on the order of 1e-16) because pre-3.12 `sum()` is not
compensated. That is why the synthetic guard test that pins these baseline
fingerprints, the committed card hashes and the contract digests compares
floats at 12 decimal places rather than for exact equality: it is checking
that the harness reproduces itself, not pinning one interpreter's rounding.
