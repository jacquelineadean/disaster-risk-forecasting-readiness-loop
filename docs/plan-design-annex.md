# Design annex: Phases 1–4 as synthesized by the design panel

Status: reference material for [`plan.md`](plan.md). Three independent designs (MVP-first, invariant-first, operator-first) were judged on invariant preservation, falsifiability, fit, completeness and offline feasibility and merged into the document below. Where `plan.md` says otherwise, `plan.md` wins: it records the scoping decisions actually taken and the status of each phase.

---


Base: the operator-first design. Grafted: the harness-owned temporal firewall and static-admission rule (invariant-first), the repro guard and `feature_version`-outside-`data_version` rule (invariant-first), leave-one-year-out history features, PAV recalibration inside training years, the `verify.py` extraction with verdict re-derivation, the pinned-derived-extract rule and 3.12 fingerprint note (mvp-first). Every fatal flaw a judge found is closed in §0 or in the phase that owned it; the closures are marked **[fix]**.

Facts this design relies on, checked against the repo: `ExperimentCard.payload()` is `asdict(self)` minus `card_hash`, so **any new dataclass field on `ExperimentCard` re-hashes every committed card**; new per-card data goes inside the existing free-form `data_snapshot` dict. `Contract.criteria()` is `asdict(self)` minus `LABELS`, so new contract fields must be elided at their default. `cmd_score --split test --spend-test-touch` spends the budget but writes no card; only `orchestrator.run_experiment` writes cards. `TouchBudget` is keyed per `model@version`. `TrainingView(panel, split)` has no subset API. `Dataset.provenance()` carries `data_version` (a hash) but no key list. The suite has one pre-existing failure on 3.10/3.11 (`sharpness([0.3]*50) == 2.8e-16`); it passes on 3.12, which is what `site.yml` and the blessed fingerprints use. Committed contract digests: `inland-flood-la 477c493035c9c70d`, `tornado-ok 827313e1273a5e76`, `tropical-cyclone-gulf d01b00aa1baedc98`.

---

## 0. Cross-cutting prerequisites (one PR, before Phase 1)

### 0.1 Repro guard (hard constraint 3, made a test)

`tests/test_repro_guard.py`:
- `test_committed_ledgers_verify`: every `experiments/*/ledger.jsonl` → `Ledger.verify().valid`, anchor agrees.
- `test_committed_card_hashes_survive_round_trip`: each committed line → `ExperimentCard(**json)` → `compute_hash()` equals stored `card_hash` (catches any accidental field added to the dataclass).
- `test_committed_contract_digests_are_stable`: literals `477c493035c9c70d`, `827313e1273a5e76`, `d01b00aa1baedc98`, and each equals `harness_expected/<name>.json["_contract"]`.
- `test_repro_fields_are_frozen`: `cli._REPRO_FIELDS` equals a frozen literal tuple; `harness_expected/*.json` per-model key sets equal `_REPRO_FIELDS + ("reliability_bins_sha256",)`.
- `test_synthetic_baselines_reproduce_bit_for_bit`: `tests/expected/synthetic_fingerprints.json` (blessed **before** any harness edit) holds, for `make_contract() × make_panel(n_regions=12)` and `make_contract(period="month") × make_panel(rare=True)`: `cli._repro_fingerprint` of `climatology-pooled` and `climatology-seasonal` on validate, `Panel.digest()`, `TrainingView.digest`, sha256 of the seasonal probability vector. The offline stand-in for `readiness verify` against NOAA data.

Rule, written into `readiness/harness/__init__.py`: new fields on `Scorecard` get defaults and never enter `_REPRO_FIELDS`; new per-card data goes in `data_snapshot`, never as an `ExperimentCard` field; new `Contract` fields are elided from `criteria()` at their schema-v1 default; `Dataset.data_version` hashes only the panel's inputs — features get their own `feature_version`.

### 0.2 Contract digest stability under schema growth

`readiness/contracts.py`: `SCHEMA_V1_DEFAULTS: dict[str, object] = {}` (Phase 4 fills it). `criteria()` pops any key whose value equals its entry. Test above pins the three digests.

### 0.3 The temporal firewall — `readiness/harness/features.py` **[fix: audit is no longer self-attested]**

Harness-owned, stdlib, imports nothing from engine/connectors. The harness computes every cutoff itself; sources hand over raw series, never per-row declarations.

```
MonthIndex = int                      # year*12 + (month-1)
def period_start(year, period, ppy) -> MonthIndex
class Series:                          # frozen; months sorted ascending
    region: str; months: tuple[int,...]; values: tuple[float,...]
    def before(self, cutoff: MonthIndex) -> "Series"      # strict <, bisect
class FeatureSource(Protocol):
    name: str; kind: Literal["series","static"]
    manifest_keys: tuple[str,...]      # pinned inputs this source was built from
    derived_through: int | None        # static: last YEAR of data the layer encodes; None = timeless physical geometry
    global_coverage: bool
    def series(self, region) -> Series | None
    def static(self, region) -> dict[str, float] | None
@dataclass(frozen=True) class FeatureSpec:
    column: str; source: str; transform: str; window_months: int = 0
    lag_months: int = 1; static_key: str = ""
    # __post_init__: transform in TRANSFORMS; lag_months >= 1 for series; window_months >= 1 for series
TRANSFORMS = {"trailing_sum", "trailing_mean", "trailing_max", "same_months_mean", "static"}
    # each Callable[[Series, FeatureSpec], float]; closed vocabulary; the only code that touches a Series
@dataclass(frozen=True) class FeatureFrame:
    columns: tuple[str,...]; rows: dict[Unit, tuple[float,...]]     # nan = missing, never 0
    specs: tuple[FeatureSpec,...]; source_keys: tuple[str,...]
    def digest() -> str            # over columns+rows+specs, 16 hex
    def restrict(units) -> FeatureFrame
    def missing_share() -> dict[str, float]
def build_frame(specs, sources: Mapping[str, FeatureSource], units: Sequence[Unit], ppy: int) -> FeatureFrame
    # takes UNITS, never a Panel: for unit (R,Y,P): cutoff = period_start(Y,P,ppy) - spec.lag_months;
    # transform receives source.series(R).before(cutoff) only
LABEL_SOURCE_PREFIXES = ("noaa/storm_events/", "records/", "emdat/", "desinventar/")
def admit(source, contract) -> None    # raises FeatureAdmissionError
    # static: derived_through is not None and derived_through >= contract.validate_years[0] -> refused
    # any kind: manifest_keys intersect LABEL_SOURCE_PREFIXES -> refused (label-origin rule)
    # static with derived_through None: allowed only if source.name in TIMELESS_STATIC_SOURCES
    #   (harness-owned allow-list: {"gazetteer", "elevation"}); anything else must declare a year
def audit_frame(specs, sources, units, contract, frame) -> FeatureAudit(findings, clean)
    # 1 "static admission": admit() for every source used
    # 2 "label origin": as above, reported separately
    # 3 "timestamp bound": rebuild every row with _Truncated(source, cutoff) — a wrapper whose series()
    #   physically drops months >= cutoff BEFORE the transform — rows must be bit-identical
    #   (guards a future transform that ignores its slice; the negative test injects such a transform)
    # 4 "coverage": nan share per column (informational)
```

Harness plumbing (all defaults keep Phase 0 paths byte-identical; fixture 0.1 proves it):
- `TrainingView.__init__(panel, split, features: FeatureFrame | None = None)`; `.frame()` raises `SplitViolation` if the frame holds a non-train unit; `.feature_digest` (`""` when none); `.restrict(years) -> TrainingView` (subset of training years, frame sliced along — needed by the calibrator; still train-only by construction). `.digest` unchanged.
- `PredictionRequest.features: FeatureFrame | None = None` (frozen field with default; units still bare).
- `scoring.score/predictions_for/screen(..., sources=None)`: when `model.feature_specs` (optional attribute, default `()`) is non-empty, build train and eval frames via `build_frame`, run `audit_frame` on the eval frame, raise `FeatureAdmissionError` if not clean, **then** fit → predict → labels, in the existing order. `screen()` refactored to fit once and derive card and canary arrays from the same probs (arithmetic identical; fixture 0.1 guards).
- `Scorecard` gains trailing defaulted `feature_digest: str = ""`, `feature_columns: tuple[str,...] = ()` — not in `_REPRO_FIELDS`.
- `canary.run(..., declared_feature_digest=None, frame_digest=None)`: finding 5 "feature provenance", skipped when the model declares none (committed verdicts unchanged), tripped on mismatch.
- `Model` Protocol docstring documents optional `feature_specs`, `feature_digest`.

### 0.4 Card provenance for replay (inside `data_snapshot`) **[fix: no new ExperimentCard fields]**

`Dataset.provenance()` gains `"inputs": sorted manifest keys of the panel` and, when features were built, `"feature_version"`, `"feature_inputs"`, `"feature_columns"`, `"feature_audit"`. `run_experiment` adds `"model_kwargs": candidate.kwargs` to `data_snapshot`. Old cards have none of these; their hashes are untouched.

### 0.5 Boundary tests — `tests/test_boundaries.py` (AST, no imports executed)

(a) no module under `readiness/engine/` imports `readiness.harness.contract`, `readiness.harness.canary`, `readiness.contracts`; (b) no module under `readiness/harness/` imports `readiness.engine`, `readiness.agent`, `claude_agent_sdk`, `anthropic`, or the name `fetch`/`Session` from `readiness.connectors.base`; (c) every import under `readiness/**` resolves to `sys.stdlib_module_names` or `readiness.*`, except `claude_agent_sdk` inside `try:` in `readiness/agent/`; (d) `readiness/exposure/`, `readiness/cite.py`, `readiness/brief.py`, `readiness/plans/` never import `readiness.harness.labels`, `readiness.harness.scoring`, or `readiness.agent`; (e) `readiness/plans/` and `readiness/harness/` never import an LLM client.

### 0.6 `readiness verify --phase N` — `readiness/verify.py`

`phase0(contract, dataset) -> list[Check]` is the current `cmd_verify` body moved verbatim (reuses `harness.contract.Check`); `phase1..phase4` added per phase. `--phase 1..4` checks are **ledger-only** (no dataset build) unless `--replay`. `verify.py` may import harness, ledger, data, contracts, cite; never an LLM. Makefile: `PHASE ?= 0`, `verify: ... verify $(CFLAG) --phase $(PHASE)`.

### 0.7 Connector registry as data

`readiness/connectors/__init__.py`: `CONNECTORS: dict[str, ConnectorInfo(key_prefix, source, license, global_coverage: bool, network: bool, is_label_source: bool)]` — census, storm_events, nws_zones now; each new connector registers. `connector_for_key(manifest_key) -> ConnectorInfo`. `DATA-LICENSES.md`'s "in use" table is checked against it by `tests/test_licenses.py::test_every_registered_connector_is_in_the_licence_manifest`.

### 0.8 CI and housekeeping

- `tests/test_metrics.py::test_constant_forecast_is_perfectly_unsharp` → `assertAlmostEqual(places=12)`. `metrics.py` arithmetic is never touched.
- `harness_expected/README.md`: fingerprints are blessed and bit-for-bit verified on Python 3.12 (compensated `sum()`); 3.10/3.11 may differ in the last ulp. `requires-python` stays `>=3.10`.
- `.github/workflows/test.yml`: `make test` on 3.10 and 3.12; no network.
- `.github/workflows/real-data.yml`: `workflow_dispatch` inputs `contract`, `target ∈ {snapshot, phase1, fleet, brief}`; restores `snapshot-${{ hashFiles('snapshots/manifest.json') }}` (cache `path:` extended in both workflows to `snapshots/open_meteo`, `snapshots/census`, `snapshots/usa_structures`); runs the Make target; opens a PR with changed `snapshots/manifest.json`, `experiments/<c>/*`, `harness_expected/<c>*.json`, `issued/`. Nothing on `main` is written by a workflow.
- Sandbox: `cli.build_parser()` marks network subcommands with `network=True`; `site/assets/sandbox.py::run_cli` refuses them; `tests/test_site.py::test_network_subcommands_are_refused_by_the_sandbox`. `tools/build_site.py::_package_files` already packs `readiness/**/*.py`.
- `readiness/agent/orchestrator.system_prompt()` forbidden-edit list gains `readiness/connectors/`, `readiness/harness/features.py`, `readiness/engine/features.py` **[fix: the agent proposes specs from the vocabulary; it cannot author a source or a transform]**.

---

## 1. Phase 1 — The loop, on one hazard

**Exit (report §6):** "BSS > 0 vs. climatology on untouched test years (2021–2025) with reliability within ±5 pts per bin — published, with the ledger, in the open repo."

Contract: the existing `inland-flood-la` (its ledger grows append-only; the four committed cards are untouched). `tornado-ok` is run through the same queue as the likelier first pass (seasonal AUC 0.82 already).

### 1.1 Connectors (each: sha256 in `snapshots/manifest.json`, `LICENSE`, `CONNECTORS` entry)

- `readiness/connectors/gazetteer.py` — Census 2020 Gazetteer counties (public domain, `global_coverage=False`). `load(snapshot_dir, manifest, *, refresh) -> dict[fips, Centroid(lat, lon, land_m2, water_m2)]`; key `census/gazetteer_counties2020`. `source() -> FeatureSource(kind="static", derived_through=None, name="gazetteer")` with `water_share`, `lat`.
- `readiness/connectors/open_meteo.py` — ERA5 via Open-Meteo archive API (CC BY 4.0, `global_coverage=True`). `snapshot(centroids: Mapping[str,(lat,lon)], years, snapshot_dir, manifest, *, keep_raw, refresh, progress) -> Path`: one request per region (`daily=precipitation_sum,temperature_2m_mean`, sequential on the shared `Session`, sleep on 429, resumable per state); `parse_daily_to_monthly(response) -> (months, precip_mm, tmean_c, elevation_m)` pure function. **Extract, not raw, is pinned [fix]:** `snapshots/open_meteo/<state_fips>_era5_monthly.jsonl`, one line per region `{"id","elevation_m","month0","precip_mm":[...],"tmean_c":[...]}` (national ≈ 3,100 × 360 floats × 2 ≈ 25 MB; raw responses discarded unless `--keep-raw`, their sha256 in `notes` with the response's model-version string; `generationtime_ms` is never hashed). Key `open-meteo/era5/<state_fips>`. Schema drift → `ConnectorError`. `source()` → series source `era5` (`precip_mm`, `tmean_c`) and static source `elevation` (`derived_through=None`, on the timeless allow-list).
- `readiness/connectors/nri.py` — FEMA NRI county CSV (public domain, `global_coverage=False`). Key `fema/nri_counties_<version>`; `VINTAGE = NriVintage(version="1.20", derived_through=2023, citation=...)`; `source()` static with `derived_through=2023`. **Refused by `admit()` for every current contract** (validate starts 2016). Ships because the roadmap names it and the refusal is the firewall's real-data demonstration; the backtest report shows NRI as a benchmark row stamped INADMISSIBLE.
- `readiness/connectors/climada_layer.py` — reads a pinned JSONL `snapshots/climada/<hazard>_<scope>.jsonl` `{region, rp10, rp50, rp100, event_set_years:[a,b], seed}` produced by `tools/climada/run_event_set.py` (GPL-3.0, outside the package, optional `[climada]` extra). Static source, `derived_through = event_set_years[1]`; a set built from tracks/gauges through 2015 is admissible, one through 2020 is refused. This is the whole CLIMADA integration in Phase 1; the subprocess-model protocol is deferred (no hazard/snapshot_dir kwargs problem to solve).

`data.py`: `Dataset` gains `sources: dict[str, FeatureSource] = {}`, `feature_version: str = ""` (`Manifest.digest` over the sources' keys), `feature_inputs: list[str]`. `build(contract, ..., features: Sequence[str] = ())` names connectors to load (`era5`, `terrain`, `nri`, `climada`); `data_version` is unchanged by features.

### 1.2 Engine (`readiness/engine/`, stdlib, Pyodide-clean)

- `features.py` — specs only: `FEATURE_SETS = {"era5-antecedent": (precip trailing_sum 1/3/12, same_months_mean over prior 10 years, tmean trailing_mean 3; all lag_months=1), "terrain": (elevation_m, water_share, lat), "nri": (EAL_SCORE, RISK_SCORE), "climada-prior": (rp10, rp50, rp100)}`.
- `history.py` — label-derived features from `TrainingView.rows()` only **[fix: leave-one-year-out]**: `seasonal_logit(region, period)` = shrunk rate as `ClimatologySeasonal` computes it via a shared `baseline.seasonal_rates(rows, shrinkage)` (extracted; `ClimatologySeasonal` calls it; fixture 0.1 proves identical output), but for a training row the rate excludes that row's own year; holdout rows use all training years. Documented asymmetry, same regime `PersistenceLastYear` lives under.
- `linear.py` — `LogisticRegression(name="logistic", version="1.0.0", feature_sets=("era5-antecedent","terrain"), history=True, l2=1.0, iters=400, lr=0.1)`: standardise on train stats, nan → train mean + missing indicator, full-batch GD, fixed iterations, no RNG; sets `training_digest=view.digest`, `feature_digest=view.feature_digest`; `predict` reads `request.features.rows[unit]`.
- `boosting.py` — `GradientBoosting(name="gbm", rounds=150, depth=2, lr=0.1, bins=32, min_leaf=20)`: histogram splits, quantile cuts on train rows, no subsampling, ties by column order; deterministic.
- `calibrate.py` — `Calibrated(inner, method="isotonic"|"platt", holdout_years=3)`: fits `inner` on `view.restrict(train_years[:-3])`, fits PAV/Platt on its predictions for the last 3 training years, refits `inner` on the full view. Never sees validate. Registered as `logistic+iso`, `gbm+iso`.
- `registry.py` — `ModelSpec` gains `needs_features: bool`; registered: `logistic`, `logistic+iso`, `gbm`, `gbm+iso`.

Determinism: `math.exp/log` may differ by 1 ulp across libm; Phase 1 fingerprints for feature models compare at 12 s.f.; climatology fingerprints stay exact.

### 1.3 Orchestrator

`PHASE1_QUEUE` (after `BASELINE_QUEUE`): `logistic` (history only), `logistic` (+era5), `logistic` (+era5+terrain), `logistic+iso`, `gbm`, `gbm+iso` — each `Candidate` with `changed`/`hypothesis` written now; `kwargs` recorded on the card. `run_local(..., queue=..., promote=False)`: with `promote=True`, after the validate queue, the **first** candidate (queue order) whose card passed and whose canary was clear is run once on `contract.splits.test` through `run_experiment` with the `TouchBudget`; `LoopResult.promoted`. `readiness promote MODEL -c NAME [--kwargs JSON]` is the explicit path; it refuses without a prior validate PASS card for that model@version+kwargs and refuses if the ledger already holds any test card under the current contract digest. **[fix]** `cmd_score --split test` now refuses and prints the `promote` command — the touch and the card are one atomic step.

### 1.4 Backtest report

`readiness/backtest.py`: `render(contract, ledger, manifest) -> str`, `write(contract) -> experiments/<c>/backtest.html` (+ `.json`). Built from committed files only. Sections: contract `describe()` + digest; `data_version`, `feature_version` and every manifest key hashed; feature columns with their specs and audit findings; NRI benchmark row (INADMISSIBLE); full validate history including failures; the test card with `dashboard.reliability_svg`, Murphy terms, bin table, `card_hash`, **total test touches in the ledger**; deliberately-not-tried list; attribution block from `DATA-LICENSES.md` incl. the Open-Meteo CC BY line and the NWS/IPAWS line; `<meta name="ledger-head">`. `tools/build_site.py` copies it into `site/generated/`.

### 1.5 Data flow and no-leak enforcement

`data.build(contract, features=[...])`: census → storm events → `Panel` (unchanged); gazetteer/open_meteo/nri/climada → `FeatureSource`s (regions and pinned files only; no events, no panel; boundary test 0.5d). `scoring.score()`: `admit()` every source (static vintage < first validate year; no label-origin keys) → `build_frame(specs, sources, units, ppy)` with harness-computed cutoffs `period_start − lag` → `audit_frame` (truncation rebuild bit-identical) → `TrainingView(train_panel, split, frame.restrict(train_units))` → `fit()` → `PredictionRequest(units, split, frame.restrict(eval_units))` → `predict()` → **only then** labels. History features are computed inside `fit()` from training labels, LOYO. Canary finding 5 checks the declared feature digest against the frame the harness handed over. Operational lead: `lag_months ≥ 1` is enforced at spec construction, so the backtest uses exactly the information available before a period starts — the same rule `issue()` (Phase 2) enforces.

What the harness proves vs. trusts: temporal precedence and label-origin are checked mechanically from harness-computed cutoffs and manifest keys; `derived_through` on a static layer is a connector constant on a reviewed allow-list, pinned into the manifest record's `notes`. That is the honest extent of "the harness can check it".

### 1.6 CLI, Makefile, docs

CLI: `snapshot --features era5,terrain[,nri,climada]`; `features -c NAME [--features ...]` (sources, admission verdicts, columns, audit on validate units — NRI refusal visible on purpose); `score MODEL [--feature-set X]*`; `loop [--queue baseline|phase1] [--features ...] [--promote]`; `promote MODEL`; `backtest`; `verify --phase 1 [--replay]`. Makefile: `features`, `loop-promote`, `backtest`, `phase1: snapshot features loop-promote backtest verify-phase1`. Docs: `docs/features.md` (firewall, admission rule, NRI example, lag = lead), `docs/backtest.md`; `skills/verification-protocol.md` (features are audited before fit; never hand-build a column); `skills/climada-recipe.md` (pinned-layer seam). `DATA-LICENSES.md`: Open-Meteo, Gazetteer, NRI → "In use now".

### 1.7 Tests

`tests/test_features.py`: `test_period_start_for_month_quarter_year`, `test_series_before_is_strict`, `test_build_frame_takes_units_and_is_invariant_to_shuffled_holdout_labels`, `test_lag_zero_is_refused_at_spec_construction`, `test_truncation_audit_is_clean_for_every_registered_transform`, `test_truncation_audit_trips_on_an_injected_forward_looking_transform`, `test_static_source_derived_through_validate_start_is_refused`, `test_nri_v1_20_is_refused_under_default_splits`, `test_label_origin_source_is_refused`, `test_untimed_static_outside_allow_list_is_refused`, `test_missing_values_are_nan_never_zero`, `test_frame_digest_stable_across_unit_order`.
`tests/test_splits.py`: `test_training_view_refuses_frame_with_holdout_units`, `test_restrict_keeps_only_training_years_and_slices_frame`.
`tests/test_scoring.py`: `test_score_refuses_unclean_audit_before_fit`, `test_baselines_bit_identical_with_and_without_sources`, `test_screen_fits_once_and_matches_fingerprint`.
`tests/test_canary.py`: `test_feature_provenance_mismatch_trips`, `test_no_features_means_skipped_not_tripped`.
`tests/test_engine_models.py`: `test_logistic_deterministic`, `test_gbm_deterministic_and_bounded`, `test_seasonal_rates_helper_reproduces_climatology_seasonal_exactly`, `test_history_uses_leave_one_year_out_for_training_rows`, `test_feature_models_beat_pooled_on_planted_signal`, `test_calibrated_only_constructs_train_only_views`, `test_no_new_model_needs_a_panel`.
`tests/test_connectors.py`: `TestOpenMeteo::{test_daily_to_monthly, test_extract_pinned_not_raw, test_schema_drift_is_error}`, `TestGazetteer::test_centroids_and_water_share`, `TestNri::test_declares_derived_through`, `TestClimadaLayer::test_schema_and_event_set_years`.
`tests/test_orchestrator.py`: `test_promote_spends_exactly_one_touch_on_first_validate_pass`, `test_promote_refuses_without_validate_pass`, `test_promote_refuses_when_a_test_card_already_exists`, `test_feature_queue_skipped_without_sources`.
`tests/test_backtest.py`: `test_renders_from_committed_files_only`, `test_embeds_test_card_hash_touch_count_and_ledger_head`, `test_lists_failures_and_inadmissible_nri_row`.
`tests/test_verify.py::TestPhase1`: `test_passes_on_synthetic_validate_pass_then_one_test_pass`, `test_fails_without_test_card`, `test_fails_when_passing_test_card_is_not_the_first_test_card`, `test_fails_when_touch_file_and_ledger_disagree`, `test_fails_when_verdict_does_not_re_derive`, `test_fails_when_backtest_missing_or_stale`.
`tests/test_cli.py`: `test_score_on_test_refuses_and_points_to_promote`, `test_verify_phase_defaults_to_zero`.
Plus 0.1, 0.5, 0.8 tests.

### 1.8 Exit check

```
make phase1 CONTRACT=inland-flood-la
# = readiness snapshot -c inland-flood-la --features era5,terrain
#   && readiness loop -c inland-flood-la --queue phase1 --features era5,terrain --promote
#   && readiness backtest -c inland-flood-la
#   && readiness verify -c inland-flood-la --phase 1
```
`verify --phase 1` (ledger-only) exits 0 iff: chain + anchor intact; exactly one card with `split=="test"` under the current contract digest, and it is the **first** test card in the ledger **[fix: no test-shopping across versions]**; its verdict is **re-derived** by `harness.contract.evaluate()` from the stored scorecard (BSS > 0, every populated bin within ±0.05, AUC ≥ 0.70, digest match); canary not rejected; `data_snapshot.feature_audit.clean`; an earlier validate PASS card exists for the same model@version+kwargs; `test_touches.json` shows 1 touch for it; `experiments/<c>/backtest.html` embeds that `card_hash` and the ledger head. `--replay` (needs pinned data) rebuilds the model from `model_kwargs`, refits and rescores on test without spending the budget, compares at 12 s.f. Publication = the `real-data.yml` PR; `make site` serves it.

Offline-provable: everything structural (firewall, admission incl. NRI refusal, label-origin, truncation audit, canary 5, determinism, LOYO history, calibrator train-only, promote spends one touch, verify accepts/rejects synthetic ledgers, repro guard, boundaries, parsers on committed 3-county samples). Needs real data: whether any candidate clears BSS > 0 with ±5 pt reliability on Louisiana 2021–2025 (seasonal already fails AUC 0.60 there; honest failure is a published card; a new contract name, not a relaxed threshold, is the sanctioned next move). If a first test touch fails, the contract cannot exit Phase 1; the report says so.

Deferred: CLIMADA subprocess model and event-set generation (GPL tool outside the package; only the pinned layer ships); AIWP reforecasts; changes to the forecast unit; the claude backend (wired, unchanged, not required).

---

## 2. Phase 2 — Multi-hazard, national, with exposure

**Exit:** "≥4 hazards pass the contract nationally; exposure joins are spot-validated against county assessor counts in 10 sampled counties."

### 2.1 Contracts and fleet
Registered as data, `states: []`: `inland-flood-us`, `tornado-us`, `hail-us`, `severe-wind-us` (quarter, drop), `winter-storm-us`, `heat-us` (month, expand). Six so four can pass. `readiness/fleet.py`: `status(registry) -> list[ContractStatus(name, hazard, scope_key, period, n_cards, phase0_ok, phase1_ok, promoted_model, test_bss, issued_periods)]`; `run_fleet(contracts, *, queue, features, promote, progress)` — **sequential** (per-contract ledgers; `manifest.save()` is not thread-safe). CLI `fleet [--national] [--promote]`, `contracts --names`; Makefile `loop-all`, `backtest-all`. `PHASE2_QUEUE`: `logistic`, `logistic+iso`, then `gbm(rounds=60, bins=16)`, `gbm+iso`; national quarterly ≈ 380k units, monthly ≈ 1.1M: logistic minutes, capped GBM tens of minutes; wall-clock recorded on the card. The claude backend gets `run_claude_fleet` over `all_hazard_analysts()`; not required by the exit.

### 2.2 Exposure
`readiness/connectors/usa_structures.py` (public domain, `global_coverage=False`, `derived_through=<layer lastEditDate year>` so `admit()` refuses it as a feature): FEMA/ORNL USA Structures ArcGIS FeatureServer `outStatistics` grouped by `FIPS, OCC_CLS, PRIM_OCC`, one paged query per state, layer URL a constant with `--layer-url` override, resolved URL and layer version pinned in `notes`; `snapshots/usa_structures/<st>_counts.jsonl` `{fips, occ_cls, prim_occ, n}`; key `fema/usa_structures/<st>`. `readiness/exposure/occupancy.py`: `CLASSES` mapping (residential/education/medical/hospital/school) confirmed on first real pull. `readiness/exposure/table.py`: `CountyExposure(fips, total, by_class, unclassified_share, vintage, source_key)`, `ExposureTable.load(...)`, `.digest()`; **no sub-county field exists**. `readiness/exposure/spotcheck.py`: `exposure_expected/assessor_counts.csv` columns `fips, assessor_count, count_definition ∈ {structures, improved_parcels}, source_url, retrieved_on, notes`; `run(table, csv, ratio_bounds) -> list[SpotCheck(fips, ours, assessor, ratio, within)]`; `EXPOSURE_SPOTCHECK_RATIO = (0.67, 1.5)` in `config.py` with the parcel-vs-structure reason. **[fix]** `notes` is informational; a row outside bounds does not count toward the 10; ratios are printed. CLI `exposure snapshot|show|spot-check`.

### 2.3 Issuance — `readiness/issue.py`
`issue(contract, dataset, model, version, kwargs, period=(year, p)) -> Issued(contract, contract_digest, model, version, model_kwargs, validated_by: card_id, period, probabilities: dict[region, float], train_digest, feature_digest, feature_version, data_version, issued_at)`. Guards: the ledger holds a passing, canary-clear **test** card for model@version+kwargs under the current digest; the model is refitted through `TrainingView` on train years only and `training_digest == card.train_digest` **and `feature_digest == card.feature_digest`** **[fix]** (a revised ERA5 extract changes the fit and is refused, not silently reissued); the target period's frame is built with `build_frame` (cutoff = period start − lag) and `audit_frame` runs; refuses if any series source's last pinned month < cutoff ("period cannot be issued yet: data through YYYY-MM needed"). Writes `issued/<contract>/<YYYY>-P<p>.json`. No label exists for a future period; the function has no parameter through which one could arrive.

### 2.4 Citations — `readiness/cite.py` (reused unchanged by Phase 3)
`Source(kind ∈ {"ledger","issued","manifest","guidance","facility","scenario","computed"}, ref)`; `Claim(id, text, value, source, fmt="")`; `Sentence(text, claim_ids)`; `Document(title, kind, sentences, claims, generated_at, inputs)`; `Resolver` (card id exists and matches; issued file exists with that period; manifest key exists; guidance id in `plans/guidance.json`; facility field path exists; scenario id+constant exists in the scenario JSON). `validate(doc, resolver) -> list[Violation]`: every sentence cites ≥1 claim; every claim resolves; every numeric token (`\d[\d,\.]*%?`) equals a cited claim's formatted value; forbidden phrasing from §7 ("will occur", "is predicted to hit", "warning"). **[fix: identifiers]** tokens are matched after removing citation markers `[c:ID]`, and identifier strings listed in `plans/guidance.json` `aliases` (e.g. `CPG 101`, `42 CFR 482.15`, `NFPA 110`), model versions `\d+\.\d+\.\d+`, card ids `exp-\d{4}`, period labels `\d{4}-[QMP]\d+` and 5-digit FIPS are exempt as identifiers; scenario constants (`96`, `T+12`) are claims of kind `scenario`. `render_html`, `to_json`. `plans/guidance.json` introduced with the NWS/IPAWS entry.

### 2.5 The brief — `readiness/brief.py`
`build(fips, period, issued: list[Issued], exposure, ledgers, counties) -> Document`; one paragraph per contract with an issued file covering the county: "{p:.0%} chance of at least one damaging {hazard} event in {county} during {period} [c1]. This comes from {model}@{version}, which scored BSS {bss:+.2f} on the untouched {test years} with every populated reliability bin within 5 points [c2]. {county} holds {total:,} structures ({unclassified:.0%} unclassified) [c3], including {schools} schools and {hospitals} hospitals [c4]. Not a warning product; official alerts come from the NWS and IPAWS [g1]." Never "would touch" (occurrence ≠ footprint). County only. `validate()` runs before write; violations → exit 1. Output `briefs/<fips>/<period>.html|.json` + attribution. CLI `brief --county FIPS --period 2026-Q4 | --state XX`. `site/briefs.html` lists validated briefs.

### 2.6 Verify, tests, exit
`verify.phase2()` (no `-c`): (1) ≥4 registered contracts with `states == []` pass `phase1` checks; (2) `assessor_counts.csv` has ≥10 distinct counties from ≥3 states with pinned USA Structures counts within `EXPOSURE_SPOTCHECK_RATIO`, ratios printed; (3) for each passing contract an `issued/` file exists for the same period whose `validated_by` is that contract's first test card, and one brief built from them validates with zero violations and has no sub-county keys.

Tests: `test_fleet.py::{test_status_reads_phase_results, test_one_ledger_and_budget_per_contract}`; `test_connectors.py::TestUsaStructures::{test_paged_stats_hashed_together, test_vintage_recorded, test_refused_as_feature_by_admit}`; `test_exposure.py::{test_rollup_by_declared_classes, test_unknown_values_counted_in_total_and_reported, test_ratio_bounds, test_out_of_bounds_rows_do_not_count_toward_ten}`; `test_issue.py::{test_refuses_without_test_pass_card, test_refuses_train_digest_mismatch, test_refuses_feature_digest_mismatch, test_refuses_period_with_missing_months, test_issued_carries_provenance_and_every_region}`; `test_cite.py::{test_uncited_sentence, test_number_without_claim, test_identifier_aliases_exempt, test_scenario_constant_is_a_claim, test_unresolvable_card, test_guidance_id_must_exist, test_forbidden_phrasing}`; `test_brief.py::{test_cites_probability_backtest_and_exposure_per_hazard, test_not_a_warning_line, test_not_written_on_violation, test_never_names_sub_county, test_missing_exposure_stated_not_substituted}`; `test_verify.py::TestPhase2::{test_requires_four_national_phase1_passes, test_requires_ten_in_bounds_counties, test_brief_must_validate}`; `test_orchestrator.py::test_national_synthetic_panel_bounded_time` (gated by `READINESS_SLOW_TESTS=1`); boundary 0.5d.

```
make phase2   # loop-all --queue phase2 --features era5,terrain --promote; backtest-all;
              # exposure snapshot --all-states; exposure spot-check; issue per passing contract; brief; verify --phase 2
```
Offline: fleet isolation, issue guards, exposure arithmetic, citation rules, brief shape, verify logic on synthetic registries. Real data: which four hazards pass (zone-coded ones risk crosswalk drift); FeatureServer URL/paging/vocabulary; the ten assessor counts (collected by a person with URLs).

Deferred: Microsoft footprints (ODbL, no occupancy, no gain for the criterion); HURDAT2/CLIMADA wind fields; live AIGFS nowcast quarters (in-period information, forbidden by the firewall for backtests; would need its own contract); tract/address risk (forbidden by §7, not deferred); parallel fleet execution.

---

## 3. Phase 3 — The planning thought-partner

**Exit:** "Blinded review: practicing emergency managers rate agent gap-reports for ≥3 real facilities as useful-or-better, and every recommendation traces to a source or a computed risk number."

### 3.1 Package `readiness/plans/` (stdlib; imports `cite`, `harness.ledger`; no LLM, no labels — boundary 0.5d/e)
- `facility.py` — `Facility` from JSON: `slug, occupancy_type ∈ {hospital, nursing_home, shelter, school, other}, county_fips, census, staff_on_shift, power{generator, fuel_hours, switchgear_elevation_ft, transfer_switch_elevation_ft, load_test_interval_days}, water{on_site_storage_hours}, design_intensity{flood_elevation_ft, flood_elevation_source, design_wind_mph}, evacuation{trigger_written, trigger_text, authority, transport_lead_hours, priority_order_written, priority_decided_on}, transfer_agreements[{name, county_fips, signed, same_floodplain, same_grid_feeder}], co_located_operators[{name, occupants}], evidence{key: {text, source_doc, page}}`. Unknown keys refused; every populated field must name an evidence key. **No address or lat/lon field exists [fix: no NFHL pin, nothing address-level reaches the manifest].** Design intensity comes from the planner's elevation certificate / FIRM, cited as a facility document. `plans/facilities/` gitignored except `example-rural-hospital.json` (fictional).
- `scenarios.py` — `Inject(id, hour, kind, params)`, `Question(id, text, rule, requires)`, `Scenario(id, title, injects, questions, fail_closed_on, source_doc)`; `plans/scenarios/96h-isolation-acute-care.json` beside the markdown; `tests/test_scenarios.py::test_question_ids_and_texts_match_the_markdown` keeps the markdown the spec.
- `rules.py` — `RULES: dict[str, Callable[[Facility, RiskLayer, Scenario], Finding]]`: `switchgear_vs_intensity` (cannot_run if `flood_elevation_ft` or either elevation is null; failed if switchgear or transfer switch < design elevation, citing both numbers), `written_trigger` (answered iff written + authority + lead hours; failed if lead hours > hours before road closure at T+12), `priority_order_in_advance`, `partner_correlated_failure` (failed if a signed partner shares the county, `same_floodplain` or `same_grid_feeder`; both counties' issued probabilities cited as context, not as the criterion), `campus_seam`, `first_break` (min over fuel_hours, water storage + 6, … vs 96; a `computed` claim). `Finding(question_id, status ∈ {answered, unanswered, failed, cannot_run}, sentences)`. Fail-closed: missing design intensity → single `cannot_run` naming the guidance id for the elevation certificate / FIRM; no county substitute path exists (tested).
- `risk.py` — `RiskLayer.load(period, counties) -> RiskLayer(probabilities, evidence)` from `issued/` only plus the backing test cards; absence is an explicit claim.
- `gap_report.py` — `build(facility, results, risk) -> cite.Document`; `render_html`; `render_blinded(doc) -> (html, sha256)` (slug, names, partners → `FACILITY-<hash6>`); written only if `validate()` is clean, to `plans/reports/<slug>/<period>.{html,json,blind.html}` (gitignored except the example).
- `draft.py` — local backend fills templated section text from findings citing facility evidence and guidance ids. `readiness/agent/planner.py` (claude backend, optional SDK) may rewrite prose; every sentence must carry `[c:ID]` tokens in the report's claim set, `cite.validate` runs on the output, rejected sentences dropped and counted on the provenance line.
- `plans/guidance.json` — FEMA CPG 101 v3, CMS 42 CFR 482.15, ASPR TRACIE hospital evacuation toolkit, NFPA 110, NFPA 99, FEMA Elevation Certificate/FIRM; ids, titles, publisher, year, url, section, `aliases`.
- Reviews: `plans/reviews/<report_sha>.json {report_sha256, facility_hash, period, reviewer_role, organisation_type, years_in_role, rating ∈ {not useful, somewhat useful, useful, very useful}, blinded: true, comments, recorded_at}`; `readiness review record --report X.blind.html --rating useful --role "practising emergency manager" ...`; sha must match a blinded render.
- Case studies: `plans/case-studies/<slug>.json {event, hazard, sources[], facility_as_recorded, expected_findings{qid: status}}`; `readiness scenarios check` runs each; a fact without a source fails schema. Zero ship; `tests/fixtures_plans.py::synthetic_case_study()` tests the mechanism.

### 3.2 Verify, tests, exit
`verify.phase3(reports_dir=None)`: (1) ≥3 distinct `facility_hash` with a review `blinded: true`, `reviewer_role` containing "emergency manager", rating ∈ {useful, very useful}; (2) each review's `report_sha256` equals the sha of a blinded render found in `plans/reports/` or `--reports DIR` **[fix: real reports are never committed]** and that report validates under `cite` with zero violations; (3) every committed case study reproduces its expected findings; (4) no committed JSON under `plans/` contains `address`, `lat` or `lon` keys.

Tests: `test_scenarios.py::{test_questions_match_markdown, test_missing_intensity_fails_closed_naming_document, test_no_rule_substitutes_county_probability_for_intensity, test_switchgear_below_design_elevation_failed, test_same_county_partner_is_correlated_failure, test_first_break_hour_is_computed_claim, test_unanswered_is_a_finding}`; `test_risk_layer.py::test_reads_only_issued_and_states_absence`; `test_gap_report.py::{test_every_sentence_cites_resolvable_claim, test_not_written_on_violation, test_blinded_render_has_no_names, test_llm_sentences_without_claims_dropped_and_counted}`; `test_draft.py::test_local_sections_cite_guidance_ids`; `test_reviews.py::{test_sha_must_match_blinded_render, test_rating_vocabulary_closed}`; `test_case_studies.py::{test_synthetic_reproduces_expected, test_without_sources_rejected}`; `test_verify.py::TestPhase3::{test_requires_three_useful_blinded_reviews, test_stale_report_invalidates_review, test_accepts_reports_dir}`; `test_cli.py::{test_gap_report_refuses_unknown_fields, test_no_committed_plans_json_has_address_or_latlon}`; boundary 0.5e.

```
readiness scenarios check && readiness verify --phase 3 --reports <dir with the three blinded reports>
```
Offline: rules, fail-closed, citation validation over rules and fake LLM output, blinding, sha binding, case-study mechanism, verify logic. Not mechanisable: that facilities are real and reviewers are practising emergency managers — the review record is an attestation bound to a specific blinded report, and the design says so. Real-data dependency: the county probabilities a real report cites need Phase 2 issued files.

Deferred: address-level intensity from NFHL (needs coordinates pinned somewhere; the planner supplies BFE instead); further scenarios (generator failure at surge, simultaneous hazards) as markdown+JSON+rules pairs after the first is reviewed; after-action-report retrieval corpus (reintroduces uncited prose).

---

## 4. Phase 4 — Global scale-out

**Exit:** "The Phase 1 contract passes in two non-US pilots using only globally available data" (the partner-use clause is recorded as a dated note in `docs/global.md`, not checked).

### 4.1 Contract schema (`readiness/contracts.py`)
`SCHEMA_V1_DEFAULTS = {"ground_truth": {"source": "storm_events"}, "regions": {"source": "census"}}`; new fields `ground_truth: dict`, `regions: dict` elided at default (US digests unchanged; repro guard pins them). Non-US: `ground_truth ∈ {{"source":"national_records","sha256":…,"record_start_year":…}, {"source":"emdat","sha256":…,"admin_level":"ADM1","record_start_year":2000}}`, `regions = {"source":"geoboundaries","admin_level":"ADM1"|"ADM2","release":"gbOpen 6.0.0"}`; `zone_policy` must be `drop`; `record_start_year` moves to `config.GROUND_TRUTH_SOURCES[source]` (storm_events 1996). The ground-truth file's sha256 is a criterion. `config.HAZARD_CATEGORIES` maps hazards to EM-DAT type strings / exact `national_records` hazard values. `is_damaging` is unchanged and applied to record fields (USD threshold, casualties) **[fix: no "damaging by inclusion" convention]**. `readiness register NAME --country ZZ --ground-truth national_records --records PATH --regions geoboundaries --admin-level ADM2 --period year`.

### 4.2 Connectors (all `global_coverage=True`)
- `geoboundaries.py` — gbOpen ADM1/ADM2 GeoJSON (CC BY 4.0); `Region(id=shapeID, name, centroid_lat, centroid_lon)` (vertex-mean centroid); key `geoboundaries/<cc>/<level>`.
- `national_records.py` — partner CSV `event_id, start_date, region_id, hazard, deaths, injured, damage_usd, source` + `# record_start_year:` header; `is_label_source=True`; pinned under `records/<cc>/<basename>`; refuses to build if the contract's sha256 differs; bytes never committed.
- `emdat.py` — reads a local xlsx export via `zipfile` + `xml.etree`; ADM1 via the `Admin Units` column through a committed crosswalk `snapshots/records/<cc>_emdat_regions.csv`; unmapped units counted; `is_label_source=True`; only the hash is committed; the site packer skips `emdat/*` and `records/*` (tested).
- Open-Meteo and the firewall are already global; `data.build` passes geoBoundaries centroids to `open_meteo.snapshot`.

### 4.3 Labels and data plane
`labels.py`: `RecordEvent(event_id, year, month, hazard, region_ids, injuries, deaths, damage_property_usd)`; `_regions_hit()` returns `event.region_ids` for a `RecordEvent`; `build_panel`/`diagnose` signatures unchanged; US panel digests bit-identical (fixture 0.1 + `harness_expected`). `data.build` dispatches on `contract.regions.source` / `contract.ground_truth.source`; `Dataset.regions: tuple[Region,...]` with `census.County` satisfying `id`/`name`. `provenance()["inputs"]` lists the region file, the records hash and the ERA5 extracts.

### 4.4 Verify, tests, exit
`verify.phase4()`: (1) ≥2 registered contracts with `country != "US"` pass `phase1` checks; (2) every key in each pilot test card's `data_snapshot.inputs + feature_inputs` resolves via `connector_for_key` to `global_coverage=True` (census, storm_events, nws_zones, nri, usa_structures, gazetteer are all `False`) **[fix: computable — key lists are on the card since 0.4]**; (3) `ground_truth.sha256` matches the pinned records record; (4) the three US contracts' digests unchanged.

Tests: `test_contracts.py::{test_non_us_requires_explicit_ground_truth_and_regions, test_defaults_elided_from_digest, test_record_start_year_from_source, test_zone_policy_expand_refused_outside_us}`; `test_connectors.py::{TestGeoBoundaries::test_regions_and_centroids, TestNationalRecords::{test_schema_and_sha_pin, test_hash_mismatch_refuses}, TestEmdat::{test_xlsx_stdlib_only, test_unmapped_units_counted}}`; `test_labels.py::{test_record_events_build_dense_panel, test_us_panel_digest_unchanged}`; `test_data.py::test_build_dispatches_on_sources`; `test_global.py::test_full_loop_promote_and_verify_phase1_on_synthetic_non_us_contract` (fixtures gain `make_records_csv()`, `make_geojson()` under ZZ/ZY); `test_verify.py::TestPhase4::{test_requires_two_non_us_phase1_passes, test_us_only_connector_in_inputs_fails, test_ground_truth_hash_must_match}`; `test_site.py::test_sandbox_never_packs_partner_records`.

```
make phase1 CONTRACT=floods-zz && make phase1 CONTRACT=cyclone-zy && readiness verify --phase 4
```
Offline: digest elision, parsers, generalised labels with US digests unchanged, the whole loop on two synthetic pilots, the global-only provenance rule. Real data: partner records / EM-DAT export (registration), geoBoundaries release, ERA5 pulls, and whether the contract passes — EM-DAT sparsity likely forces ADM1 × year, decided with the partner before registering (the unit is in the digest).

Deferred: Open Buildings / Overture exposure (point-in-polygon over S2 CSVs — the first thing worth a real dependency, so a separate non-sandboxed tool); Flood Hub as a feature (forecast API, not a pinnable history; GloFAS via Open-Meteo Flood API is the pinnable substitute if a flood signal is needed and is a one-connector addition under the same firewall); global brief and gap report (wait for exposure); DesInventar.

---

## Ordered work plan

1. §0.1 repro guard incl. blessing `synthetic_fingerprints.json` — before anything else. (0.5 d)
2. §0.8 test fix, CI `test.yml`, `harness_expected/README.md` note. (0.5 d)
3. §0.2 digest elision + §0.7 registry + §0.5 boundary tests + §0.6 `verify.py` extraction (phase0 verbatim). (1 d)
4. §0.3 firewall + harness plumbing + canary 5 + `screen()` single-fit; §0.4 provenance in `data_snapshot`. Fixture from step 1 must stay green. (2 d)
5. Phase 1 connectors (gazetteer, open_meteo, nri, climada_layer) with committed 3-region samples. (2 d)
6. Engine: `seasonal_rates` extraction (fixture green), history LOYO, logistic, gbm, calibrate, registry. (2 d)
7. Orchestrator queue + promote; `score --split test` refusal; backtest renderer; `verify.phase1`; docs; sandbox refusal list; `real-data.yml`. (2 d) → **Phase 1 shippable offline; one `real-data.yml` run for the exit.**
8. Phase 2: contracts, fleet, usa_structures, exposure table + spot-check, `cite.py`, `issue.py`, brief, `verify.phase2`, `briefs.html`. (4 d)
9. Phase 3: facility, scenarios JSON + markdown-match test, rules, risk layer, gap report + blinding, reviews, case-study mechanism, `verify.phase3`, guidance registry, planner drafter. (4 d)
10. Phase 4: contract fields, labels `RecordEvent`, geoboundaries/national_records/emdat, data dispatch, synthetic pilot fixtures, `verify.phase4`, `docs/global.md`. (3 d)

Approximate new code: ~2,900 (0+1), ~2,000 (2), ~2,000 (3), ~1,400 (4).

## Risk list

- Phase 1 may not exit on `inland-flood-la` (seasonal AUC 0.60 vs 0.70; Storm Events occurrence reflects reporting practice). Mitigation: `tornado-ok` and the national convective contracts are likelier passes; a failed first test touch is published and the next move is a new contract, never a relaxed threshold or a second touch.
- NRI is refused as a feature under every current contract; the roadmap's wording expects it. The backtest report must show the refusal as a finding, or it reads as a bug.
- `derived_through` on static layers is a reviewed connector constant, not something the harness derives; the allow-list and manifest `notes` make it visible, not proven. Label-origin and temporal precedence are proven.
- Any drift in `ClimatologySeasonal` arithmetic during the `seasonal_rates` extraction moves blessed fingerprints; the synthetic fixture must be blessed before the refactor.
- Fingerprints are exact only on 3.12 (compensated `sum`); CI verifies bit-for-bit on 3.12 and runs tests on 3.10.
- Open-Meteo free tier: national pulls need resumable per-state runs across days; ERA5T revisions change the extract hash — `issue()` then refuses (feature-digest mismatch) until the model is re-validated, which is correct and must be documented.
- USA Structures FeatureServer semantics and `OCC_CLS`/`PRIM_OCC` vocabulary are unconfirmed offline; the mapping table is confirmed on the first real pull. Parcel counts ≠ structure counts; the (0.67, 1.5) bounds are a declared judgement, printed per row.
- Pure-Python GBM at national scale is tens of minutes per fit; the Phase 2 queue puts logistic first and caps GBM; the sandbox exposes logistic only.
- `cite.validate` is a smoke alarm for words: it proves every number and recommendation cites something, not that the citation supports the sentence; human review remains the Phase 3 criterion, and the attestation is the weakest link.
- Privacy: facility files and real gap reports never enter git; the no-address-field schema and the committed-plans key test are the guards. Nothing publishes sub-county risk.
- EM-DAT/national records at ADM2 × quarter may be too sparse for a populated reliability bin; choose the unit with the partner before registering.
- CLIMADA stays a pinned-layer seam unless someone runs the GPL tool; say so rather than imply integration.