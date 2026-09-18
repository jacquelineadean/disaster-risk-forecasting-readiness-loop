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

**Status: Phase 0 complete, Phase 1 built; the Phase 1 exit needs the
real-data run.** The eval plane is built and its Phase 0 exit criteria are
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

Each Storm Events year file is national, so one download serves every state
and every hazard: it is checksummed, split into one compact extract per state
(or one national extract), and the raw bytes are discarded unless `--keep-raw`
is set — in which case a later contract for another state re-uses the pinned
raw file instead of downloading it again.

Report §7 warns that federal series can stop — NOAA retired its Billion-Dollar
Disasters product in May 2025 with no updates beyond CY2024. Hence the pinning,
and hence `--keep-raw` for mirroring what licences allow. Per-layer licences and
attribution: [`DATA-LICENSES.md`](DATA-LICENSES.md).

A cached file with no manifest record counts as *unpinned* and is re-fetched
rather than used. An experiment that cannot name its data version is not an
experiment.

---

## Commands

```
readiness contracts         list the registered contracts
readiness contract          print one contract and its hash        [-c NAME]
readiness register NAME     pre-register a new contract from options
readiness hazards           list the hazard catalogue
readiness models            list proposable models
readiness snapshot          pull and pin the data, print the manifest   [-c NAME]
readiness panel             build the labelled panel, print coverage   [-c NAME]
readiness features          load the feature sources, print admission verdicts and the audit  [-c NAME] [--features era5,terrain,nri,climada]
readiness score MODEL       fit and score one model  [-c NAME] [--split train|validate] [--features ...] [--param k=v]*
readiness loop              run the full experimental loop  [-c NAME] [--queue baseline|phase1] [--features ...] [--promote] [--backend local|claude]
readiness promote MODEL     the one atomic test touch: spend the budget and write the test card  [-c NAME] --spend-test-touch
readiness backtest          write experiments/<name>/backtest.html from committed files only  [-c NAME] [-o PATH]
readiness canary            demonstrate the harness rejecting a leaked model  [-c NAME]
readiness ledger            show and verify the experiment ledger  [-c NAME] [--show] [--id ID]
readiness verify            check a phase's exit criteria  [-c NAME] [--phase 0|1] [--replay] [--bless]
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
  verify.py          the exit criteria as a library: phase0 (fingerprints, canary), phase1 (ledger-only)
  backtest.py        the Phase 1 report, rendered from committed files only
  connectors/        data plane: base (pinning, HTTP), census, storm_events, nws_zones, mcp_server,
                     gazetteer, open_meteo, nri, climada_layer (the Phase 1 feature sources)
  harness/           eval plane: metrics, splits, labels, scoring, contract, canary, ledger,
                     features.py (the feature channel and its temporal firewall)
  engine/            proposable models: climatologies, persistence, the canary target,
                     features.py (the catalogue), history.py, linear.py, boosting.py, calibrate.py
  agent/             orchestrator, subagent definitions, guard.py (the integrity guard around --backend claude)
  dashboard.py       the ledger rendered as a self-contained HTML page
  cli.py             the `readiness` command
contracts/           registered contracts, one JSON file each; three examples ship
experiments/         one directory per contract: ledger, anchor, test-touch budget
harness_expected/    blessed baseline fingerprints, one file per contract
snapshots/           pinned data; only manifest.json is committed
docs/                how-it-works.md (the walkthrough), contracts.md (the reference), features.md (the
                     firewall), backtest.md (the report), plan.md and plan-design-annex.md, media/
skills/              agent runbooks: verification-protocol.md, experiment-card.md, climada-recipe.md
tools/               build_report.py (design -> report), build_site.py (the website), demo/capture.py (docs media),
                     climada/ (run_event_set.py, the GPL tool that writes a pinned layer, never imported)
site/                the overview website: pages, and the browser sandbox that runs the package
plans/               scenario library — the Phase 3 seed
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
- **Phase 2 — multi-hazard, national, with exposure.** One registered contract
  per peril against the same harness; join to USA Structures so outputs become
  human.
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
