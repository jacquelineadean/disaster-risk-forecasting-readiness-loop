# Skill: the experiment card

Report §4: the agent "reads the score report, writes an experiment card (what
changed, why, result), adjusts features or calibration, and reruns".

Every scored run produces a card, appended to the contract's own ledger at
`experiments/<contract>/ledger.jsonl`. The ledger is hash-chained and anchored,
so cards cannot be edited, reordered, or quietly deleted after the fact.

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
| `scorecard` | every metric, plus the reliability bins |
| `verdict` | per-clause contract result, naming the contract |
| `canary` | leakage screen findings |
| `data_snapshot` | contract name and digest, data version, panel digest, hazard, scope, period, region count, year range |
| `contract_digest` | which contract this was judged under |
| `prev_hash` / `card_hash` | the chain |

## Reading the ledger

```bash
readiness ledger -c <contract>              # summary table + chain verification
readiness ledger -c <contract> --show       # full cards
readiness ledger -c <contract> --id exp-0002
```

If `verify()` reports a broken chain, stop. Every score above the break is
untrustworthy, and the fix is to find out what rewrote history — not to
regenerate the ledger.
