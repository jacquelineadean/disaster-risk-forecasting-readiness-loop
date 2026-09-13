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
