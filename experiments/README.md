# experiments/

One directory per registered contract, created the first time `readiness loop`
or `readiness score --split test` runs against it:

```
experiments/<contract>/ledger.jsonl              the append-only experiment ledger
experiments/<contract>/ledger.jsonl.anchor.json  head hash + card count, against truncation
experiments/<contract>/test_touches.json         how often each model version touched TEST
```

All three are meant to be committed. A ledger without its anchor cannot rule
out tail truncation; a touch budget that is not committed is not a budget.

To run the loop, or spend a test touch, without appending to these committed
files — a scratch experiment, a fourth contract not ready to commit, or CI —
set `READINESS_EXPERIMENTS_DIR` to point the whole tree somewhere else:

```bash
READINESS_EXPERIMENTS_DIR=/tmp/readiness-scratch readiness loop -c tornado-ok
```

Every command that reads or writes a contract's experiment directory honours
it (see `readiness/data.py`); `harness_expected/` is unaffected — the blessed
fingerprints stay committed regardless.
