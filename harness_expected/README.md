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

The fingerprints are blessed, and checked bit-for-bit, on **CPython 3.12**,
where `sum()` uses Neumaier-compensated summation. The package still supports
3.10 and 3.11; there, the same arithmetic can differ from the blessed values
by a few ulps (on the order of 1e-16) because pre-3.12 `sum()` is not
compensated. That is why the synthetic guard test that pins these baseline
fingerprints, the committed card hashes and the contract digests compares
floats at 12 decimal places rather than for exact equality: it is checking
that the harness reproduces itself, not pinning one interpreter's rounding.
