# harness_expected/

Blessed baseline fingerprints, one file per registered contract:

```
harness_expected/<contract>.json
```

Written by `readiness verify -c <contract> --bless` and checked by
`readiness verify -c <contract>`. The file records every scorecard number the
two climatology baselines produce on the contract's validate split, plus the
data version and contract digest they were produced under. If any of it moves,
the Phase 0 reproducibility criterion fails for that contract — which is the
point.
