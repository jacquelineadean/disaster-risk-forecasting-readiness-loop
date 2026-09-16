# Plan: refactor, then Phases 1–4

Status: **living document.** Updated at the end of every phase. The design the
phases follow is in [`plan-design-annex.md`](plan-design-annex.md); this file
records what was decided, what was built, and what each phase still owes.

| phase | scope | status |
|---|---|---|
| R | refactor from the audit findings; reproducibility guard; CI | in progress |
| 1 | the loop on one hazard: feature channel, real models, promote-to-test, backtest report | pending |
| 2 | multi-hazard, national, with exposure: fleet, exposure join, issuance, cited brief | pending |
| 3 | the planning thought-partner: facility record, scenario rules, gap report, reviews | pending |
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
| blessed fingerprints depend on CPython's `sum()` (3.12 compensated) | documented in `harness_expected/README.md`; CI checks bit-for-bit on 3.12 and runs the suite on 3.10 and 3.12; the sharpness test compares at 12 places | none |

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

## 3. Phase 2: multi-hazard, national, with exposure

**Exit:** at least four hazards pass the contract nationally; exposure joins
spot-validated against county assessor counts in ten sampled counties.

- Six national contracts registered as data; `readiness fleet` runs them
  sequentially through the Phase 1 queue; per-contract ledgers and budgets.
- Exposure: FEMA/ORNL USA Structures counts per county and occupancy class,
  pinned per state; `ExposureTable` has no sub-county field by construction.
  Spot-check against a committed `assessor_counts.csv` with ratio bounds; rows
  outside the bounds do not count toward the ten.
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

## 6. Order of work

1. Phase R, in the cluster order above; the guard stays green throughout.
2. Phase 1 harness (`features.py`, plumbing, canary check 5), then connectors,
   then engine, then orchestrator and `promote`, then the backtest report and
   `verify --phase 1`.
3. Phase 2, 3, 4 as sections 3–5.

After each phase: the suite green on the working tree, this file's status
table updated, one commit per coherent change, pushed to the PR branch.
