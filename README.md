# The Readiness Loop

An open-source, agentic system that forecasts natural-disaster risk from public
data, **validates its own probabilities against history**, and iterates until
they can be trusted — then turns them into emergency plans.

The research briefing that specifies this is in [`report/index.html`](report/index.html)
(open it, or `make serve`). Everything below is the implementation.

**Status: Phase 0 complete.** The eval plane is built and its exit criteria are
met, on real NOAA data. There is no forecasting model yet, on purpose — see
[Why there is no model yet](#why-there-is-no-model-yet).

---

## Quickstart

No dependencies. Python 3.10+.

```bash
make snapshot   # pull and pin ~300 MB of public data (once, ~3 min)
```

```bash
make loop       # run the full experimental loop
```

```bash
make verify     # check the Phase 0 exit criteria
```

```bash
make test       # 129 tests, no network needed
```

---

## What Phase 0 actually does

The forecast unit is deliberately small and auditable:

> **P**(at least one damaging inland-flood event in county *C* during quarter *Q*)

"Damaging" is fixed in [`readiness/config.py`](readiness/config.py): property
damage ≥ $10,000, or any injury or death, per NOAA Storm Events. Louisiana,
1996–2025, 64 parishes × 30 years × 4 quarters = **7,680 labelled
county-quarters**, base rate 7.98%.

`make loop` runs `gather context → take action → verify work → repeat` over a
queue of candidate models and writes an experiment card for each. Real output:

```
id        model                             split           BSS     AUC  verdict
exp-0001  climatology-global@1.0.0          validate    +0.0000  0.5000  FAIL
exp-0002  climatology-county-quarter@1.0.0  validate    +0.0169  0.5957  FAIL
exp-0003  persistence-last-year@1.0.0       validate    -0.0526  0.5012  FAIL
exp-0004  leaky-oracle@1.0.0                validate    +1.0000  1.0000  REJECTED

ledger chain intact: 4 card(s)
```

Read that table as four assertions about the harness, not four attempts at a
forecast:

1. **`climatology-global` scores exactly 0.0000 skill and exactly 0.5000 AUC.**
   It is the reference forecast scored against itself. Any other number would
   mean the yardstick is bent. It still **fails** the contract, because the
   contract says `BSS > 0` and being climatology is not beating climatology.
2. **`climatology-county-quarter` beats it (+0.0169) and still fails.** It has
   genuine skill — Louisiana flooding is seasonal and spatial — but its AUC of
   0.60 is below the 0.70 floor, and its 0.2–0.3 bin observes 0.127 against a
   forecast of 0.230, a 10-point calibration miss where the contract allows 5.
   A weaker harness would have called this a success.
3. **`persistence-last-year` scores −0.0526: worse than climatology.** It is in
   the queue precisely so the contract is seen rejecting something obvious.
4. **`leaky-oracle` scores a perfect +1.0000 / 1.0000 and is REJECTED.** It is a
   model constructed with direct access to the outcomes it is scored on. This is
   the Phase 0 exit criterion.

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

`make verify` checks both criteria:

```
[ok]   splits are disjoint
[ok]   climatology baselines reproduce bit-for-bit
[ok]   leakage canary rejected leaky-oracle
         (tripped: implausible skill, implausible auc, outcome agreement, train provenance)
[ok]   ledger chain intact: 4 card(s)

Phase 0 exit criteria met.
```

---

## Architecture

Four planes, one contract. **The agent proposes; the harness disposes.**

```
readiness/connectors/   data plane    fetch, checksum and pin public sources
readiness/engine/       the models    everything the agent may propose
readiness/agent/        agent plane   orchestrator + subagents; writes cards
readiness/harness/      eval plane    scores them. No LLM. No agent writes.
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

### The contract

Pre-registered, hashed, and recorded on every experiment card. Change a
threshold and the hash changes, which marks every prior experiment as visibly
incomparable — that is the intended cost, not a bug.

```
contract        1.0.0  (sha256:36b0a1660c421816)
hazard          inland_flood  ['Flood', 'Flash Flood']
geography       LA, county x quarter
damaging event  property >= $10,000 or any casualty
train           1996-2015  (20y)     fit here; the 20-year climatology window
validate        2016-2020  (5y)      iterate freely
test            2021-2025  (5y)      1 touch, per model version, ever
reference       climatology-global
passes when     BSS > 0.0  |  reliability within +/-5% per populated bin (n >= 30)
                |  AUC >= 0.70
```

The test-touch budget is persisted to disk, not held in memory — a budget that
resets when you restart the process is not a budget. Scoring against `test`
additionally requires an explicit `--spend-test-touch` flag.

### The ledger

`experiments/ledger.jsonl` is hash-chained: each card carries the hash of the
one before it, so edits, reorderings and mid-file deletions break the chain and
`verify()` reports where.

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
| ground truth | NOAA Storm Events, 1996– | the validation target |
| county universe | Census national county file | the panel denominator |

Report §7 warns that federal series can stop — NOAA retired its Billion-Dollar
Disasters product in May 2025 with no updates beyond CY2024. Hence the pinning,
and hence `--keep-raw` for mirroring what licences allow. Per-layer licences and
attribution: [`DATA-LICENSES.md`](DATA-LICENSES.md).

A cached file with no manifest record counts as *unpinned* and is re-fetched
rather than used. An experiment that cannot name its data version is not an
experiment.

### Something the data says out loud

```
train     1996-2015   n=5,120   positives=434   base=0.0848
validate  2016-2020   n=1,280   positives=112   base=0.0875
test      2021-2025   n=1,280   positives= 67   base=0.0523
```

The test-period base rate is ~38% below the training period. Some of that is
Storm Events reporting practice rather than weather (report §7: "Storm Events
reflects reporting practice as much as weather"). A model tuned on 1996–2015
frequencies will be systematically over-confident on 2021–2025 no matter how
well it is calibrated in-sample. This is visible here only because the panel is
dense and the splits are locked, which is the argument for building the eval
plane first.

---

## Commands

```
readiness contract          print the pre-registered contract and its hash
readiness models            list proposable models
readiness snapshot          pull and pin the data, print the manifest
readiness panel             build the labelled panel, print split coverage
readiness score MODEL       fit and score one model  [--split, --spend-test-touch]
readiness loop              run the full experimental loop  [--backend local|claude]
readiness canary            demonstrate the harness rejecting a leaked model
readiness ledger            show and verify the experiment ledger
readiness verify            check the Phase 0 exit criteria
readiness report            rebuild the static research report
readiness mcp               run the read-only MCP data server on stdio
```

### The agent backends

`--backend local` (default) is deterministic and uses no LLM: it walks a fixed
queue of candidates. This is what CI runs and what the bit-for-bit
reproducibility criterion is checked against — a loop whose control flow depends
on a language model cannot have a bit-for-bit criterion.

`--backend claude` runs the same loop with a Claude Agent SDK orchestrator and
the subagents from report §4 (hazard analyst per peril, calibration critic, data
steward). Needs `pip install 'readiness-loop[agent]'` and an API key. The
harness is unchanged; no subagent is granted a write tool.

### MCP

`readiness mcp` serves the pinned data and the ledger over MCP (stdio,
standard-library JSON-RPC, no dependencies):

```json
{"command": "python3", "args": ["-m", "readiness.cli", "mcp"]}
```

Every tool is a read. `get_county_history` exposes **training years only**.
There is no tool that returns a holdout outcome, and a test asserts it.

---

## Repository map

```
readiness/
  config.py          the pre-registered contract. Hashed. Do not edit while iterating.
  connectors/        data plane: base (pinning, HTTP), census, storm_events, mcp_server
  harness/           eval plane: metrics, splits, labels, scoring, contract, canary, ledger
  engine/            proposable models: climatologies, persistence, the canary target
  agent/             orchestrator + subagent definitions
  cli.py             the `readiness` command
harness_expected/    blessed baseline fingerprints (the reproducibility target)
experiments/         the append-only ledger, its anchor, and the test-touch budget
snapshots/           pinned data; only manifest.json is committed
skills/              agent runbooks: verification protocol, experiment-card format
plans/               scenario library — the Phase 3 seed
design/              the imported Claude Design source (.dc.html) — source of truth
report/              index.html, compiled from design/ by tools/build_report.py
tests/               129 tests, no network required
```

---

## Roadmap

Phases 1–4 are specified in [report §6](report/index.html#roadmap). Each has a
falsifiable exit criterion, and the architecture is meant to absorb them without
redesign:

- **Phase 1 — the loop, on one hazard.** Real candidate models, ERA5 features,
  CLIMADA integration. *Exit: BSS > 0 on untouched test years with reliability
  within ±5 pts, published with the ledger.*
- **Phase 2 — multi-hazard, national, with exposure.** One subagent per peril
  against the same harness; join to USA Structures so outputs become human.
- **Phase 3 — the planning thought-partner.** The Memorial case study becomes a
  regression test: any plan the system blesses must survive the scenario that
  killed people there. Seeded in [`plans/`](plans/).
- **Phase 4 — global scale-out.** Swap US layers for Open Buildings, Flood Hub,
  EM-DAT. The architecture does not change; the connectors do.

---

## What this is not

**This is decision support. It is never a warning channel.** Official alerts
come from the National Weather Service and IPAWS. Quarterly county probabilities
must never be phrased as predictions of specific events, and no output here
should be used to decide whether to evacuate for a storm that is coming.

The full set of risks the design commits to carrying — overfitting the loop,
biased and drifting ground truth, LLM prose in life-safety documents, licensing
hygiene, and the harm that publishing address-level risk can do to the people it
is meant to help — is in [report §7](report/index.html).
