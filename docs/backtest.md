# The backtest report

`readiness backtest -c NAME` writes `experiments/<name>/backtest.html` and
`backtest.json`: the published record of a contract's Phase 1 run. It is the
document the exit criterion names — "BSS > 0 against climatology on
untouched test years with reliability within ±5 points per populated bin,
*published with the ledger*" — and it is built so that a reader can check
every number in it against files that are committed beside it.

## Built from committed files only

The report reads the ledger, its anchor, the contract, the snapshot manifest
and the blessed fingerprints. It does not build the dataset, fit anything or
touch the network. That is a deliberate constraint, not a convenience: a
report that recomputed its numbers could disagree with the ledger, and then
one of them would have to be believed. This one cannot disagree, because it
has nothing of its own to say. `make backtest` runs on a clean clone without
the pinned data.

The page embeds `<meta name="ledger-head">`, the hash of the last card at
the time it was rendered. `readiness verify --phase 1` requires the report
to carry the head of the ledger it is checking, so a report rendered before
the last card, or edited after it, is stale and the check fails.

## What the report contains

- **The contract**, as `readiness contract` prints it, and its digest. Every
  card below was judged under this digest; a card from an earlier digest is
  shown as incomparable, not silently merged.
- **Data provenance**: the `data_version` and `feature_version` the cards
  record, and every manifest key they were built from with its sha256.
- **Feature columns**, each with its spec — source, variable, transform,
  window and lag — and the audit findings recorded on the cards
  ([features.md](features.md)).
- **The NRI benchmark row, stamped INADMISSIBLE.** FEMA's National Risk Index
  is the layer everyone asks about, and the report shows it in its place:
  refused, with the reason (encodes data through 2023; the contract validates
  from 2016).
- **The full validate history**, including every failure. A ledger of only
  successes is a marketing document; the report is the ledger.
- **The test card**, when one exists: the reliability diagram
  (`dashboard.reliability_svg`), the Murphy terms, the bin table, the
  verdict per clause, its `card_hash`, and the **total number of test touches
  in the ledger**.
- **Deliberately not tried**: the candidates the queue names and the run
  skipped, with the reason (sources not loaded; set inadmissible).
- **Attribution**, from [`DATA-LICENSES.md`](../DATA-LICENSES.md): the
  Open-Meteo CC BY line for the ERA5 extract, the public-domain lines for
  Storm Events, the Census and FEMA, the CLIMADA line if a layer was used —
  and the sentence that this is decision support and never a warning
  channel; official alerts come from the National Weather Service and IPAWS.

## How to read the test card

The test card is the only card on the page that says anything about the
future the contract was written for. Read it in this order:

1. **The touch count.** The report prints how many cards in the ledger have
   `split == "test"` under the current contract digest. The number to expect
   is **one**. `verify --phase 1` further requires the passing test card to
   be the *first* test card in the ledger: a pass that follows an earlier
   failed touch is not a pass, it is test-shopping, whatever version number
   the second model carried.
2. **The verdict, re-derived.** The verdict shown is the one on the card;
   `verify --phase 1` recomputes it from the stored scorecard with the same
   `evaluate()` the loop used (BSS > 0, every populated bin within tolerance,
   AUC at or above the floor, digest match). If the two disagree the check
   fails and the report is not to be believed.
3. **The canary**, which must not have rejected the card, and the **feature
   audit**, which must be clean.
4. **The validate card it was promoted from.** `readiness promote` refuses
   without a prior validate PASS for the same model, version and constructor
   arguments (`data_snapshot.model_kwargs`); the report links the two.
5. **Then the numbers.** BSS against the pooled climatology, the reliability
   bins against the ±5-point band, AUC. A test score much worse than the
   validate score is information about how much validate was overfitted, and
   it belongs on the card, not in a footnote.

## One touch, and what a failure means

The contract grants one test touch per model version, kept on disk in
`test_touches.json`. `readiness promote MODEL -c NAME --spend-test-touch` is
the only command that spends it (`score --split test` is refused and points
here), and it charges the budget before it scores, so an interrupted run has
still spent the touch and still has a card.

**A failed first test touch is published.** The card is in the ledger, the
report shows it, the site serves it, and the contract cannot exit Phase 1.
That is the result, and the report says so plainly. The sanctioned next move
is a **new contract** — a different hazard, scope, period or damage
definition, registered under a new name with its own ledger and its own
budget — never a relaxed threshold on this one (which changes the digest and
marks every card incomparable, as intended) and never a second touch under
a new version number. The budget exists so that the published number is the
number that was got, not the best of several.

## Regenerating and publishing

```bash
readiness backtest -c NAME          # experiments/NAME/backtest.html and backtest.json
readiness verify -c NAME --phase 1  # ledger-only: chain, one first passing test card, fresh report
make site                           # copies the report to site/generated/backtest/NAME.html
```

The real-data run that spends the touch is
[`.github/workflows/real-data.yml`](../.github/workflows/real-data.yml); its
pull request carries the ledger, the anchor, the touch file and the report
together, and the site publishes them from `main`.
