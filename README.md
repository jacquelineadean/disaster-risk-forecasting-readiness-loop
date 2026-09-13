# The Readiness Loop

An open-source, agentic system that forecasts natural-disaster risk from public
data, **validates its own probabilities against history**, and iterates until
they can be trusted — then turns them into emergency plans.

The research briefing that specifies this is in [`report/index.html`](report/index.html)
(open it, or `make serve`). Everything below is the implementation; for a
step-by-step tour with screenshots and a recording of a real run, see
[**docs/how-it-works.md**](docs/how-it-works.md).

**Status: Phase 0 complete, for any hazard.** The eval plane is built and its
exit criteria are met on real NOAA data. The whole loop is *contract-driven*:
the hazard, the geography, the forecast period, the damage definition, the
locked splits and the acceptance thresholds are all declared in a registered
contract, and the same harness runs against any of them. There is no
forecasting model yet, on purpose — see
[Why there is no model yet](#why-there-is-no-model-yet).

---

## Quickstart

No dependencies. Python 3.10+.

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
make test                         # 260 tests, no network needed
```

Replace `XX` with a two-letter US state, or omit `--state` for the whole
country, and `inland_flood` with any hazard from `readiness hazards`. Three
example contracts ship registered, with their ledgers and blessed fingerprints
— see [Examples](#examples) — so `make loop CONTRACT=tornado-ok` works from a
clean clone once the data is pulled.

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
| `persistence-last-year` | a sharp, fixed-level forecast | expected to *fail* on reliability, so the contract is seen rejecting something |
| `leaky-oracle` | reads the outcomes it is scored on | must be **REJECTED** by the leakage canary; this is the Phase 0 exit criterion |

## Why there is no model yet

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
| `inland-flood-la` | inland flood | one state (64 parishes) | quarter | dropped (county-coded hazard) | 7,680 units, base rate 8.0% |
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
   flood contract it has slight skill and misses the AUC floor; on tornadoes
   it discriminates well (AUC 0.82) and fails only on calibration, a 12-point
   miss in one bin; on monthly tropical cyclones it *passes* — the season is
   so sharp that knowing the region and the month clears every clause. Its
   skill score there is +0.009, because at a 0.66% base rate the pooled
   reference is already nearly right nearly everywhere. A passing contract
   is permission to spend one test touch, not a claim of a forecast.
3. **The persistence baseline is rejected on every contract**, for a different
   clause each time. On the tornado contract it is the only model that gains
   skill from last year's events; on tropical cyclones it has none.
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
validate or test year. `predict()` receives bare units. Labels are fetched in
`scoring.score()` *after* `predict()` has returned — one function, readable in
one sitting, which is the point.

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
resets when you restart the process is not a budget. Scoring against `test`
additionally requires an explicit `--spend-test-touch` flag.

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
join to a county universe, and the label builder drops them. `readiness panel`
reports exactly how many events went where, and warns when a hazard is mostly
zone-coded, so a thin panel is explained rather than mistaken for a rare
hazard. Joining zones to counties is the next step on the data plane.

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
pass it. Documented rather than papered over.

---

## Data

Every source is fetched, checksummed and pinned before use.
`snapshots/manifest.json` is committed; the ~300 MB of raw pulls are not, so a
clone reproduces the exact data version without carrying the bytes.

| layer | source | role |
|---|---|---|
| ground truth | NOAA Storm Events, 1996– | the validation target, every hazard |
| region universe | Census national county file | the panel denominator, any state or all |

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
readiness score MODEL       fit and score one model  [-c NAME] [--split, --spend-test-touch]
readiness loop              run the full experimental loop  [-c NAME] [--backend local|claude]
readiness canary            demonstrate the harness rejecting a leaked model  [-c NAME]
readiness ledger            show and verify the experiment ledger  [-c NAME]
readiness verify            check the Phase 0 exit criteria  [-c NAME] [--bless]
readiness dashboard         render a contract's ledger as a static HTML page  [-c NAME | --all]
readiness report            rebuild the static research report
readiness mcp               run the read-only MCP data server on stdio  [-c NAME]
```

`-c/--contract` takes a registered name or a path to a contract JSON. If it is
omitted the CLI uses `$READINESS_CONTRACT`, then the sole registered contract
if there is exactly one; with several registered it refuses and lists them.

### The agent backends

`--backend local` (default) is deterministic and uses no LLM: it walks a fixed
queue of candidates. This is what CI runs and what the bit-for-bit
reproducibility criterion is checked against — a loop whose control flow depends
on a language model cannot have a bit-for-bit criterion.

`--backend claude` runs the same loop with a Claude Agent SDK orchestrator and
the subagents from report §4 (a hazard analyst for the contract's hazard, a
calibration critic, a data steward). Needs `pip install 'readiness-loop[agent]'`
and an API key. The harness is unchanged; no subagent is granted a write tool.

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
  connectors/        data plane: base (pinning, HTTP), census, storm_events, mcp_server
  harness/           eval plane: metrics, splits, labels, scoring, contract, canary, ledger
  engine/            proposable models: climatologies, persistence, the canary target
  agent/             orchestrator + subagent definitions
  dashboard.py       the ledger rendered as a self-contained HTML page
  cli.py             the `readiness` command
contracts/           registered contracts, one JSON file each; three examples ship
experiments/         one directory per contract: ledger, anchor, test-touch budget
harness_expected/    blessed baseline fingerprints, one file per contract
snapshots/           pinned data; only manifest.json is committed
docs/                how-it-works.md (the walkthrough), contracts.md (the reference), media/
skills/              agent runbooks: verification protocol, experiment-card format
tools/               build_report.py (design -> report), demo/capture.py (docs media)
plans/               scenario library — the Phase 3 seed
design/              the imported Claude Design source (.dc.html) — source of truth
report/              index.html, compiled from design/ by tools/build_report.py
tests/               260 tests, no network required
```

---

## Roadmap

Phases 1–4 are specified in [report §6](report/index.html#roadmap). Each has a
falsifiable exit criterion, and the architecture is meant to absorb them without
redesign:

- **Phase 1 — the loop, on one hazard.** Real candidate models, ERA5 features,
  CLIMADA integration. *Exit: BSS > 0 on untouched test years with reliability
  within ±5 pts, published with the ledger.*
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
