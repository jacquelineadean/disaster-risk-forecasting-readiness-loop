# Plan: refactor, then Phases 1–4

Status: **living document.** Updated at the end of every phase. The design the
phases follow is in [`plan-design-annex.md`](plan-design-annex.md); this file
records what was decided, what was built, and what each phase still owes.

| phase | scope | status |
|---|---|---|
| R | refactor from the audit findings; reproducibility guard; CI | **done** (see §1.9) |
| 1 | the loop on one hazard: feature channel, real models, promote-to-test, backtest report | in progress (see §2.1) |
| 2 | multi-hazard, national, with exposure: fleet, exposure join, issuance, cited brief | built; exit needs the data run (see §3.1) |
| 3 | the planning thought-partner: facility record, scenario rules, gap report, reviews | built; exit needs three real facilities and their blinded reviews (see §4.1, review §4.2) |
| 4 | global scale-out: non-US ground truth and regions, global connectors, pilots | pending |

## 0. How the audit was done, and what it found

Seven readers, one per subsystem, read every line of their subsystem and its
tests and returned a module map, the invariants they saw enforced, and
findings with quoted evidence. Every finding was then given to two skeptics
(one for "is it real", one for "is it worth doing in a refactor that must keep
the ledgers bit-for-bit"); a finding survives only if neither refuted it. Of
116 raw findings, 76 survived and 40 were refuted. The adopted ones are listed
in §1 by cluster. A few refuted findings were adopted anyway because the fix
was cheap and made a later phase simpler (the engine base class, the single
`build_model` gate, the reliability flag reuse); a few confirmed ones were
declined, with the reason, at the end of §1.

Two facts constrain everything below:

- **The committed artefacts are the ground truth.** `experiments/*/ledger.jsonl`,
  the anchors, `harness_expected/*.json` and the three contract digests must
  not move. `ExperimentCard.payload()` hashes `asdict(self)`, so a new field on
  the card re-hashes every committed card: per-card additions go inside
  `data_snapshot`. `Contract.criteria()` hashes every non-label field, so a new
  contract field must be elided from the digest at its default.
- **This build session has no network to NOAA, Census, Open-Meteo or FEMA**, so
  nothing here can be re-run on real data. `tests/test_repro_guard.py` pins the
  synthetic fixture fingerprints (blessed before any harness edit) and re-checks
  the committed ledgers, card hashes and digests on every test run. The
  real-data criteria are checked by CI workflows that restore the pinned data
  from the Actions cache, and are reported as "needs the data run" here.

The invariants the readers found enforced, and which every phase keeps:
TrainingView is the only data channel and refuses holdout years; predict()
receives bare units; labels are read after predict() returns; nothing in the
harness imports an LLM client; the engine never imports the contract judge or
the canary; the reference forecast is one number computed one way; a scorecard
is never produced without a canary report; the ledger is chained and anchored;
the test-touch budget is on disk.

## 1. Phase R: the refactor

Findings are grouped by the cluster that fixes them. "Numbers" says whether the
fix can move a committed number (it never does; the guard proves it).

### R1. Harness core (`readiness/harness/`, `readiness/engine/`)

| finding | fix | numbers |
|---|---|---|
| `scoring.screen()` fits and predicts every model twice through two copied fit/predict sequences and builds a third TrainingView | fit once; the card and the canary arrays come from the same probabilities | identical (models are deterministic; guard) |
| `score(reference_probs=...)` is dead and is a path by which the reference could be overridden | remove the parameter | none |
| fitted/digest boilerplate copied across four models; a model that omits `training_digest` silently weakens the canary | `engine/base.py::FittedModel` owns `training_digest` and the "not fitted" guard; the four baselines subclass it; arithmetic untouched | identical (guard) |
| canary "train provenance" is *skipped*, not tripped, when a model declares no digest | trip it; every model now declares one through the base class | committed cards all declared a digest, so their verdicts are unchanged |
| `evaluate()` passes a card scored on the training split | new check `holdout split`: the card's split must be `validate` or `test` | committed cards are all `validate`; the check is on new cards only |
| the reliability "populated" rule is computed in `scoring` and again in `contract.evaluate` | `evaluate()` reads the card's own `populated` flag | identical rule |
| `build_panel` and `diagnose` duplicate the per-event pipeline and `data.build` runs both | one pass (`_walk_events`) feeds both; `build_panel` is a projection of it | identical panel digests (labels tests + guard) |
| `needs_panel` is a general side channel repeated at three call sites | `build_model(name, *, canary_panel=None)`: only a registered canary target receives a panel; call sites pass the panel once | none |
| `PersistenceLastYear` is constant for every holdout year after the first, and its docstring says otherwise | docstring and README corrected: it persists the last *training* year because no holdout label ever reaches a model; the numbers are what version 1.0.0 produces and stay | none |
| blessed fingerprints depend on CPython's `sum()` (3.12 compensated) | documented in `harness_expected/README.md`; CI checks bit-for-bit on 3.12 and runs the suite on 3.10 and 3.12; the sharpness test compares at 12 places. Applies to `metrics.py` and the Phase 0 baselines only: Phase 1 reduces through `math.fsum` (§2.2) | none |

### R2. Verification and the CLI (`readiness/verify.py`, `readiness/cli.py`)

| finding | fix |
|---|---|
| Phase 0 fingerprint logic is private to `cli.py` and re-implemented in the sandbox through underscore imports | `readiness/verify.py`: `repro_fingerprint()`, `REPRO_FIELDS`, `phase0()` returning checks; `cli` and the sandbox call it |
| the test-split touch is spent after the scorecard is printed | spent before scoring, so an interrupted run cannot score without paying |
| `loop --quiet` is parsed and never read | honoured |
| `register` re-declares every default that `contracts.new` owns and hard-codes them a third time in help text | the parser reads the defaults from `contracts.DEFAULTS` |
| the package version is hand-copied in three places | `readiness.__version__` is the single source; `pyproject.toml` reads it dynamically; the site build imports it |
| the manifest-key list that defines `data_version` is duplicated in `tools/build_site.py`; pinned-data presence logic is written three times | `data.input_keys(contract)` and `data.pinned(contract)`; `Dataset.provenance()` gains `inputs` (inside `data_snapshot`, so committed cards are untouched) |

### R3. Connectors (`readiness/connectors/`)

| finding | fix |
|---|---|
| `census.load` and `nws_zones.load` trust cached bytes without checking them against the manifest sha256 | cached bytes are hashed and compared; a mismatch is re-fetched, and refused on refusal to refetch |
| `Session` ignores `HTTPS_PROXY`, so `snapshot` cannot run behind a proxy where urllib would | honour `HTTP(S)_PROXY`/`NO_PROXY` with a CONNECT tunnel |

### R4. Agent plane (`readiness/agent/`)

| finding | fix |
|---|---|
| the claude backend and every subagent are granted Bash, which is a write channel to the harness, contracts and snapshots | `agent/guard.py`: a sha256 manifest of the harness, `contracts.py`, `config.py`, `verify.py`, `data.py`, `connectors/`, the guard, `contracts/` and `snapshots/` (the derived combined extract excepted) is taken before `run_claude` and checked after; every card is stamped with the digest of the guarded code at scoring time and checked against the pre-run digest, so edit-run-restore is caught; the cards that existed before the run must be intact afterwards; the system prompt's forbidden list matches |
| agent prompts hard-code Storm Events and county vocabulary | prompts read the contract's own `describe()`; Phase 4 supplies the rest |

### R5. Site and sandbox (`site/`, `tools/build_site.py`)

| finding | fix |
|---|---|
| `score_playground` lets the browser score the **test** split with no touch-budget check | the playground refuses any split but `validate` |
| dataset and base-scorecard caches keyed by contract name go stale after `register --force` | keyed by contract digest |
| the packer duplicates `data.py`'s layout knowledge | uses `data.pinned()` and `data.input_keys()` |

### R6. Tests and docs

| finding | fix |
|---|---|
| no tests for `verify`, `score`, `canary`, `panel`; `run_experiment`'s test-split path untested; MCP data tools never executed | tests against a synthetic `Dataset` injected in place of `data.build` |
| engine import rule is docstring-only | `tests/test_boundaries.py` walks the AST: engine never imports the judge or the canary; harness never imports the engine, the agent or an LLM client; every import under `readiness/` is stdlib or `readiness.*` except the SDK behind `try:` in `agent/` |
| README says 260 tests (277), misstates the tornado calibration miss (12 points; the ledger says 7.7), and runs the quickstart against the committed ledgers without naming `READINESS_EXPERIMENTS_DIR`; skills say every scored run writes a card (`score` does not) | corrected |
| no CI runs the test suite | `.github/workflows/test.yml`: the suite on 3.10 and 3.12, no network; `real-data.yml`: manual, restores the pinned-data cache and runs `snapshot` then `verify` for a contract with any extra arguments (the `--phase` flag arrives with Phase 1) |

### R7. The low-severity sweep

Confirmed low-severity findings worth carrying, done as one pass after R1–R6:

- `Ledger.append` writes the card and the anchor in two steps; a crash between
  them reads as truncation. The anchor is written atomically (temp file and
  rename) and the card line is flushed first.
- Card status (REJECTED / PASS / FAIL) is derived in three places and the
  card-to-JSON line is copied in three modules: both become methods on
  `ExperimentCard` (methods, not fields, so hashes are untouched).
- `readiness ledger --id` is ignored without `--show`; `dashboard --all` with
  `-c` or `-o` is silently partial; a JSON-RPC message that is valid JSON but
  not an object kills the MCP server; `Contract.from_spec` lets a `TypeError`
  or `ValueError` escape as a traceback; `make clean-derived` deletes the
  tracked report; `tests/test_labels.py` calls `unittest.main()` before its
  last class. All fixed.
- README: the flood contract's seasonal model fails reliability as well as
  the AUC floor; persistence is *failed*, not *rejected*; `inland_flood` is
  mixed-coded; the zone join exists (`connectors/nws_zones.py`) and is a third
  pinned layer; the repository map and skills list are completed; the install
  step precedes the `readiness` entry point. `experiments/README.md` and
  `harness_expected/README.md` say exactly what their files record.
- The site's register form and the playground re-state the contract defaults
  and the models' parameters in JavaScript; the build exports
  `contracts.DEFAULTS` and each model's parameter schema (added to
  `ModelSpec` for Phase 1's models) and the JavaScript reads them.

### Declined or deferred, with the reason

- *Ledger card hashes include a wall-clock timestamp, so `loop` is not
  reproducible run to run.* By design: the card is a record of when; the
  bit-for-bit criterion is on the scores, which `verify` checks. Documented.
- *The dashboard renderer is re-implemented in `site.js`.* The browser cannot
  import Python; `tests/test_site.py` pins the shared class names instead.
- *`readiness report` shells out to `tools/build_report.py`, which is not in the
  installed package.* The report is a repository artefact, not a package
  feature; left as is.
- *`readiness.engine` transitively imports `readiness.contracts` through
  `harness.labels`.* The rule is about the judge and the canary; the type
  import is harmless and the boundary test checks direct imports.
- *The scope digest is order-sensitive: the same states in another order is a
  different contract.* Normalising the order would change the committed
  five-state contract's digest. Duplicates are rejected; order is documented
  as significant.
- *`utc_now()` is defined three times.* Each plane keeps its own to avoid an
  import from the harness into the connectors or back.
- Everything filed as a Phase 1–4 blocker (feature channel, generic region and
  ground-truth sources, national scope in the site packer, hazard taxonomy) is
  handled in its phase below, not in R.

### 1.9. Phase R outcome

Done in three stages of parallel, file-disjoint clusters, each integrated
only when the whole suite and the reproducibility guard were green, then
reviewed adversarially: five review lenses (invariants, numbers, correctness,
tests, documentation), every finding handed to two skeptics. Four findings
survived and were fixed:

- the agent guard compared only the tree before and after a run, so an agent
  that edited a guarded file, scored, and restored it was invisible: every
  card now carries the digest of the guarded code at scoring time and is
  checked against the pre-run digest, and the cards that existed before the
  run must be intact afterwards;
- the guard did not cover the data plane that builds the labels: it now
  covers `data.py`, the connectors, the manifest and the pinned extracts
  (the derived combined extract excepted, since every build rewrites it);
- `run_experiment` spent the test touch after scoring while the CLI had been
  changed to spend it first; both now pay first;
- the guard hashed a derived file the first build of a run legitimately
  rewrites, which would have reported an honest run as tampered.

Sixteen more findings were refuted as immaterial, and the cheap ones were
taken anyway: a JSON-RPC request whose `params` is not an object no longer
kills the MCP server; `refresh` with fetching disallowed is an error, not a
download; `readiness loop --split test` can no longer spend a touch per queued
candidate silently (Phase 1 replaces it with `promote`); and the README, the
licence manifest, the verification protocol and this plan say what the guard
and the loop actually do. The suite went from 277 tests with one failure to
505 tests, green on Python 3.10 and 3.12 in CI; the committed ledgers,
fingerprints and digests are byte-identical to `main`.

## 2. Phase 1: the loop, on one hazard

**Exit (report §6):** BSS > 0 against climatology on untouched test years with
reliability within ±5 points per populated bin, published with the ledger.

**Decisions taken against the annex:**

- The feature channel is harness-owned (`readiness/harness/features.py`). The
  harness computes every cutoff itself from the unit's period start and the
  spec's lag; sources hand over raw monthly series and static tables and never
  per-row declarations. A closed vocabulary of transforms is the only code that
  touches a series. Static layers declare the last year of data they encode and
  are refused for any contract whose validate split starts at or before it, so
  FEMA NRI v1.20 (through 2023) is *refused* under every current contract and
  the refusal is a visible finding, not a bug. Label-origin sources are refused
  as features outright.
- History features (the shrunk seasonal rate) are computed inside `fit()` from
  training labels only, leave-one-year-out for training rows.
- Models: `logistic` (full-batch gradient descent, fixed iterations, no RNG),
  `gbm` (histogram boosted stumps, deterministic), and an isotonic calibrator
  fitted on the last three training years. Pure standard library so the
  sandbox keeps working.
- The test touch and the card are one atomic step: `readiness promote MODEL`
  refuses without a prior validate PASS for the same model, version and
  arguments, and refuses if a test card already exists under the contract
  digest. `score --split test` is withdrawn in favour of it.
- `verify --phase 1` is ledger-only: it re-derives the verdict from the stored
  scorecard, requires the passing test card to be the *first* test card, and
  requires the backtest report to embed that card's hash.
- Connectors: Census Gazetteer (centroids, water share), Open-Meteo ERA5
  monthly extract (precipitation, temperature, elevation), FEMA NRI (refused,
  shipped for the demonstration), and a pinned CLIMADA layer file produced by a
  tool outside the package. Each pins its bytes and declares its licence.
- Deferred: the CLIMADA subprocess model; AIWP reforecasts; the claude backend
  beyond wiring.

**Provable offline:** the firewall, admission, truncation audit, canary feature
provenance, model determinism, the calibrator's train-only construction, that
promote spends exactly one touch, that `verify --phase 1` accepts and rejects
the right synthetic ledgers. **Needs the data run:** whether any candidate
clears the contract on Louisiana or Oklahoma 2021–2025.

### 2.1. Phase 1 status

Built and integrated, green with the guard:

- `readiness/harness/features.py`: the channel and the firewall. One
  deliberate reading of "lag": the cutoff is the period's first month minus
  the lag, and only months strictly before it reach a transform, so the
  minimum lag of one withholds the month just before the period (a July
  forecast is built from data through May). The audit hands every transform
  the *uncut* series with every month at or after the cutoff poisoned and
  requires the same answer as the honest, cut run.
- The plumbing: `TrainingView(features=)` refuses a frame carrying a holdout
  unit and exposes `restrict(years)` for a calibrator; `PredictionRequest`
  carries the scored units' rows; `scoring` builds and audits the frame from
  units alone before the view exists and refuses to fit on an unclean audit;
  three trailing defaulted fields on `Scorecard`; the canary's fifth check.
- The engine: `seasonal_rates` extracted with identical arithmetic; history
  features leave-one-year-out; `logistic`, `gbm`, and isotonic or Platt
  `Calibrated` wrappers fitted on the last three training years. One
  deviation from the annex, found by measurement: in the boosted model the
  history logit is the boosting *offset*, not a split column, because trees
  read the leave-one-year-out artefact back into the labels when it is a
  column (history-only `gbm` scored below climatology as a column and
  reproduces the seasonal climatology as an offset).
- The connectors: Gazetteer centroids, Open-Meteo ERA5 monthly extract (the
  pinned artefact is the extract, resumable per state), FEMA NRI (declares
  `derived_through=2023`, refused under every current contract), the pinned
  CLIMADA layer with its out-of-package tool stub; the connector registry as
  data; `data.build(features=...)` with a separate `feature_version`.

- The loop: `PHASE1_QUEUE` (history-only logistic, logistic with the
  ERA5 antecedent set, with terrain, isotonic-calibrated, `gbm`, `gbm+iso`),
  candidates skipped with a progress line when their sources are not
  loaded, `readiness promote` as the one atomic test touch (refuses without
  a validate pass for the same model, version and arguments, refuses a second
  test card), `readiness features`, `readiness backtest` from committed files
  only, `readiness verify --phase 1` ledger-only with `--replay`.
- The site and the documentation: the feature catalogue and the backtest on
  the website, the sandbox refusing `promote` and `backtest` in the browser,
  `docs/features.md`, `docs/backtest.md`, the walkthrough section.

### 2.2. Phase 1 review outcome

Five lenses (leakage, numbers, correctness, tests, documentation) over the
whole Phase 1 diff; two findings were confirmed by both skeptics before the
review's verification budget ran out, and the rest were triaged by hand.
Fixed:

- **Interpreter-dependent numbers (high).** CPython 3.12 made `sum()` over
  floats compensated; 3.10 is a naive fold. Feature values, feature digests
  and boosted-tree splits therefore differed between the two interpreters in
  CI, and a card written under one could not be replayed under the other.
  Every float reduction in the feature transforms and the Phase 1 models now
  goes through `math.fsum`, which is exactly rounded and identical
  everywhere; a committed fingerprint of the four feature models on the
  fixture is checked on both interpreters. `metrics.py` and the Phase 0
  baselines are untouched, so `verify --replay` compares scorecard floats at
  a tolerance and reliability bins bin by bin rather than through an exact
  hash. The replay prints agreement per field, never the refit's test-split
  values.
- **A touch spent with no card (medium).** `promote` now refuses, before the
  budget is charged, when the candidate's sources are not loaded, when any
  test card already exists in the ledger under any digest, and when the
  validate pass was produced on a different data or feature version; the
  feature frame is built and audited before the spend; and `verify --phase 1`
  requires the touch file to hold exactly one touch for the promoted model.
- `promote` without arguments adopts the arguments of the model's latest
  validate pass, so `make promote` works; `loop --promote` is refused in the
  browser; the loop reports a promotion refusal as an exit code rather than a
  traceback; the ERA5 extract is refused when its bytes no longer match the
  manifest and starts two years before the first split so the first period's
  twelve-month window exists; the history feature is leave-one-year-out for
  the pooled and per-period totals as well as the cell; the canary's
  feature-provenance mismatch branch is tested; the documentation says where
  the feature provenance actually lives (on the scorecard).

What Phase 1 still owes is the real-data run: `real-data.yml` with
`phase=1` on a state contract, and a passing test card published with its
backtest.

## 3. Phase 2: multi-hazard, national, with exposure

**Exit:** at least four hazards pass the contract nationally; exposure joins
spot-validated against county assessor counts in ten sampled counties.

- Six national contracts registered as data; `readiness fleet` runs them
  sequentially through the Phase 1 queue (delivered as the Phase 2 queue,
  §3.1); per-contract ledgers and budgets.
- Exposure: FEMA/ORNL USA Structures counts per county and occupancy class,
  pinned per state; `ExposureTable` has no sub-county field by construction.
  Spot-check against a committed `assessor_counts.csv` with ratio bounds; rows
  outside the bounds do not count toward the ten, and the ten must come from
  at least three states, so one state's assessor convention cannot carry the
  criterion.
- Issuance: `readiness issue` refits the validated model through TrainingView,
  requires the training and feature digests to match the test card, builds the
  target period's features under the same firewall, and writes a probability
  per county. No parameter exists through which a label could arrive.
- Citations: `readiness/cite.py` validates that every sentence cites a claim,
  every claim resolves (a card, an issued file, a manifest key, a guidance
  document), every number in prose equals a cited value, and no forbidden
  phrasing ("will occur", "warning") appears. The county brief is written only
  if validation is clean, and never names anything below the county.

**Needs the data run:** which four hazards pass; the USA Structures layer
vocabulary; the ten assessor counts (collected by a person, with URLs).

### 3.1. Phase 2 status

Built, on the branch stacked above Phase 1, green with the guard (886 tests,
no skips):

- Six national contracts registered as data through the CLI, no hand edits
  (`inland-flood-us`, `tornado-us`, `hail-us`, `severe-wind-us` quarterly;
  `winter-storm-us`, `heat-us` monthly with the zone crosswalk); their
  digests are pinned by a test; none has a ledger yet.
- `readiness fleet`: sequential loops per contract with the Phase 2 queue
  (logistic, calibrated logistic, a capped boosted model and its calibrated
  form), continuing past a contract whose data is missing, promoting where a
  validate pass exists, and a ledger-only `--status` table; every card now
  records its wall-clock cost.
- Exposure: USA Structures county counts pulled per state through paged
  statistics queries and pinned, an occupancy mapping marked to confirm on
  the first real pull, a county table with no sub-county field by
  construction, and the assessor spot-check over a header-only committed CSV
  with a declared ratio band; rows outside the band never count toward the
  ten.
- `readiness/cite.py`: the six citation rules every human-facing document
  passes (uncited sentence, unknown claim, unresolved source, a cited value
  the artefact does not hold, number without a claim, forbidden phrasing),
  with identifier exemptions the document names and the fixed alerts
  disclaimer; `plans/guidance.json` registers the guidance documents.
- `readiness issue`: refits the validated model through the training view,
  requires the training and feature digests to match the test card, builds
  and audits the target period's frame under the firewall, refuses a period
  inside the years the contract spans and one the series data does not yet
  reach, refuses to overwrite an issued file without `--reissue`, and has no
  parameter through which a label could arrive (a test flips every holdout
  label and gets a byte-identical file).
- `readiness brief`: one paragraph per contract covering the county, every
  number a cited claim, exposure stated or its absence stated (never
  substituted), the alerts disclaimer citing its guidance entry, written only
  when validation is clean; never below the county, never "would touch".
- `readiness verify --phase 2`, the briefs page on the site, `docs/brief.md`.

Four decisions taken while building: the fleet's default queue is the Phase
2 queue, not Phase 1's, so `readiness fleet` and `make loop-all` run the four
national candidates without being told to; the exposure spot-check needs its
ten in-band counties to come from at least three states, so one state's
assessor convention or layer vintage cannot carry the criterion on its own;
the "issued" check accepts the period label the most passing contracts
issued (quarterly and monthly contracts cannot share one label) and requires
at least four of them to name their first test card; and the
missing-exposure sentence cites a computed claim derived from the
paragraph's own probability claim, because an absence has no artefact to
point at.

What Phase 2 still owes is the data run: which four hazards pass nationally,
the USA Structures layer's vocabulary confirmed on the first pull, and the
ten assessor counts collected by a person with their URLs.

### 3.2. Phase 2 review outcome

Three lenses (leakage and the county floor, correctness of the numbers and
the guards, tests and documentation) over the whole Phase 2 diff; sixteen
findings confirmed with reproductions, all fixed on the same branch (925
tests, no skips). Fixed:

- **A citation that only checked existence (high).** `cite.validate` resolved
  a claim's source and stopped; a brief could cite the right row and print
  the wrong number. New code `VALUE_MISMATCH`: the resolver returns the leaf
  a reference lands on and the artefact's value must equal it (floats at
  1e-9, integers and strings exactly); computed claims must bottom out in a
  non-computed source, and mutually computed claims are unresolved. The bare
  five-digit exemption that let any FIPS-shaped number through is gone: a
  document names its own identifiers, and the brief passes its county.
- **Issuance without bounds (high).** `readiness issue` accepted a period
  inside the years the contract spans, which would have issued a probability
  for a period whose labels exist; it now refuses anything before the first
  period after the contract's last year, names that period, and a model with
  no feature series may issue exactly that one period. An issued file is
  never overwritten without `--reissue`, which records what it replaced.
- **The brief's card guard (high).** `brief.build` accepted any card as
  `validated_by`; it now requires a passing, canary-clear test card whose
  contract digest matches the issued file's, cites the issued probability by
  county so the value check applies, reads the tolerance off the card and
  carries the reading caveat in its footer; `verify`'s brief check requires
  the document's own kind, county and period before it validates, and the
  site validates briefs through `brief.check` and publishes the page rendered
  from the validated document, not a committed sibling.
- **USA Structures paging (medium).** Paging ended on a page as full as we
  asked for rather than on the server's transfer-limit flag, so a layer with a
  smaller page size pinned one page as a whole state; a FIPS longer than five
  digits could be truncated into a county by `int()`. Both refused now, and
  `ExposureTable` checks every key is five digits of its own state.
- The fleet reports a promotion refusal as a reason beside the loop that ran
  rather than as a failed dataset; the browser refuses `fleet --promote` and
  every argparse abbreviation of it; the spot-check's detail leads with its
  verdict; the Makefile's `phase2` continues past a contract without data and
  `issue` takes the period flag; the fleet step of the real-data workflow is
  continue-on-error; the documentation says `wall_clock_s` is inside the
  hashed payload, names the Phase 2 queue as the fleet's default, and states
  the four guards and the three-state spot-check clause.

Two findings closed only as far as the artefacts allow: no card records the
test years it applied, so the brief reads them from the contract the card,
the issued file and the brief already share by digest; and the Makefile and
the workflow have no automated test, only `make -n` and inspection.

## 4. Phase 3: the planning thought-partner

**Exit:** blinded review by practising emergency managers of gap reports for at
least three real facilities rated useful or better; every recommendation
traces to a source or a computed number.

- `readiness/plans/`: a facility record with no address or coordinate field
  (design intensity is supplied by the planner from an elevation certificate
  or FIRM, cited as a facility document); the 96-hour scenario as JSON beside
  its markdown, with a test that keeps the markdown the specification; rules
  that answer each scenario question from the record and the issued risk
  layer; fail-closed when the design intensity is missing, with no path that
  substitutes a county number.
- The gap report is a cited document validated by the same `cite` rules; a
  blinded rendering replaces names with a hash; a review record binds a rating
  to the sha256 of a blinded report. Real facility files and reports never
  enter git; a test refuses any committed plan JSON with address or
  coordinate keys.
- Case studies are JSON with sources per fact and expected findings, run as
  regression tests; none ships, the mechanism is tested on a synthetic one.

**Not mechanisable:** that the facilities are real and the reviewers are
practising emergency managers. The review record is an attestation bound to a
specific blinded report, and `verify --phase 3` says so.

### 4.1. Phase 3 status

Built, on the branch stacked above Phase 2, green with the guard (1,236
tests, no skips, after the review round in §4.2; the docs tests' parser
guards for all three CLI clusters assert rather than skip, and the README
map's pending set is empty):

- `readiness/plans/facility.py`: the record refuses unknown keys by name and
  requires every populated leaf to be named by an evidence entry (document
  and page), so every sentence a rule writes cites a field path that resolves
  back to a document. No address, coordinate, tract, block or parcel field
  exists; a test walks every committed plan JSON for such keys.
- `readiness/plans/scenarios.py`: the 96-hour scenario as JSON beside its
  markdown; `questions_from_markdown` re-reads the "must answer" bullets and a
  test asserts the two agree in order, so the markdown stays the
  specification. Constants are named claims (`96h-isolation-acute-care/
  isolation_hours`) rather than digits typed into prose.
- `readiness/plans/rules.py`: one rule per question, four statuses
  (`answered`, `unanswered`, `failed`, `cannot_run`). `switchgear_vs_intensity`
  never reads the risk layer; a missing design intensity is one `cannot_run`
  finding naming the Elevation Certificate. `readiness/plans/risk.py` reads
  `issued/` files only, never fits or scores, and returns an explicit
  absence claim where nothing validated covers the county.
- `readiness/plans/gap_report.py`: the brief's shape and the same five `cite`
  rules; a clean run writes the document JSON and the page under
  `<out>/<slug>/` and the blinded page under `<out>/blinded/<label>/`, and a
  violation writes nothing. `readiness/plans/reviews.py` binds a rating to
  the sha256 of the blinded page and recomputes it from the file.
- `readiness/plans/case_studies.py`: facts cite a source index, expected
  statuses are the test; none ships, the synthetic one in
  `tests/fixtures_plans.py` exercises the mechanism.
- `readiness/plans/draft.py` is a deterministic template per finding; the
  optional `claude` drafter (`readiness/agent/planner.py`, lazy SDK import)
  may only rewrite sentences that keep their markers exactly, and a rewrite
  that fails the rules is refused: the sentence keeps its local wording and
  the provenance line counts the refusals.
- `readiness scenarios`, `gap-report`, `review record`, `verify --phase 3`;
  `docs/plans.md`; how-it-works §13.

Decisions taken while building, each recorded in the module docstring that
owns it:

1. A question the record cannot settle is `unanswered`, a finding in its own
   right rather than a gap in the report: the campus seam with no co-located
   operator named, an evacuation trigger with no authority or lead time, a
   priority order with no author or date.
2. The blinded label is `FACILITY-` plus twelve hex characters of a random
   `blind_id` the planner generates once into the record (§4.2; it was a
   hash of the slug at first), partners are `PARTNER-n`, counties `COUNTY-x`
   and documents `DOCUMENT-n`, assigned from the record in a fixed order and
   used in prose by every rule, so blinding is structural; the page carries a
   `readiness-blind` meta tag holding the label, the period and the kind, so
   `review record` can bind a rating without being handed anything a reviewer
   should not have, and refuses a page without the tag.
3. `gap_report.write` takes its resolver as an argument rather than deriving
   one from the document: a resolver built from the document's own claims
   would accept whatever the document said.
4. `build(rewriter=...)` is how the `claude` drafter is injected, so the
   tests exercise the refuse-and-keep path with a fake function and the SDK
   is never imported by the suite.
5. Case studies run the rules against an empty risk layer and state the
   absence in prose, exactly as a live report does for a county nothing has
   been issued for.

What Phase 3 still owes is the part that is not mechanisable: three real
facility records prepared by their planners, their gap reports, and blinded
reviews by practising emergency managers. `verify --phase 3` counts only
reviews whose sha re-finds a blinded report and says in its own output that it
cannot establish that a facility is real or that a reviewer practises
emergency management.

### 4.2. Phase 3 review outcome

Three lenses (leakage and blinding, correctness of the rules and the
citations, tests and documentation) over the whole Phase 3 diff;
forty-five findings confirmed with reproductions, all fixed on the same
branch (1,236 tests, no skips). Fixed:

- **Blinding that was a string replacement (high).** Names were written
  into prose and then replaced in the blinded page by exact match, so a
  doubled space, a re-cased or shortened name from the `claude` drafter, or
  a name containing a digit or the word "alert" either leaked or made the
  report unwritable; the label was six hex characters of the slug's hash,
  recovered from the page by a dictionary search in milliseconds; the
  county FIPS and the slug-named directory stayed on the blinded page; and
  `verify --phase 3` printed the label beside the slug. Now no rule writes a
  name: partners, counties and documents are `PARTNER-n`, `COUNTY-x` and
  `DOCUMENT-n` from the record, names live only in claim text that the
  blinded document cuts, the unblinded page carries a legend, the label is
  twelve hex characters of a random `blind_id` in the record, the blinded
  page is written under `blinded/<label>/` with no timestamp so a re-run on
  identical inputs keeps its sha, `render_blinded` refuses a page in which
  any identifying string from the record survives, and `verify` prints
  labels and sha prefixes, never a path.
- **A review bound to a page but not to its document (high).** The sha
  bound the blinded page, but `verify` re-validated a sibling JSON found by
  file name, never compared the review's facility label with the page, and
  one report with three hand-written reviews satisfied "three facilities".
  `verify` now re-renders every report JSON, matches reviews by sha, and
  requires the label, period and kind on the page to be the review's.
- **The drafter could delete findings (medium).** A refused rewrite was
  dropped, so a model returning nothing usable left an empty, validated
  report with its `cannot_run` finding gone. A refused rewrite now keeps
  the local sentence; markers must match exactly; URLs and any digit run not
  in the original or a cited value are refused; the count of refusals is on
  the provenance line. A county FIPS still reaches the model inside claim
  ids it must keep verbatim, and the blinded render re-keys those ids.
- **Rules at the boundary.** Equipment exactly at the design flood elevation
  is in the water (`<=`); an under-declared scenario yields `cannot_run`
  rather than a traceback; a rule bound to two questions is refused; a null
  flood-elevation source is fail-closed; the correlated-failure sentence
  cites the flag it turns on and names every partner in a shared county;
  negative hours and elevations are refused at load; numbers render without
  an exponent.
- **The committed set.** `.gitignore` covers subdirectories and every
  extension under the facilities, reports and reviews directories, and the
  tests read `git ls-files`; the forbidden-key scan grew to twenty-six keys
  and reads string values (a street address, a ZIP+4, a decimal-degree
  pair), and case studies refuse unknown keys; `--period` is validated and
  the meta tag escaped, so a label can no longer be a path; the three files
  are written atomically; a second review of one report no longer
  overwrites the first.
- **Boundaries.** `tests/test_boundaries.py` records `from X import Y`
  aliases, so a rule can no longer be bypassed by import style; the plans
  row forbids the panel, the engine, features, metrics, scoring, labels and
  the orchestrator; the reverse rule keeps the harness from importing the
  plans. The one genuine violation it surfaced, `risk.py` importing the
  panel builder for a directory name, is fixed; `RiskLayer` also keeps only
  a card's id, model and version, never its scorecard.
- Docs: the case-study README example loads through the loader; the
  Roadmap's Phase 3 entry has the Built/Remaining/Exit shape; every doc that
  quotes the forbidden keys quotes all of them, and a test says so.

Behaviour a user of the branch will notice: a facility record needs a
`blind_id`; a review record's `facility_hash` is `facility_label`; blinded
pages moved directory, so reports are regenerated before their reviews
count again.

## 5. Phase 4: global scale-out

**Exit:** the Phase 1 contract passes in two non-US pilots using only globally
available data.

- Contract schema gains `ground_truth` and `regions` sources, elided from the
  digest at their US defaults so the three committed digests do not move.
- Connectors: geoBoundaries regions with centroids; partner national records
  (CSV, pinned by hash, bytes never committed); EM-DAT export read with the
  standard library. Open-Meteo and the firewall are already global.
- The label builder accepts a generic `RecordEvent`; US panels are bit-identical.
- `verify --phase 4` requires two non-US contracts to pass the Phase 1 checks
  with every input resolving to a connector flagged as globally available.

**Needs the data run:** the partner records or EM-DAT export, a geoBoundaries
release, the ERA5 pulls, and whether the contracts pass.

## 6. Order of work, and how it is delivered

1. Phase R, in the cluster order above; the guard stays green throughout.
2. Phase 1 harness (`features.py`, plumbing, canary check 5), then connectors,
   then engine, then orchestrator and `promote`, then the backtest report and
   `verify --phase 1`.
3. Phase 2, 3, 4 as sections 3–5.

After each phase: the suite green on the working tree, this file's status
table updated, one commit per coherent change.

Delivery is a stack of pull requests, each reviewable on its own and each
based on the one below it:

| PR | branch | contains | base |
|---|---|---|---|
| #11 | `claude/affectionate-lovelace-bnu9tz` | the guard, this plan, Phase R and Phase 1 | `main` |
| #12 | `…-phase2` | Phase 2: national contracts, fleet, exposure, citations, issuance, the brief | #11 |
| #13 | `…-phase3` | Phase 3: facility record, scenarios, rules, gap report, blinded reviews, case studies | #12 |
| next | `…-phase4` | Phase 4: contract schema, global connectors, pilots | the Phase 3 branch |

A fix to a lower PR is made there and the branches above it are rebased.
