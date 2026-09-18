# The county brief

A county brief is the first human-facing output of the loop: one short
document per county and forecast period, built by `readiness brief` from
the files that `readiness issue` wrote, the pinned USA Structures counts and
the ledgers, and written only when `readiness.cite.validate` finds nothing
wrong with it. Report §7 sets the terms: "Numbers come from the harness;
prose comes from the model — and prose can hallucinate. Require every plan
sentence to cite either a computed number, a dataset row, or a named
guidance document, validated by rules before display." And, from the same
section: this is decision support, never a warning channel.

This page says what a brief is, the exact sentences it contains, the rules
it passes, the guards on the issuance it depends on, what it never contains,
and the attribution it carries. The code is `readiness/brief.py` (the
document), `readiness/issue.py` (the probabilities), `readiness/cite.py`
(the rules) and `readiness/exposure/` (the counts).

```bash
readiness brief --county 40109 --period 2026-Q4      # one county
readiness brief --state OK --period 2026-Q4           # every county in a state
readiness brief --county 40109 --period 2026-Q4 --out /tmp/briefs
```

Output: `briefs/<fips>/<period>.html` and `briefs/<fips>/<period>.json` —
the rendered page and the `cite.Document` it was rendered from, so anyone
can re-run the validator on the committed file. Exit 1, listing every
violation, when the document does not validate; nothing is written then.
The website's [briefs page](../site/briefs.html) lists the committed briefs
that still validate at build time.

## What a brief is

One paragraph per contract with an issued file covering the county, in a
fixed order: the probability, where it came from, what the county holds, and
the disclaimer. Every sentence carries inline citation markers, `[c:ID]`,
that point at claims listed under the paragraph; a claim is a value, a
format, and a source of one of the kinds `readiness.cite` knows: a ledger
card, an issued file, a manifest key, a guidance document. The brief is not
written by a language model. The sentences are templates the code fills
from cited values, which is why every number in them can be checked.

## The sentence shapes

For each contract whose issued file covers the county, exactly these
sentences, in this order (annex §2.5):

1. **The probability.** `{p:.0%} chance of at least one damaging {hazard}
   event in {county} during {period} [c1].` — `p` is the county's value in
   the issued file; `hazard` is the contract's hazard; `period` is the label
   the file was issued for (`2026-Q4`, `2026-M11` or `2026`).
2. **The provenance.** `This comes from {model}@{version}, which scored a Brier skill score of
   {bss:+.2f} on the untouched {test years} with every populated reliability
   bin within 5 points [c2].` — `bss` is the Brier skill score on the
   contract's *test* card, cited by card id; `model@version` is the promoted
   model the issued file names as `validated_by`. The 5 is the contract's
   reliability tolerance and is itself a cited value.
3. **The exposure.** `{county} holds {total:,} structures ({unclassified:.0%}
   unclassified) [c3], including {schools} schools and {hospitals} hospitals
   [c4].` — from the county's `CountyExposure` row, cited by the manifest key
   of the state extract it came from. When no extract is pinned for the
   county's state the brief says so in a cited sentence; it never substitutes
   a neighbour's counts or a national average.
4. **The disclaimer.**
   `Not a warning product; official alerts come from the NWS and IPAWS [g1].`
   — verbatim, citing the guidance entry `nws-ipaws` in `plans/guidance.json`.
   This is the only sentence allowed to contain the words "warning" and
   "alert", and only with that citation.

"Chance of at least one damaging event in the county during the period" is
the forecast unit, and the brief repeats it rather than paraphrasing it.
"At least one" means occurrence: the contract labels a county-period
positive when Storm Events records one qualifying event there, whatever its
footprint.

## The citation rules it passes

`readiness.cite.validate` runs before anything is written and returns a
list of violations; the brief is written only when the list is empty. Each
rule has a code, and `readiness brief` prints the code with the sentence
that broke it:

| code | rule |
|---|---|
| `UNCITED` | every sentence cites at least one claim, by marker or by id |
| `UNKNOWN_CLAIM` | every citation names a claim the document lists |
| `UNRESOLVED` | every claim's source resolves: the card exists in the ledger and has the cited field; the issued file exists and covers the cited period; the manifest key is in `snapshots/manifest.json`; the guidance id is in `plans/guidance.json`; a `computed` claim names the claims it was derived from |
| `NUMBER_WITHOUT_CLAIM` | every numeric token in the prose equals the rendered value of a claim that sentence cites — the same digits, through the claim's format — so a number cannot be typed in, rounded differently or carried over from another sentence |
| `FORBIDDEN_PHRASE` | no "will occur", "will hit", "will strike", "is predicted to hit", "warning" or "alert" anywhere, except the fixed disclaimer sentence with its `nws-ipaws` citation |

Identifiers are not numbers: a card id (`exp-0007`), a model version
(`1.2.0`), a period label (`2026-Q4`), a five-digit FIPS code, a bare year
and the aliases listed in `plans/guidance.json` (`CPG 101`, `42 CFR 482.15`,
`NFPA 110`) are exempt from the number rule. Everything else with a digit in
it must be a cited value.

The validator is a smoke alarm for words. It proves that no number was
invented and no sentence stands without a source; it does not prove that
the source supports the sentence. That reading remains a person's job, and
the brief says so in its footer.

## The issuance guards

A brief cites an issued file, and `readiness issue` writes one only when
every guard in `readiness/issue.py` holds. There is no flag to skip any of
them, and there is no parameter through which a label for the target period
could arrive — no such label exists for a future period, and the function
has nowhere to put one.

- **A validated model.** The contract's ledger must hold a passing,
  canary-clear *test* card for exactly this model, version and constructor
  arguments under the current contract digest. `readiness issue` takes the
  model name and `--param key=value`; if that combination has no such card
  it is refused (`IssueRefused`, exit 2), whatever its validate cards say.
- **The digests must match.** The model is refitted through `TrainingView`
  on the training years only, and the refit's training digest and feature
  digest must equal the ones on the test card. A revised ERA5 extract, a
  changed county file or a different feature set changes the digest and the
  issue is refused rather than silently reissued from a fit nobody scored.
- **The period must be issuable.** The target period's features are built
  by `build_frame` under the same firewall the backtest used — the cutoff is
  the period start minus the feature lag — and `audit_frame` runs on them.
  If any series source's last pinned month is earlier than the cutoff, the
  period cannot be issued yet, and the refusal names the month the data
  would have to reach ("period cannot be issued yet: data through YYYY-MM
  needed"). Issuance waits for the data; it never extrapolates.

What gets written, `issued/<contract>/<period>.json`, carries the contract
and its digest, the model, version and kwargs, the test card it was
validated by, the period, the probabilities keyed by county FIPS, the
training and feature digests, the feature version, the data version and the
time of issue. `verify --phase 2` checks that each passing contract's issued
file names that contract's *first* test card.

Period labels are `YYYY-Qn` for quarterly contracts, `YYYY-Mnn` for monthly
ones and `YYYY` for annual ones; `readiness.issue.parse_period` and
`period_label` convert between the label and the `(year, period)` pair the
harness uses, and refuse a label whose shape does not match the contract.

## What is never in a brief

- **Anything below the county.** The exposure table has no sub-county
  field by construction (`CountyExposure` has six fields and a test pins the
  list), the issued file is keyed by county FIPS, and the brief's document
  is checked for sub-county keys before it is written. No tract, block,
  parcel, point, address or facility name can appear, because no input
  carries one. Report §7: publish county aggregates only.
- **"Would touch."** A probability of at least one damaging event in the
  county is a statement about occurrence, not about a footprint. The brief
  never says that an event would touch, reach or affect any number of
  structures; it says what the county holds, in a separate cited sentence.
- **Predictions of specific events.** No "will occur", "will hit", "will
  strike", "is predicted to hit". A region-period probability is never
  phrased as the forecast of one storm.
- **Warning language.** No "warning", no "alert", except in the one
  disclaimer sentence that points at the NWS and IPAWS. The brief is
  decision support for planning, weeks and months ahead; it must never be
  read as a reason to act on a hazard that is imminent.
- **Prose from a language model.** `readiness/brief.py`, `issue.py`,
  `cite.py` and `exposure/` import no LLM client, and
  `tests/test_boundaries.py` asserts it from the source.

## The attribution block

Every brief ends with a footer the validator does not read as prose: the
generation time; the inputs it was built from (the contract digest, the
issued file's path and the data version, the manifest keys of the exposure
extracts, the ledger head); and the attribution lines the data licences
require — the ground truth, "NOAA NCEI Storm Events"; the region universe,
"US Census Bureau"; and the exposure line from the connector, "Exposure:
FEMA / Oak Ridge National Laboratory USA Structures (public domain), county
counts only". The exposure-joined brief is a separate artefact from the
probability outputs, as [`DATA-LICENSES.md`](../DATA-LICENSES.md) requires,
so that a future exposure layer under a share-alike licence never reaches
the forecast.
