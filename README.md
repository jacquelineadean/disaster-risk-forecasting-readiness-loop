# The Readiness Loop

An open-source, agentic system that forecasts natural-disaster risk from public
data, **validates its own probabilities against history**, and iterates until
they can be trusted — then turns them into emergency plans.

**The website is published at
[jacquelineadean.github.io/disaster-risk-forecasting-readiness-loop](https://jacquelineadean.github.io/disaster-risk-forecasting-readiness-loop/)** —
the design, the walkthrough, every committed ledger, and a sandbox that runs
this package in your browser. Nothing to install.

The research briefing that specifies this is in [`report/index.html`](report/index.html)
(open it, or `make serve`). Everything below is the implementation; for a
step-by-step tour with screenshots and a recording of a real run, see
[**docs/how-it-works.md**](docs/how-it-works.md), or open the
[website](#the-website), which walks through the design and runs the real
code in your browser.

**Status: Phase 0 complete; Phases 1–3 built; their exits need the data run
and, for Phase 3, the blinded reviews.** The eval plane is built and its Phase 0 exit criteria are
met on real NOAA data. The whole loop is *contract-driven*: the hazard, the
geography, the forecast period, the damage definition, the locked splits and
the acceptance thresholds are all declared in a registered contract, and the
same harness runs against any of them. Phase 1 adds the feature channel with
its temporal firewall, four candidate models, the one atomic test touch
(`readiness promote`) and the backtest report. Its exit criterion — BSS > 0
on the untouched test years with reliability within ±5 points, published
with the ledger — is checked by this repository's own CI with the pinned
data, through
[`.github/workflows/real-data.yml`](.github/workflows/real-data.yml), and it
is met only when a test card **passes and is published**. Until that run, no
test touch has been spent. Why the harness came before any model:
[Harness before model](#harness-before-model).

Phase 2 runs the same protocol over nine registered contracts — three
examples and six national ones, "six so four can pass" — and puts the first
human-facing output at the end of it: `readiness fleet` walks the contracts
in turn with one ledger and one touch budget each; `readiness exposure`
pins FEMA / ORNL USA Structures counts per county, with no sub-county field
by construction, and spot-checks ten sampled counties against assessor
counts a person collected; `readiness issue` refits a promoted model and
writes a probability per county for a period the pinned data has reached;
and `readiness brief` writes a county paragraph whose every sentence cites
a card, an issued file, a pinned extract or a named guidance document, and
which is written only when `readiness.cite` finds no violation
([docs/brief.md](docs/brief.md)). Its exit — four hazards passing
nationally, ten spot-checked counties — needs the same real-data run.

Phase 3 turns a county's risk into a document about one building:
`readiness gap-report` stress-tests a facility's emergency plan against a
hazard-agnostic 96-hour scenario, the facility's own record (no address or
coordinate field exists — design intensity comes from the planner's
elevation certificate or FIRM, cited as a facility document) and the
county's issued risk layer, answering each scenario question `answered`,
`unanswered`, `failed` or `cannot_run`, fail-closed when the design
intensity is missing; it is validated by the same `readiness.cite` rules and
rendered twice, plain and blinded (`FACILITY-<hash6>`, `PARTNER-n`), and
real facility files and reports never enter git. `readiness review record`
binds a practising emergency manager's rating to the sha256 of the blinded
report they read. Its exit — three real facilities' blinded gap reports
rated useful or better — needs both the data run and those reviews
([docs/plans.md](docs/plans.md)).

---

## Quickstart

No dependencies. Python 3.10+.

```bash
make install   # pip install -e ., so the `readiness` entry point exists
```

```bash
readiness register flood-xx --hazard inland_flood --state XX   # pre-register a contract
```

```bash
make snapshot CONTRACT=flood-xx   # pull and pin ~300 MB of public data (once, ~3 min)
```

```bash
make loop CONTRACT=flood-xx       # run the full experimental loop
```

```bash
make verify CONTRACT=flood-xx     # check the Phase 0 exit criteria
```

```bash
make test                         # no network needed
```

Replace `XX` with a two-letter US state, or omit `--state` for the whole
country, and `inland_flood` with any hazard from `readiness hazards`. Three
example contracts ship registered, with their ledgers and blessed fingerprints
— see [Examples](#examples) — so `make loop CONTRACT=tornado-ok` works from a
clean clone once the data is pulled.

`make loop` appends to the contract's **committed** ledger in `experiments/`.
To try a contract without touching those files — for a scratch run, a fourth
contract you are not ready to commit, or CI — point the whole experiments tree
somewhere else first:

```bash
READINESS_EXPERIMENTS_DIR=/tmp/readiness-scratch make loop CONTRACT=tornado-ok
```

Every command that reads or writes `experiments/<contract>/` (`loop`,
`promote`, `backtest`, `ledger`, `verify`, `dashboard`) honours it.

---

## What the loop actually does

The forecast unit is deliberately small and auditable, and every part of it is
a contract term:

> **P**(at least one damaging event of hazard *H* in region *R* during period *T*)

"Damaging" is fixed per contract (by default: property damage ≥ $10,000, or any
injury or death, per NOAA Storm Events). Regions are US counties; the period is
a month, a quarter or a year. For one state, twenty training years and a
quarterly period, that is a few thousand labelled region-quarters — dense
enough to calibrate against, small enough to reason about.

`make loop` runs `gather context → take action → verify work → repeat` over a
queue of candidate models and writes an experiment card for each, into the
contract's own ledger. The queue is the same for every contract:

| model | what it is | what it is for |
|---|---|---|
| `climatology-pooled` | the training base rate, issued everywhere | the contract's reference; scored against itself it must show exactly zero skill and AUC 0.5, or the yardstick is bent |
| `climatology-seasonal` | per-region, per-period frequency, shrunk toward the scope-wide seasonal rate | the first candidate with any structure — it should beat the reference |
| `persistence-last-year` | persists the last *training* year's outcome and issues it for every holdout year — no holdout label ever reaches a model, so this is a sharp, fixed-level forecast, constant across the holdout years | expected to *fail* on reliability, so the contract is seen rejecting something |
| `leaky-oracle` | reads the outcomes it is scored on | must be **REJECTED** by the leakage canary; this is the Phase 0 exit criterion |

`readiness loop -c <name> --queue phase1 --features era5,terrain` runs the
baselines and then the Phase 1 candidates (`make phase1 CONTRACT=<name>`
chains it with the snapshot, the promotion, the report and the check). Each is a registered
model whose features the harness builds and audits before the fit
([Features](#features)); a candidate whose feature sets need sources that
were not loaded is skipped with a progress line, not a card:

| model | what it is |
|---|---|
| `logistic` | L2 logistic regression on the harness-built features plus a leave-one-year-out seasonal-rate logit from the training labels; full-batch gradient descent, fixed iterations, no RNG |
| `logistic+iso` | the same, then an isotonic map fitted on the last three training years only — the calibrator never sees a holdout year |
| `gbm` | histogram gradient boosting with depth-limited stumps, quantile cuts on training rows, no subsampling; deterministic |
| `gbm+iso` | `gbm` with the same isotonic calibrator |

## Harness before model

> Phase 0 — Harness before model. Build the eval plane first, with no
> forecasting at all.
> **Exit when** the agent reproduces the climatology baseline's scores
> bit-for-bit from a clean clone, and the harness rejects a deliberately leaked
> model (a canary test).

"Iterate until the accuracy is acceptable" is only science if the thing
measuring accuracy never moves. Building the scorer first — and proving it
rejects a cheater before any real model exists — is what stops the loop from
becoming an elaborate way to memorise the past.

`make verify` checks both criteria for a contract:

```
[ok]   contract flood-xx validates (sha256:…); splits are disjoint
[ok]   climatology baselines reproduce bit-for-bit
[ok]   leakage canary rejected leaky-oracle
         (tripped: implausible skill, implausible auc, outcome agreement, train provenance)
[ok]   ledger chain intact: 4 card(s)

Phase 0 exit criteria met for flood-xx.
```

---

## Examples

Three contracts are registered in [`contracts/`](contracts/). They were chosen
to differ in every dimension the contract controls, so that the same harness
is seen running across hazards, scopes, periods and zone policies:

| contract | hazard | scope | period | zone events | panel |
|---|---|---|---|---|---|
| `inland-flood-la` | inland flood | one state (64 parishes) | quarter | dropped (inland_flood is mixed-coded; Flash Flood is county-coded, Flood mostly so) | 7,680 units, base rate 8.0% |
| `tornado-ok` | tornado | one state (77 counties) | quarter | dropped (county-coded hazard) | 9,240 units, base rate 6.8% |
| `tropical-cyclone-gulf` | tropical cyclone | five states (534 counties) | month | expanded via the NWS crosswalk | 192,240 units, base rate 0.66% |

Each has been run through `make loop`, blessed and verified. The ledgers are
committed; `make verify CONTRACT=<name>` reproduces them from a clean clone.

```
inland-flood-la
id        model                        split           BSS     AUC  verdict
exp-0001  climatology-pooled@1.0.0     validate    +0.0000  0.5000  FAIL
exp-0002  climatology-seasonal@1.0.0   validate    +0.0169  0.5957  FAIL
exp-0003  persistence-last-year@1.0.0  validate    -0.0526  0.5012  FAIL
exp-0004  leaky-oracle@1.0.0           validate    +1.0000  1.0000  REJECTED

tornado-ok
exp-0001  climatology-pooled@1.0.0     validate    +0.0000  0.5000  FAIL
exp-0002  climatology-seasonal@1.0.0   validate    +0.1201  0.8175  FAIL
exp-0003  persistence-last-year@1.0.0  validate    +0.0114  0.5401  FAIL
exp-0004  leaky-oracle@1.0.0           validate    +1.0000  1.0000  REJECTED

tropical-cyclone-gulf
exp-0001  climatology-pooled@1.0.0     validate    +0.0000  0.5000  FAIL
exp-0002  climatology-seasonal@1.0.0   validate    +0.0092  0.8484  PASS
exp-0003  persistence-last-year@1.0.0  validate    -0.0588  0.5000  FAIL
exp-0004  leaky-oracle@1.0.0           validate    +0.9999  1.0000  REJECTED
```

Read the tables as assertions about the harness, not as forecasts:

1. **The reference scores exactly 0.0000 / 0.5000 on every contract**, and
   still fails every contract, because being climatology is not beating it.
2. **The same seasonal model lands differently on each hazard.** On the
   flood contract it has slight skill (+0.0169) but fails both reliability
   (worst populated bin [0.2, 0.3), n=55, deviates 10.3 points against a
   5-point tolerance) and the AUC floor (0.5957); on tornadoes it discriminates
   well (AUC 0.82) and fails only on calibration — its worst populated bin
   ([0.2, 0.3), n=90) forecasts 0.245 against an observed 0.322, a 7.7-point
   miss where the contract allows 5; on monthly tropical cyclones it *passes*
   — the season is so sharp that knowing the region and the month clears
   every clause. Its skill score there is +0.009, because at a 0.66% base
   rate the pooled reference is already nearly right nearly everywhere. A
   passing contract is permission to spend one test touch, not a claim of a
   forecast.
3. **The persistence baseline is FAILED, not rejected, on every contract** —
   only the leakage canary rejects; the contract fails things. It issues the
   last training year's outcome as a fixed level for every holdout year, so
   it never discriminates well enough to clear the AUC floor anywhere (0.50
   on flood and tropical cyclones, 0.54 on tornadoes). On the flood contract
   that fixed level also has negative skill (BSS -0.0526) and misses
   reliability; on tropical cyclones it has negative skill too (-0.0588) but
   passes reliability; on tornadoes it is the one contract where the fixed
   level carries a little real skill (BSS +0.0114) and clears reliability —
   AUC alone still fails it.
4. **The leaky oracle is rejected everywhere**, tripping all four canary
   checks. Running the canary across base rates from 8% to 0.66% is what
   exposed — and fixed — a check that had been calibrated to one hazard:
   judged as a pooled rate, near-zero forecasts on a rare hazard "agree" with
   the outcomes almost always and looked like a leak. The check is now per
   outcome class.

The first contract is the original Phase 0 series, and its numbers are
unchanged by the move to contracts-as-data. Register a fourth with
`readiness register` and the same table comes out for it.

---

## The website

**<https://jacquelineadean.github.io/disaster-risk-forecasting-readiness-loop/>**

[`site/`](site/) is a static overview website: the system design, the
walkthrough with its captured transcripts, every committed ledger with its
reliability diagrams and a hash chain your browser re-verifies, and a
**sandbox** that loads the actual `readiness` package into a Python runtime in
the browser ([Pyodide](https://pyodide.org)) together with the registered
contracts, the committed ledgers, the blessed fingerprints and the pinned
Storm Events extracts for the example contracts. Every button there runs the
same functions the command line runs — the loop, `verify` against the blessed
fingerprints, a calibration playground scored by the real harness, ledger
tampering caught by the real chain check.

```bash
make site          # generate site/generated/ from the registry, ledgers, fingerprints and snapshots
make serve-site    # http://localhost:8138
```

The look is a quiet research microsite: one reading column on white, one
typeface, whitespace instead of rules, and pictures that are the data — the
tile maps on the overview and the animated hero are the contracts' own
labelled panels, one tile per county, drawn from `generated/tapes.json`.

`tools/build_site.py` writes nothing by hand: contracts, digests, ledgers,
hazard catalogue, model registry, panels and transcripts are read from the same
modules the CLI uses, and the sandbox archive packs whatever pinned extracts
are in `snapshots/` (run `make snapshot CONTRACT=<name>` first; without them
the sandbox still registers and validates contracts, it just cannot build a
panel). States named by a single-state contract are packed with every event
type, so a visitor can register a new hazard against Oklahoma or Louisiana and
run the loop on it; the Gulf states are packed with tropical-cyclone rows only.
The whole archive is about 1 MB.

[`.github/workflows/site.yml`](.github/workflows/site.yml) builds and publishes
the site with GitHub Pages on every push to `main`. It pulls the pinned data
once per data version and caches it. Publishing needs Pages switched on for the
repository once, by hand — Settings → Pages → Source: "GitHub Actions" — which
is the one step the workflow cannot take for itself: creating a Pages site
needs admin rights, and is refused to the workflow's own token whatever its
`permissions:` block asks for.

---

## Architecture

Four planes, one contract per experiment series. **The agent proposes; the
harness disposes.**

```
readiness/contracts.py  the contract   pre-registered criteria, as data; hashed
readiness/connectors/   data plane     fetch, checksum and pin public sources
readiness/engine/       the models     everything the agent may propose
readiness/agent/        agent plane    orchestrator + subagents; writes cards
readiness/harness/      eval plane     scores them. No LLM. No agent writes.
```

Two invariants hold everywhere, and most of the design follows from them:

**No model ever receives a holdout label.** `splits.TrainingView` is the only
channel a model gets data through, and it raises if handed a panel containing a
validate or test year. `predict()` receives units and — for a Phase 1 model —
the harness-built and audited feature rows for exactly those units; never a
label. Labels are fetched in `scoring._fit_predict()` *after* `predict()` has
returned — one function, readable in one sitting, which is the point;
`score()`, `predictions_for()` and `screen()` all go through it.

**Nothing in the harness calls a language model.** Verification has to be
rules-based to be worth anything. If an LLM wants these numbers it reads them
downstream, from the ledger.

### Contracts

A contract is a JSON file in `contracts/`, written *before* any model is
fitted. Its criteria are hashed, and the hash is recorded on every experiment
card. Change a criterion and the hash changes, which marks every prior
experiment as visibly incomparable — that is the intended cost, not a bug.

```
contract        flood-xx 1.0.0  (sha256:…)
hazard          inland_flood  ['Flood', 'Flash Flood']
geography       US, XX
forecast unit   region x quarter
damaging event  property >= $10,000 or any casualty
zone events     dropped (do not join to counties)
train           1996-2015  (20y)
validate        2016-2020  (5y)
test            2021-2025  (5y, 1 touch)
reference       climatology-pooled
passes when     BSS > 0.0  |  reliability within +/-5% per populated bin (n >= 30)  |  AUC >= 0.7
```

`readiness register` writes one from options and validates it: splits must be
disjoint, contiguous and ordered in time; nothing may start before 1996; the
hazard must be in the catalogue or come with explicit Storm Events event
types. The name, version and description are labels and are not hashed;
everything else is. Every contract gets its own ledger, its own test-touch
budget and its own blessed fingerprints under its name.

The test-touch budget is persisted to disk, not held in memory — a budget that
resets when you restart the process is not a budget. The only command that
spends it is `readiness promote MODEL -c NAME --spend-test-touch`, which
refuses without a prior validate PASS for the same model, version and
arguments, refuses if any test card already exists under the contract
digest, and otherwise charges the budget and writes the test card in one
step. `readiness score --split test` is refused outright and names
`promote`: a test touch without a ledger card would be a spent budget with
no record.

### The hazard catalogue

`readiness hazards` lists the hazards the catalogue knows, each mapped to the
Storm Events event types that constitute it: inland flood, tornado, hail,
severe wind, lightning, tropical cyclone, storm surge, coastal flood, heat,
extreme cold, winter storm, drought, wildfire, debris flow, tsunami, avalanche
and dust storm. A contract may also name a hazard outside the catalogue by
listing its event types.

One caveat the catalogue makes explicit: Storm Events codes convective hazards
against counties and most broad-scale hazards — heat, tropical cyclones,
winter storms, wildfire — against NWS forecast zones. Zone-coded rows do not
join to a county universe on their own, and by default (`zone_policy: drop`)
the label builder discards them. The join exists:
[`readiness/connectors/nws_zones.py`](readiness/connectors/nws_zones.py) pins
the NWS zone-county correlation file, and a contract registered with
`--zone-policy expand` has the label builder map each zone-coded event to
every county in its NWS zone instead of dropping it — `tropical-cyclone-gulf`
is registered this way. `readiness panel` reports exactly how many events went
where under either policy — dropped, expanded, unmapped — and warns when a
hazard is mostly zone-coded and still set to `drop`, so a thin panel is
explained rather than mistaken for a rare hazard.

### The ledger

`experiments/<contract>/ledger.jsonl` is hash-chained: each card carries the
hash of the one before it, so edits, reorderings and mid-file deletions break
the chain and `verify()` reports where.

Chaining alone does **not** catch truncation — any prefix of a hash chain is
perfectly self-consistent, and "run ten experiments, delete the nine that
failed" is exactly the attack that matters here. So the head hash and card count
are anchored in a committed sidecar file. Forging both is still possible for
anyone with repo write access, but it is now two coordinated edits visible in
git history rather than one silent `head -n 1`.

### The leakage canary

Four checks over a model's output: implausible skill, implausible AUC,
near-binary forecasts that match the outcomes, and a training-digest mismatch.

It is a smoke alarm, not a proof. The real defence is structural — the two
invariants above. The canary catches the cases where that structure is breached
by an accidental join carrying the label column, a feature computed over the
full panel, or an agent that read a file it should not have. A sufficiently
subtle leak that produces merely-excellent rather than impossible scores will
pass it. Documented rather than papered over. Phase 1 adds a fifth check:
the feature digest a model declares must equal the digest of the frame the
harness handed it.

### Features

A Phase 1 model does not construct features; it names feature *sets* from
the engine's catalogue, and the harness builds every row under its own
temporal firewall ([`readiness/harness/features.py`](readiness/harness/features.py)).
Sources hand over raw monthly series and static tables and never decide a
cutoff; the harness computes each unit's cutoff as the period's first month
minus the spec's lag (at least one month, so a July forecast is built from
data through May); a closed vocabulary of transforms is the only code that
touches a series; and before any fit, an audit rebuilds every value with the
months at or after the cutoff poisoned and requires the frame to be
bit-identical.

Admission is where the firewall shows on real data. A source built from the
ground truth is refused outright. A static layer declares the last year it
encodes and is refused for any contract whose validate split starts at or
before it — so **FEMA's National Risk Index (v1.20, through 2023) is refused
under every current contract**, and `readiness features --features nri`
prints that refusal as a finding, not an error. A layer that claims to be
timeless must be on the harness's short allow-list of physical geometry
(county centroids, elevation). The whole of it, with the transform
vocabulary and what the harness proves versus trusts, is in
[docs/features.md](docs/features.md).

### Phase 2: the fleet, exposure and the brief

The fleet ([`readiness/fleet.py`](readiness/fleet.py)) is the loop over
every registered contract, sequential on purpose — the manifest the data
plane writes is not safe to share between two runs — and tolerant of a
contract whose data is missing, which is reported and skipped so one
unpulled extract does not cost the other five their ledgers. `fleet
--status` reads the ledgers, touch files and backtest reports back through
the same `verify.phase1` the exit check uses, and fits nothing.

Exposure ([`readiness/exposure/`](readiness/exposure/),
[`readiness/connectors/usa_structures.py`](readiness/connectors/usa_structures.py))
is a join, never a covariate: the connector asks the USA Structures
FeatureServer for *counts* grouped by county and occupancy class and never
downloads a footprint; `CountyExposure` has six fields and none in which a
tract, parcel, point or address could travel; and the layer declares its
edit year as `derived_through`, so the firewall refuses it as a feature
under every current contract. The spot-check
([`exposure_expected/`](exposure_expected/)) divides our county total by an
assessor's count collected by a person, prints every ratio, and counts only
in-band rows toward the ten the exit needs.

Issuance and the brief (`readiness/issue.py`, `readiness/brief.py`,
[`readiness/cite.py`](readiness/cite.py)) close the loop with a document.
`issue` has four guards and no flag that skips one: a passing, canary-clear
*test* card for exactly this model, version and arguments; the refit's
training and feature digests equal to that card's; a clean feature audit of
the target period's frame; and a period that is issuable — after every year
the contract spans, and one the pinned series reach (a model with no feature
sources may issue exactly the first period after the contract's last year).
It has no parameter through which a label could arrive, and it will not
overwrite an issued file without `--reissue`. `cite.validate` is the rule set
every human-facing document passes: every sentence cites, every citation
resolves, every cited value equals the number the artefact holds, every
number is a cited value, no warning language. The brief names nothing below
the county and is written only when the list of violations is empty. The guidance documents a
sentence may cite are registered in [`plans/guidance.json`](plans/guidance.json).

---

## Data

Every source is fetched, checksummed and pinned before use.
`snapshots/manifest.json` is committed; the ~300 MB of raw pulls are not, so a
clone reproduces the exact data version without carrying the bytes.

| layer | source | role |
|---|---|---|
| ground truth | NOAA Storm Events, 1996– | the validation target, every hazard |
| region universe | Census national county file | the panel denominator, any state or all |
| zone-county crosswalk | NWS zone-county correlation file | joins zone-coded events (heat, tropical cyclones, winter storms, wildfire, …) to counties, pinned only for contracts registered with `--zone-policy expand` |
| exposure | FEMA / ORNL USA Structures, county counts by occupancy class | the Phase 2 join, pinned per state by `readiness exposure snapshot`; refused as a feature; never below the county |

Each Storm Events year file is national, so one download serves every state
and every hazard: it is checksummed, split into one compact extract per state
(or one national extract), and the raw bytes are discarded unless `--keep-raw`
is set — in which case a later contract for another state re-uses the pinned
raw file instead of downloading it again.

Report §7 warns that federal series can stop — NOAA retired its Billion-Dollar
Disasters product in May 2025 with no updates beyond CY2024. Hence the pinning,
and hence `--keep-raw` for mirroring what licences allow. Per-layer licences and
attribution: [`DATA-LICENSES.md`](DATA-LICENSES.md).

Exposure outputs — the `readiness exposure` tables, the issued files' joins
and the county briefs — are a separate artefact from the probability
outputs, so that an exposure layer under a share-alike licence could never
reach the forecast; today's layer (USA Structures) is public domain and the
brief carries its attribution line.

A cached file with no manifest record counts as *unpinned* and is re-fetched
rather than used. An experiment that cannot name its data version is not an
experiment.

---

## Commands

```
readiness contracts         list the registered contracts  [--names] [--national]
readiness contract          print one contract and its hash        [-c NAME]
readiness register NAME     pre-register a new contract from options
readiness hazards           list the hazard catalogue
readiness models            list proposable models
readiness snapshot          pull and pin the data, print the manifest   [-c NAME]
readiness panel             build the labelled panel, print coverage   [-c NAME]
readiness features          load the feature sources, print admission verdicts and the audit  [-c NAME] [--features era5,terrain,nri,climada]
readiness score MODEL       fit and score one model  [-c NAME] [--split train|validate] [--features ...] [--param k=v]*
readiness loop              run the full experimental loop  [-c NAME] [--queue baseline|phase1|phase2] [--features ...] [--promote] [--backend local|claude]
readiness fleet             the loop over many contracts in turn, or their status  [--national | --contracts A,B] [--queue baseline|phase1|phase2] [--features ...] [--promote] [--status]
readiness promote MODEL     the one atomic test touch: spend the budget and write the test card  [-c NAME] --spend-test-touch
readiness backtest          write experiments/<name>/backtest.html from committed files only  [-c NAME] [-o PATH]
readiness canary            demonstrate the harness rejecting a leaked model  [-c NAME]
readiness ledger            show and verify the experiment ledger  [-c NAME] [--show] [--id ID]
readiness exposure snapshot pull and pin USA Structures county counts per state  [--states A,B | --all-states] [--layer-url URL]
readiness exposure show     print CountyExposure rows from the pinned extracts  [--county FIPS | --state XX]
readiness exposure spot-check  ours / assessor for every row of exposure_expected/assessor_counts.csv; exit 1 below ten in-band counties from three states  [--counts PATH]
readiness issue MODEL       refit the promoted model, write issued/<contract>/<period>.json; exit 2 on a refusal  -c NAME --period YYYY-Qn|YYYY-Mnn|YYYY [--features ...] [--param k=v]* [--reissue]
readiness brief             one cited, validated brief per county; exit 1 listing the violations  (--county FIPS | --state XX) --period YYYY-Qn [--out DIR]
readiness scenarios         list the scenario library, or check every case study against its expected findings  {list|check} [--case-studies DIR]
readiness gap-report        one cited, blinded gap report for a facility; exit 1 listing violations, exit 2 unreadable  --facility PATH --period YYYY-Qn [--scenario ID] [--out DIR] [--drafter local|claude]
readiness review record     bind a rating to a blinded report's sha256, plans/reviews/<sha>.json  --report PATH.blind.html --rating {not useful,somewhat useful,useful,very useful} --role ROLE --org-type hospital|county|state|ngo|other --years N [--comments TEXT] [--reviews DIR]
readiness verify            check a phase's exit criteria  [-c NAME] [--phase 0|1|2|3] [--replay] [--bless] [--reports DIR] [--reviews DIR]
readiness dashboard         render a contract's ledger as a static HTML page  [-c NAME | --all]
readiness report            rebuild the static research report
readiness mcp               run the read-only MCP data server on stdio  [-c NAME]
```

`-c/--contract` takes a registered name or a path to a contract JSON. If it is
omitted the CLI uses `$READINESS_CONTRACT`, then the sole registered contract
if there is exactly one; with several registered it refuses and lists them.

`readiness ledger --id exp-0002` prints that one card on its own; `--id`
implies `--show`, so it is sufficient by itself.

### The agent backends

`--backend local` (default) is deterministic and uses no LLM: it walks a fixed
queue of candidates. This is what CI runs and what the bit-for-bit
reproducibility criterion is checked against — a loop whose control flow depends
on a language model cannot have a bit-for-bit criterion.

`--backend claude` runs the same loop with a Claude Agent SDK orchestrator and
the subagents from report §4 (a hazard analyst for the contract's hazard, a
calibration critic, a data steward). Needs `pip install 'readiness-loop[agent]'`
and an API key. The harness is unchanged; no subagent is granted a write tool.
Because Bash is a write channel whatever the prompt says, `readiness.agent.guard`
hashes the harness, the contract machinery, the data plane (`readiness/data.py`,
`readiness/connectors/`, the manifest and the pinned extracts), the guard
itself and `contracts/` before the run and again after it, and stamps every
card the run writes with the digest of that code as it stood at scoring time.
Any byte that moved, even one restored before the run ended, fails the run
with the list of paths, and the cards it wrote are not to be trusted or
committed.

### MCP

`readiness mcp -c NAME` serves the pinned data and the contract's ledger over
MCP (stdio, standard-library JSON-RPC, no dependencies):

```json
{"command": "python3", "args": ["-m", "readiness.cli", "mcp", "-c", "NAME"]}
```

Every tool is a read. `get_region_history` exposes **training years only**.
There is no tool that returns a holdout outcome, and a test asserts it.

---

## Repository map

```
readiness/
  contracts.py       the contract schema, validation, digest and registry. Do not edit while iterating.
  config.py          harness-wide policy: the hazard catalogue, record start, canary ceilings
  data.py            builds a contract's dataset: snapshot, panel, provenance; input_keys/pinned
  verify.py          the exit criteria as a library: phase0 (fingerprints, canary), phase1 (ledger-only),
                     phase2, phase3 (reviews, reports, case studies, no coordinates)
  backtest.py        the Phase 1 report, rendered from committed files only
  fleet.py           the loop over every registered contract in turn, and --status from the ledgers
  issue.py           refit the promoted model, write the issued probabilities per county; parse_period/period_label
  brief.py           the county brief: one cited paragraph per hazard, validated before it is written
  cite.py            the citation rules every human-facing document passes (six violation codes)
  exposure/          USA Structures county counts: occupancy.py (classes), table.py (county-only), spotcheck.py
  connectors/        data plane: base (pinning, HTTP), census, storm_events, nws_zones, mcp_server,
                     gazetteer, open_meteo, nri, climada_layer (the Phase 1 feature sources),
                     usa_structures.py (county counts, never footprints)
  harness/           eval plane: metrics, splits, labels, scoring, contract, canary, ledger,
                     features.py (the feature channel and its temporal firewall)
  engine/            proposable models: climatologies, persistence, the canary target,
                     features.py (the catalogue), history.py, linear.py, boosting.py, calibrate.py
  agent/             orchestrator, subagent definitions, guard.py (the integrity guard around --backend claude),
                     agent/planner.py (optional Claude Agent SDK drafter for `readiness gap-report --drafter claude`)
  dashboard.py       the ledger rendered as a self-contained HTML page
  cli.py             the `readiness` command
readiness/plans/     facility record, scenario rules, the risk layer, gap report and its blinding,
                     reviews, case-study checks — the Phase 3 package; see docs/plans.md. Not the same
                     directory as plans/ below, which is committed data, not code.
contracts/           registered contracts, one JSON file each; three examples and six national ship
experiments/         one directory per contract: ledger, anchor, test-touch budget
issued/              what `readiness issue` writes: issued/<contract>/<period>.json, one probability per
                     county; outputs, not sources, so only issued/README.md is committed
briefs/              what `readiness brief` writes: briefs/<fips>/<period>.html and .json, the page and
                     the validated document it was rendered from; only briefs/README.md is committed
harness_expected/    blessed baseline fingerprints, one file per contract
snapshots/           pinned data; only manifest.json is committed
exposure_expected/   assessor_counts.csv, the person-collected half of the exposure spot-check (ships header-only)
docs/                how-it-works.md (the walkthrough), contracts.md (the reference), features.md (the
                     firewall), backtest.md (the report), brief.md (the county brief), plans.md (the
                     gap report), plan.md and plan-design-annex.md, media/
skills/              agent runbooks: verification-protocol.md, experiment-card.md, climada-recipe.md
tools/               build_report.py (design -> report), build_site.py (the website), demo/capture.py (docs media),
                     climada/ (run_event_set.py, the GPL tool that writes a pinned layer, never imported)
site/                the overview website: pages (briefs.html lists the fleet and the validated briefs), and the
                     browser sandbox that runs the package
plans/               guidance.json (the documents a report may cite), scenarios/ (md beside json, one
                     scenario), case-studies/ (worked examples with a source per fact; none ship).
                     facilities/, reports/, reviews/ hold real inputs and outputs and are gitignored
                     except one fictional example and each directory's own README
design/              the imported Claude Design source (.dc.html) — source of truth
report/              index.html, compiled from design/ by tools/build_report.py
tests/               unittest suite, no network required
```

---

## Roadmap

Phases 1–4 are specified in [report §6](report/index.html#roadmap). Each has a
falsifiable exit criterion, and the architecture is meant to absorb them without
redesign:

- **Phase 1 — the loop, on one hazard.** *Built:* the harness-owned feature
  channel and its firewall, the ERA5, Gazetteer, NRI and pinned-CLIMADA
  connectors, the `logistic`/`gbm` candidates with an isotonic calibrator,
  `loop --queue phase1`, `promote` as the one atomic test touch, the
  backtest report and `verify --phase 1`. *Remaining:* the real-data run
  (`make phase1 CONTRACT=<name>` through `real-data.yml`) that spends the
  touch — whether any candidate clears the contract on Louisiana or Oklahoma
  2021–2025 is not knowable offline. *Exit: BSS > 0 on untouched test years
  with reliability within ±5 pts, published with the ledger.*
- **Phase 2 — multi-hazard, national, with exposure.** *Built:* six
  national contracts registered as data, `readiness fleet` with one ledger
  and one touch budget per contract, the USA Structures connector and the
  county-only exposure table with its spot-check, `readiness issue` with its
  three guards, `readiness cite` and the county brief, `verify --phase 2`.
  *Remaining:* the real-data run (`make phase2`) — which four hazards pass
  is not knowable offline, and the zone-coded ones (heat, winter storm) risk
  crosswalk drift; the USA Structures layer URL and field vocabulary are
  unconfirmed until the first pull; and the ten assessor counts are
  collected by a person, with URLs, into `exposure_expected/`. *Exit: at
  least four hazards pass the contract nationally; exposure joins
  spot-validated against county assessor counts in ten sampled counties.*
- **Phase 3 — the planning thought-partner.** Scenario stress-tests of a
  facility's emergency plan against the validated risk layer. Case studies
  become regression tests. Seeded in [`plans/`](plans/).
- **Phase 4 — global scale-out.** Swap US layers for Open Buildings, Flood Hub,
  EM-DAT. The architecture does not change; the connectors do.

---

## What this is not

**This is decision support. It is never a warning channel.** Official alerts
come from the National Weather Service and IPAWS. Region-period probabilities
must never be phrased as predictions of specific events, and no output here
should be used to decide whether to act on a hazard that is imminent.

The full set of risks the design commits to carrying — overfitting the loop,
biased and drifting ground truth, LLM prose in life-safety documents, licensing
hygiene, and the harm that publishing address-level risk can do to the people it
is meant to help — is in [report §7](report/index.html).
