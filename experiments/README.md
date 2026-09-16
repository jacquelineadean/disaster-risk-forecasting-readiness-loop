# experiments/

One directory per registered contract:

```
experiments/<contract>/ledger.jsonl              the append-only experiment ledger
experiments/<contract>/ledger.jsonl.anchor.json  head hash + card count, against truncation
experiments/<contract>/test_touches.json         how often each model version touched TEST
```

`ledger.jsonl` and its anchor are created the first time `readiness loop` runs
against the contract. `test_touches.json` is created later, and only then: it
appears the first time a model version actually spends a test touch, which
only `readiness promote MODEL --spend-test-touch` (or `readiness loop
--promote`) can do; `score` and `loop` refuse the test split outright, so the
touch and the test card are always one step. A contract that has never touched
TEST has no `test_touches.json` at all, and that absence is itself the record.

All three, once present, are meant to be committed. A ledger without its
anchor cannot rule out tail truncation; a touch budget that is not committed
is not a budget.

To run the loop, or spend a test touch, without appending to these committed
files — a scratch experiment, a fourth contract not ready to commit, or CI —
set `READINESS_EXPERIMENTS_DIR` to point the whole tree somewhere else:

```bash
READINESS_EXPERIMENTS_DIR=/tmp/readiness-scratch readiness loop -c tornado-ok
```

Every command that reads or writes a contract's experiment directory honours
it (see `readiness/data.py`); `harness_expected/` is unaffected — the blessed
fingerprints stay committed regardless.
