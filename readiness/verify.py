"""Phase 0 and Phase 1 verification as a library.

Report §6, Phase 0:

    Exit when the agent reproduces the climatology baseline's scores
    bit-for-bit from a clean clone, and the harness rejects a deliberately
    leaked model (a canary test).

`readiness verify` prints the outcome; the browser sandbox recomputes the
fingerprints in Pyodide; CI runs the same check against the pinned data. Three
callers, one definition: the fingerprint fields, the models that must
reproduce, and the checks themselves live here so no caller can drift from the
others through a private import.

`REPRO_FIELDS` and `repro_fingerprint` are load-bearing: the blessed files in
`harness_expected/` were written by them, and a change to either would make
every committed fingerprint unverifiable. They do not move.

Phase 1 (plan §2) is checked from the ledger alone: the verdict on the test
card is re-derived from its stored scorecard rather than read, the passing
test card must be the first test card ever written (no shopping across
contract versions), a validate pass with the same model, version and
arguments must precede it, the touch file must agree, and the backtest report
must embed the card. `replay` rebuilds and rescores the promoted model from
the card's own arguments without spending a touch, for anyone with the data.
"""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
from dataclasses import dataclass
from typing import Mapping

from readiness import data as data_mod
from readiness.contracts import Contract
from readiness.engine import build_model
from readiness.harness import contract as contract_mod
from readiness.harness import scoring
from readiness.harness.contract import Check
from readiness.harness.ledger import ExperimentCard, Ledger
from readiness.harness.splits import TouchBudget

#: Scorecard fields that must reproduce exactly. Reliability bins are included
#: via a hash so a bin-level difference cannot hide behind matching aggregates.
REPRO_FIELDS: tuple[str, ...] = (
    "n_units",
    "n_positive",
    "base_rate",
    "brier_score",
    "brier_score_reference",
    "brier_skill_score",
    "auc",
    "sharpness",
    "reliability",
    "resolution",
    "uncertainty",
    "panel_digest",
    "train_digest",
    "contract_digest",
)

#: The baselines whose scores must reproduce bit-for-bit from a clean clone.
REPRO_MODELS: tuple[str, ...] = ("climatology-pooled", "climatology-seasonal")

#: The model the harness must reject for the second criterion to hold.
CANARY_MODEL = "leaky-oracle"


def repro_fingerprint(card: scoring.Scorecard) -> dict:
    """The reproducibility fingerprint of one scorecard.

    Byte-identical to what wrote `harness_expected/*.json`: the repro fields
    as they are, plus a short hash of the canonical reliability bins.
    """
    bins = json.dumps(card.reliability_bins, sort_keys=True, separators=(",", ":"))
    out = {f: getattr(card, f) for f in REPRO_FIELDS}
    out["reliability_bins_sha256"] = hashlib.sha256(bins.encode()).hexdigest()[:16]
    return out


def fingerprints(dataset: data_mod.Dataset) -> dict:
    """Every fingerprint a blessed file records, recomputed from this dataset.

    The layout is the blessed file's: one entry per repro model on the
    validate split, plus the data version and the contract digest so a
    comparison also notices when the inputs or the criteria moved.
    """
    c = dataset.contract
    split = c.splits.validate
    observed: dict = {}
    for name in REPRO_MODELS:
        card = scoring.score(build_model(name), dataset.panel, c, split)
        observed[name] = repro_fingerprint(card)
    observed["_data_version"] = dataset.data_version
    observed["_contract"] = c.digest()
    return observed


def diff_keys(expected: dict, observed: dict) -> list[str]:
    """The top-level fingerprint groups that differ, in a stable order."""
    return [
        key
        for key in sorted(set(expected) | set(observed))
        if expected.get(key) != observed.get(key)
    ]


@dataclass(frozen=True)
class Phase0Result:
    """The Phase 0 criteria for one contract, each as a check with its evidence.

    `checks` is ordered as the CLI prints it: the contract, reproducibility,
    the canary, the ledger. `observed` is the fingerprint set the
    reproducibility check compared (or blessed), so a caller that wants to
    show the numbers need not score again.
    """

    contract: str
    checks: tuple[Check, ...]
    observed: dict
    expected_path: pathlib.Path
    blessed: bool = False

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def failures(self) -> list[str]:
        """One line per failed check: the first line of its detail."""
        return [c.detail.splitlines()[0] for c in self.checks if not c.passed]


def phase0(
    contract: Contract,
    dataset: data_mod.Dataset,
    *,
    ledger_path: pathlib.Path,
    expected_path: pathlib.Path,
    bless: bool = False,
) -> Phase0Result:
    """Evaluate the Phase 0 exit criteria for one contract against one dataset.

    With `bless`, the observed fingerprints are written to `expected_path`
    instead of compared, which is how a contract gets its first blessed file.
    """
    observed = fingerprints(dataset)
    checks = (
        _contract_check(contract),
        _write_blessed(observed, expected_path)
        if bless
        else _reproducibility_check(observed, contract, expected_path),
        _canary_check(dataset),
        _ledger_check(ledger_path),
    )
    return Phase0Result(contract.name, checks, observed, expected_path, bless)


def _contract_check(contract: Contract) -> Check:
    # A Contract validates itself on construction, so reaching this point is
    # the evidence; the row records what was checked and under which digest.
    return Check(
        "contract",
        True,
        f"contract {contract.name} validates (sha256:{contract.digest()}); "
        "splits are disjoint",
    )


def _write_blessed(observed: dict, expected_path: pathlib.Path) -> Check:
    expected_path.parent.mkdir(parents=True, exist_ok=True)
    expected_path.write_text(json.dumps(observed, indent=2, sort_keys=True) + "\n")
    return Check(
        "reproducibility",
        True,
        f"blessed baseline fingerprints -> {data_mod.relative(expected_path)}",
    )


def _reproducibility_check(
    observed: dict, contract: Contract, expected_path: pathlib.Path
) -> Check:
    if not expected_path.exists():
        return Check(
            "reproducibility",
            False,
            "reproducibility: nothing to compare against\n"
            f"no blessed baseline at {data_mod.relative(expected_path)}; "
            f"run `readiness verify -c {contract.name} --bless` once, then commit it",
        )
    expected = json.loads(expected_path.read_text())
    diffs = diff_keys(expected, observed)
    if not diffs:
        return Check(
            "reproducibility", True, "climatology baselines reproduce bit-for-bit"
        )
    lines = [f"reproducibility: {len(diffs)} field group(s) differ"]
    for key in diffs:
        lines.append(key)
        lines.append(f"  expected {json.dumps(expected.get(key), sort_keys=True)}")
        lines.append(f"  observed {json.dumps(observed.get(key), sort_keys=True)}")
    return Check("reproducibility", False, "\n".join(lines))


def _canary_check(dataset: data_mod.Dataset) -> Check:
    c = dataset.contract
    oracle = build_model(CANARY_MODEL, canary_panel=dataset.panel)
    _card, report = scoring.screen(oracle, dataset.panel, c, c.splits.validate)
    if not report.rejected:
        return Check("leakage canary", False, "leakage canary accepted a leaked model")
    tripped = ", ".join(f.check for f in report.findings if f.tripped)
    return Check(
        "leakage canary",
        True,
        f"leakage canary rejected {CANARY_MODEL} (tripped: {tripped})",
    )


def _ledger_check(ledger_path: pathlib.Path) -> Check:
    status = Ledger(ledger_path).verify()
    return Check("ledger", status.valid, status.format())


# ---------------------------------------------------------------------------
# Phase 1
# ---------------------------------------------------------------------------

#: Significant figures a replay *prints* at. The comparison is `agrees()`,
#: not this: see `REPLAY_REL_TOL`.
REPLAY_SIGFIGS = 12


@dataclass(frozen=True)
class Phase1Result:
    """The Phase 1 exit criteria for one contract, each as a check with evidence.

    Ledger-only: nothing here builds a dataset or fits a model. `card` is the
    test card the checks were made against, when there was exactly one.
    """

    contract: str
    checks: tuple[Check, ...]
    card: ExperimentCard | None = None

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def failures(self) -> list[str]:
        """One line per failed check: the first line of its detail."""
        return [c.detail.splitlines()[0] for c in self.checks if not c.passed]


def phase1(
    contract: Contract,
    *,
    ledger_path: pathlib.Path,
    touch_path: pathlib.Path,
    backtest_path: pathlib.Path,
) -> Phase1Result:
    """Evaluate the Phase 1 exit criteria from the committed record alone."""
    ledger = Ledger(ledger_path)
    cards = list(ledger.read())
    card = _the_test_card(cards, contract)
    checks = [_ledger_check(ledger_path), _test_card_check(cards, contract, card)]
    if card is not None:
        checks += [
            _verdict_check(card, contract),
            _canary_check_on_card(card),
            _features_check(card),
            _validated_first_check(cards, card),
            _touch_budget_check(card, touch_path, contract),
            _published_check(card, ledger.head(), backtest_path),
        ]
    return Phase1Result(contract.name, tuple(checks), card)


def _the_test_card(
    cards: list[ExperimentCard], contract: Contract
) -> ExperimentCard | None:
    """The test card under the current digest, when there is exactly one."""
    digest = contract.digest()
    mine = [c for c in cards if c.split == "test" and c.contract_digest == digest]
    return mine[0] if len(mine) == 1 else None


def _test_card_check(
    cards: list[ExperimentCard], contract: Contract, card: ExperimentCard | None
) -> Check:
    digest = contract.digest()
    tests = [c for c in cards if c.split == "test"]
    mine = [c for c in tests if c.contract_digest == digest]
    if not mine:
        return Check(
            "test card", False,
            f"test card: none under contract sha256:{digest}\n"
            f"run `readiness promote MODEL -c {contract.name} --spend-test-touch` "
            "after a validate pass",
        )
    if card is None:
        ids = ", ".join(c.experiment_id for c in mine)
        return Check(
            "test card", False,
            f"test card: {len(mine)} test cards under contract sha256:{digest} ({ids}); "
            "exactly one is allowed",
        )
    if tests[0] is not card:
        return Check(
            "test card", False,
            f"test card: {card.experiment_id} is not the first test card in the ledger "
            f"({tests[0].experiment_id}, {tests[0].model}@{tests[0].version} under "
            f"sha256:{tests[0].contract_digest} came first); the test split was "
            "touched before this contract version and cannot be re-shopped",
        )
    return Check(
        "test card", True,
        f"test card {card.experiment_id}: {card.model}@{card.version} on test, the "
        f"first and only test card under contract sha256:{digest}",
    )


def stored_scorecard(card: ExperimentCard) -> scoring.Scorecard:
    """The card's scorecard as the object the judge takes, tuples restored."""
    raw = dict(card.scorecard)
    raw["reliability_bins"] = tuple(raw.get("reliability_bins", ()))
    raw["feature_columns"] = tuple(raw.get("feature_columns", ()))
    return scoring.Scorecard(**raw)


def _verdict_check(card: ExperimentCard, contract: Contract) -> Check:
    """Re-derive the verdict from the stored numbers; never trust the stored word."""
    verdict = contract_mod.evaluate(stored_scorecard(card), contract)
    stored = (card.verdict or {}).get("passed")
    if verdict.passed and stored:
        return Check(
            "verdict", True,
            f"verdict re-derived from the stored scorecard: PASS "
            f"(BSS {card.scorecard['brier_skill_score']:+.4f}, "
            f"AUC {card.scorecard['auc']:.4f})",
        )
    failed = [c.name for c in verdict.checks if not c.passed]
    lines = [
        "verdict: the stored scorecard does not pass the contract"
        + (" although the card says it did" if stored else "")
        + f" (failed: {', '.join(failed) or 'none'})"
    ]
    lines += [c.format().strip() for c in verdict.checks]
    return Check("verdict", False, "\n".join(lines))


def _canary_check_on_card(card: ExperimentCard) -> Check:
    rejected = bool((card.canary or {}).get("rejected"))
    findings = (card.canary or {}).get("findings", [])
    tripped = ", ".join(f["check"] for f in findings if f["tripped"])
    if rejected:
        return Check(
            "canary", False, f"canary: the test card was rejected (tripped: {tripped})"
        )
    return Check("canary", True, "canary clear on the test card")


def _features_check(card: ExperimentCard) -> Check:
    sc = card.scorecard or {}
    columns = sc.get("feature_columns") or []
    if not columns:
        return Check("features", True, "features: none on this card; audit check skipped")
    audit = sc.get("feature_audit") or {}
    if audit.get("clean"):
        return Check(
            "features", True,
            f"feature audit clean over {len(columns)} column(s): {', '.join(columns)}",
        )
    findings = [f["detail"] for f in audit.get("findings", []) if not f.get("passed")]
    return Check(
        "features", False,
        "features: the card's audit is not clean\n" + "\n".join(findings or ["no audit"]),
    )


def _validated_first_check(cards: list[ExperimentCard], card: ExperimentCard) -> Check:
    kwargs = card.data_snapshot.get("model_kwargs")
    earlier = cards[: cards.index(card)]
    for prior in earlier:
        if (
            prior.split == "validate"
            and prior.model == card.model
            and prior.version == card.version
            and prior.contract_digest == card.contract_digest
            and prior.data_snapshot.get("model_kwargs") == kwargs
            and prior.status == "PASS"
        ):
            return Check(
                "validated first", True,
                f"validate card {prior.experiment_id} passed with the same model, "
                "version and arguments before the test touch",
            )
    return Check(
        "validated first", False,
        f"validated first: no earlier validate card for {card.model}@{card.version} "
        f"with arguments {json.dumps(kwargs, sort_keys=True)} passed under this contract",
    )


def _touch_budget_check(
    card: ExperimentCard, touch_path: pathlib.Path, contract: Contract
) -> Check:
    if not touch_path.exists():
        return Check(
            "touch budget", False,
            f"touch budget: {data_mod.relative(touch_path)} is missing, yet the ledger "
            "holds a test card",
        )
    counts = TouchBudget(touch_path, contract.test_touch_budget).as_dict()
    key = f"{card.model}@{card.version}"
    # The file must be exactly {the card's model: 1}. A second touch is the
    # obvious failure; a stray key is the quieter one — it records a test
    # score for a model with no test card behind it, which is either a card
    # that was removed or a touch spent outside `promote`.
    used = counts.get(key, 0)
    strays = sorted(k for k in counts if k != key)
    problems = []
    if used != 1:
        problems.append(f"{used} touch(es) for {key}, not 1")
    if strays:
        problems.append(
            f"touches for {strays} with no test card: "
            + ", ".join(f"{k}={counts[k]}" for k in strays)
        )
    if problems:
        return Check(
            "touch budget", False,
            f"touch budget: {data_mod.relative(touch_path)} records "
            + "; ".join(problems)
            + f"; the ledger's one test card needs exactly {{\"{key}\": 1}}",
        )
    return Check("touch budget", True, f"touch budget: exactly 1 test touch for {key}")


def _published_check(
    card: ExperimentCard, head: str, backtest_path: pathlib.Path
) -> Check:
    if not backtest_path.exists():
        return Check(
            "published", False,
            f"published: no backtest report at {data_mod.relative(backtest_path)}; "
            "run `readiness backtest`",
        )
    text = backtest_path.read_text(encoding="utf-8")
    has_card = card.card_hash in text
    has_head = f'<meta name="ledger-head" content="{head}">' in text
    if has_card and has_head:
        return Check(
            "published", True,
            f"backtest report embeds the test card ({card.card_hash[:12]}...) and the "
            f"ledger head ({head[:12]}...)",
        )
    parts = (("the test card's hash", has_card), ("the ledger head", has_head))
    missing = [what for what, ok in parts if not ok]
    return Check(
        "published", False,
        f"published: {data_mod.relative(backtest_path)} is stale; it does not embed "
        f"{' or '.join(missing)}; re-run `readiness backtest`",
    )


def sig(value: object, figures: int = REPLAY_SIGFIGS) -> str:
    """A value at `figures` significant figures, for display only.

    Never for comparison: two numbers that differ in the last bit can round to
    the same string and two that agree can straddle a rounding boundary.
    `agrees()` is the comparison.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return f"{value:.{figures}g}"


#: How close a replayed number must be to the card's. Relative, so every
#: quantity is held to the same twelve figures whatever its magnitude;
#: absolute as well, so a number that is legitimately zero is not measured
#: against a relative tolerance nothing can meet. The remaining slack is
#: `harness.metrics`, whose builtin `sum` over floats is compensated on
#: CPython 3.12 and a left fold on 3.10: the scores themselves can differ in
#: the last bit between two interpreters that are both running the same code.
REPLAY_REL_TOL = 1e-12
REPLAY_ABS_TOL = 1e-12

#: Reliability-bin fields that must be identical, and those compared at the
#: tolerance. A bin's edges, its count and whether it is populated are
#: integers and exact fractions; only the two averages are floating-point
#: reductions over forecasts.
BIN_EXACT_FIELDS: tuple[str, ...] = ("lower", "upper", "count", "populated")
BIN_CLOSE_FIELDS: tuple[str, ...] = ("mean_forecast", "observed_frequency")

#: The only replay fields whose values may be printed. A digest and a count
#: name the data; every other field is a score on the one-shot test split, and
#: a command that printed the refit's would hand a second reading of the
#: holdout to whoever ran it. Whether it agrees is the finding; what it says
#: is on the card.
REPLAY_SHOWN_FIELDS: frozenset[str] = frozenset(
    {"n_units", "n_positive", "panel_digest", "train_digest", "contract_digest",
     "feature_digest"}
)


def agrees(expected: object, observed: object) -> bool:
    """Card value against refit value: numbers at the tolerance, anything else equal."""
    if isinstance(expected, bool) or isinstance(observed, bool):
        return expected == observed
    if isinstance(expected, (int, float)) and isinstance(observed, (int, float)):
        return math.isclose(
            expected, observed, rel_tol=REPLAY_REL_TOL, abs_tol=REPLAY_ABS_TOL
        )
    return expected == observed


def _same_bin(expected: Mapping | None, observed: Mapping | None) -> bool:
    """One reliability bin, field by field: edges and counts exact, means close."""
    if expected is None or observed is None:
        return False
    if any(expected.get(f) != observed.get(f) for f in BIN_EXACT_FIELDS):
        return False
    return all(agrees(expected.get(f), observed.get(f)) for f in BIN_CLOSE_FIELDS)


def replay(
    contract: Contract, dataset: data_mod.Dataset, card: ExperimentCard
) -> list[tuple[str, object, object, bool]]:
    """Refit the card's model from its own arguments and rescore it on test.

    No touch is spent: this is `scoring.score`, not the orchestrator, and the
    card it is compared against already exists. Returns one row per compared
    field — (field, card value, refit value, agree) — over the reproducibility
    fields, the feature digest (exact: the feature channel reduces with
    `math.fsum` and is the same on every interpreter) and then one row per
    reliability bin, compared bin by bin rather than through a hash of the
    lot, so a difference names the bin it is in instead of reporting that
    something, somewhere, moved.
    """
    kwargs = card.data_snapshot.get("model_kwargs", {})
    model = build_model(card.model, canary_panel=dataset.panel, **kwargs)
    split = contract.splits.get(card.split)
    refit = scoring.score(model, dataset.panel, contract, split, sources=dataset.sources)
    stored = stored_scorecard(card)

    rows: list[tuple[str, object, object, bool]] = []
    for field in REPRO_FIELDS:
        want, got = getattr(stored, field), getattr(refit, field)
        rows.append((field, want, got, agrees(want, got)))
    rows.append(
        ("feature_digest", stored.feature_digest, refit.feature_digest,
         stored.feature_digest == refit.feature_digest)
    )
    want_bins, got_bins = stored.reliability_bins, refit.reliability_bins
    for i in range(max(len(want_bins), len(got_bins))):
        want = want_bins[i] if i < len(want_bins) else None
        got = got_bins[i] if i < len(got_bins) else None
        rows.append((f"reliability_bins[{i}]", want, got, _same_bin(want, got)))
    return rows


# ---------------------------------------------------------------------------
# Phase 2
# ---------------------------------------------------------------------------

#: Plan §3's exit: "at least four hazards pass the contract nationally".
MIN_NATIONAL_PASSES = 4

#: The person-collected half of the exposure spot-check.
COUNTS_PATH = data_mod.REPO_ROOT / "exposure_expected" / "assessor_counts.csv"


@dataclass(frozen=True)
class Phase2Result:
    """The Phase 2 exit criteria, each as a check with its evidence.

    Fleet-wide rather than per contract, so it takes no `-c`: the criterion is
    about the whole registry ("at least four hazards pass the contract
    nationally; exposure joins spot-validated in ten sampled counties"), and a
    per-contract answer could not state it. Like Phase 1 it reads committed
    files only — ledgers, the counts file, the pinned extracts, the issued
    files and the briefs — and builds nothing.
    """

    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def failures(self) -> list[str]:
        """One line per failed check: the first line of its detail."""
        return [c.detail.splitlines()[0] for c in self.checks if not c.passed]


def phase2(
    *,
    registry: dict[str, Contract] | None = None,
    experiments_dir: pathlib.Path | None = None,
    counts_path: pathlib.Path | None = None,
    issued_dir: pathlib.Path | None = None,
    briefs_dir: pathlib.Path | None = None,
    snapshot_dir: pathlib.Path | None = None,
) -> Phase2Result:
    """Evaluate the Phase 2 exit criteria across the registry."""
    from readiness import contracts as contracts_mod

    known = contracts_mod.registered() if registry is None else dict(registry)
    snapshot_dir = snapshot_dir or data_mod.SNAPSHOT_DIR
    counts_path = counts_path or COUNTS_PATH

    national_check, passing = _national_check(known, experiments_dir)
    exposure_check, counties = _spot_check(counts_path, snapshot_dir)
    issued_check, label, issued = _issued_check(
        known, passing, issued_dir, experiments_dir
    )
    brief_check = _brief_check(
        known, label, issued, counties,
        briefs_dir=briefs_dir, issued_dir=issued_dir, snapshot_dir=snapshot_dir,
        experiments_dir=experiments_dir,
    )
    return Phase2Result((national_check, exposure_check, issued_check, brief_check))


def _national_check(
    known: dict[str, Contract], experiments_dir: pathlib.Path | None
) -> tuple[Check, list[str]]:
    """Which national contracts pass the Phase 1 checks, and whether four do."""
    national = {name: c for name, c in known.items() if not c.states}
    passing: list[str] = []
    notes: list[str] = []
    for name in sorted(national):
        contract = national[name]
        where = data_mod.paths(contract, experiments_dir=experiments_dir)
        result = phase1(
            contract,
            ledger_path=where.ledger,
            touch_path=where.touch_budget,
            backtest_path=where.directory / "backtest.html",
        )
        if result.passed:
            card = result.card
            passing.append(name)
            notes.append(
                f"  [pass] {name:<18} {card.model}@{card.version} "
                f"(BSS {card.scorecard['brier_skill_score']:+.4f}, {card.experiment_id})"
            )
        else:
            notes.append(f"  [ .. ] {name:<18} {result.failures()[0]}")
    head = (
        f"national contracts: {len(passing)}/{len(national)} registered with "
        f"scope 'every region' pass the Phase 1 checks; the exit needs "
        f">= {MIN_NATIONAL_PASSES}"
    )
    return Check("national contracts", len(passing) >= MIN_NATIONAL_PASSES,
                 "\n".join([head, *notes])), passing


def _spot_check(
    counts_path: pathlib.Path, snapshot_dir: pathlib.Path
) -> tuple[Check, list[str]]:
    """The exposure join against the assessor counts; the in-band counties come back."""
    from readiness.connectors import usa_structures
    from readiness.connectors.base import ConnectorError, Manifest
    from readiness.exposure import spotcheck
    from readiness.exposure.table import ExposureError, ExposureTable

    name = "exposure spot-check"
    if not counts_path.exists():
        return Check(name, False, f"exposure spot-check: no counts file at "
                     f"{data_mod.relative(counts_path)}"), []
    try:
        counts = spotcheck.load(counts_path)
    except (spotcheck.SpotCheckError, OSError) as exc:
        return Check(name, False, f"exposure spot-check: {exc}"), []
    if not counts:
        return Check(
            name, False,
            "exposure spot-check: exposure_expected/assessor_counts.csv is header-only; "
            "the ten counts are collected by a person, with URLs",
        ), []

    states = sorted({row.fips[:2] for row in counts})
    manifest = Manifest.load(snapshot_dir / "manifest.json")
    absent = [st for st in states if usa_structures.manifest_key(st) not in manifest.records]
    if absent:
        return Check(
            name, False,
            "exposure spot-check: no USA Structures extract is pinned for state(s) "
            f"{', '.join(absent)}, which the counts file samples; run "
            f"`readiness exposure snapshot --states {','.join(absent)}`",
        ), []
    try:
        table = ExposureTable.load(snapshot_dir, manifest, states)
    except (ExposureError, ConnectorError, OSError, ValueError) as exc:
        return Check(name, False, f"exposure spot-check: {exc}"), []

    checks = spotcheck.run(table, counts)
    summary = spotcheck.summary(checks)
    counties = sorted({c.fips for c in checks if c.within})
    return Check(name, summary.within_bounds, spotcheck.format(checks)), counties


def _issued_files(
    names: list[str], issued_dir: pathlib.Path | None
) -> dict[str, dict[str, object]]:
    """Per contract, its issued files by period label."""
    from readiness.issue import read_issued

    out: dict[str, dict[str, object]] = {}
    for name in names:
        out[name] = {i.period_label: i for i in read_issued(name, issued_dir)}
    return out


def _issued_check(
    known: dict[str, Contract],
    passing: list[str],
    issued_dir: pathlib.Path | None,
    experiments_dir: pathlib.Path | None = None,
) -> tuple[Check, str, dict]:
    """One period issued by the passing contracts, each from its first test card."""
    name = "issued"
    if not passing:
        return Check(name, False, "issued: no national contract passes Phase 1 yet"), "", {}
    files = _issued_files(passing, issued_dir)
    labels: dict[str, list[str]] = {}
    for contract_name, by_label in files.items():
        for label in by_label:
            labels.setdefault(label, []).append(contract_name)
    if not labels:
        return Check(
            name, False,
            f"issued: no issued file for any of {passing}; run `readiness issue MODEL "
            "-c NAME --period YYYY-Qn`",
        ), "", {}
    label = sorted(labels, key=lambda k: (-len(labels[k]), k))[0]
    covered = sorted(labels[label])
    chosen = {n: files[n][label] for n in covered}

    problems = []
    for contract_name, issued in sorted(chosen.items()):
        first = _first_test_card(known[contract_name], experiments_dir)
        if first is not None and issued.validated_by != first:
            problems.append(
                f"{contract_name}: issued/{contract_name}/{label}.json names "
                f"{issued.validated_by}, but the contract's first test card is {first}"
            )
    missing = sorted(set(passing) - set(covered))
    ok = len(covered) >= MIN_NATIONAL_PASSES and not problems
    lines = [
        f"issued: {len(covered)}/{len(passing)} passing national contract(s) have an "
        f"issued file for {label}; the exit needs >= {MIN_NATIONAL_PASSES}"
    ]
    lines += [f"  [ok]  {n}: issued/{n}/{label}.json ({chosen[n].validated_by})"
              for n in covered]
    lines += [f"  [..]  {n}: nothing issued for {label}" for n in missing]
    lines += [f"  [FAIL] {p}" for p in problems]
    return Check(name, ok, "\n".join(lines)), label, chosen


def _first_test_card(
    contract: Contract, experiments_dir: pathlib.Path | None = None
) -> str | None:
    """The id of the first test card in a contract's ledger, or None if it has none."""
    where = data_mod.paths(contract, experiments_dir=experiments_dir)
    for card in Ledger(where.ledger).read():
        if card.split == "test":
            return card.experiment_id
    return None


def _brief_identity(doc, fips: str, label: str) -> str:
    """Why this document is not the brief for this county and period, or "".

    Read from the document itself — its kind and the inputs it was built from
    — never from the path it was found at, because the path is what a person
    chose to call a file.
    """
    from readiness import brief as brief_mod

    if doc.kind != brief_mod.KIND:
        return f"kind is {doc.kind!r}, not {brief_mod.KIND!r}"
    got_county = doc.inputs.get("county")
    if got_county != fips:
        return f"inputs name county {got_county!r}, not {fips!r}"
    got_period = doc.inputs.get("period")
    if got_period != label:
        return f"inputs name period {got_period!r}, not {label!r}"
    return ""


def _brief_check(
    known: dict[str, Contract],
    label: str,
    issued: dict,
    counties: list[str],
    *,
    briefs_dir: pathlib.Path | None,
    issued_dir: pathlib.Path | None,
    snapshot_dir: pathlib.Path,
    experiments_dir: pathlib.Path | None,
) -> Check:
    """One committed brief for a spot-checked county, re-validated from the tree."""
    from readiness import brief as brief_mod
    from readiness import cite

    name = "brief"
    if not (label and issued):
        return Check(name, False, "brief: nothing is issued, so no brief can cite one")
    if not counties:
        return Check(
            name, False,
            "brief: no county is in the exposure spot-check band, so there is no "
            "county whose brief the exit would accept",
        )
    root = brief_mod.briefs_root(briefs_dir)
    resolve = brief_mod.resolver(
        known, issued_dir=issued_dir, snapshot_dir=snapshot_dir,
        experiments_dir=experiments_dir,
    )
    tried: list[str] = []
    for fips in counties:
        path = root / fips / f"{label}.json"
        if not path.exists():
            continue
        try:
            doc = cite.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            tried.append(f"{data_mod.relative(path)}: unreadable ({exc})")
            continue
        # The file's path is not evidence of what is in it. A document that
        # is not a county brief, or is one for another county or period,
        # would otherwise pass this criterion from the right filename.
        mismatch = _brief_identity(doc, fips, label)
        if mismatch:
            tried.append(f"{data_mod.relative(path)}: {mismatch}")
            continue
        violations = brief_mod.check(doc, resolve)
        if not violations:
            return Check(
                name, True,
                f"brief {data_mod.relative(path)} validates with zero violations and "
                f"names nothing below the county ({len(doc.sentences)} sentences, "
                f"{len(doc.claims)} claims)",
            )
        tried.append(
            f"{data_mod.relative(path)}: {len(violations)} violation(s), "
            f"first: {violations[0]}"
        )
    head = (
        f"brief: no validating brief for {label} in the {len(counties)} spot-checked "
        f"county/counties ({', '.join(counties[:5])}"
        f"{', ...' if len(counties) > 5 else ''})"
    )
    detail = [head] + [f"  {t}" for t in tried]
    if not tried:
        detail.append(
            f"  nothing under {data_mod.relative(root)}/<fips>/{label}.json; run "
            f"`readiness brief --county {counties[0]} --period {label}`"
        )
    return Check(name, False, "\n".join(detail))


# ---------------------------------------------------------------------------
# Phase 3 (plan §4)
# ---------------------------------------------------------------------------

#: How many distinct facilities the exit criterion needs a useful-or-better
#: blinded review for. Report §6: "at least three real facilities".
MIN_USEFUL_REVIEWS = 3


@dataclass(frozen=True)
class Phase3Result:
    """The Phase 3 exit criteria, each as a check with its evidence.

    Like Phase 2 it takes no contract: the criteria are about the gap reports
    and their reviews, not about one hazard. Two of the four are honest about
    what a machine cannot establish. Nothing here can know that a facility is
    real or that a reviewer is a practising emergency manager; what it checks
    is that each rating is bound to the sha256 of a blinded rendering that
    still exists and still validates, that every committed case study still
    reproduces its expected findings, and that no committed plan JSON has
    grown an address or a coordinate.
    """

    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def failures(self) -> list[str]:
        return [c.detail.splitlines()[0] for c in self.checks if not c.passed]


def phase3(
    *,
    reports_dir: pathlib.Path | None = None,
    reviews_dir: pathlib.Path | None = None,
    case_studies_dir: pathlib.Path | None = None,
    plans_dir: pathlib.Path | None = None,
) -> Phase3Result:
    """Evaluate the Phase 3 exit criteria from committed and supplied files.

    Real facility files, gap reports and review records are never committed
    (plan §4), so `reports_dir` and `reviews_dir` point at the planner's own
    tree; the case studies and the no-coordinates scan read `plans/` itself.
    """
    from readiness.plans import reviews as reviews_mod

    try:
        reviews = reviews_mod.load_all(reviews_dir)
        read_error = ""
    except reviews_mod.ReviewError as exc:
        reviews, read_error = [], str(exc)

    reviews_check, counted = _reviews_check(reviews, reviews_dir, read_error)
    reports_check = _reports_check(counted, reports_dir)
    studies_check = _case_studies_check(case_studies_dir)
    coordinates_check = _no_coordinates_check(plans_dir)
    return Phase3Result(
        (reviews_check, reports_check, studies_check, coordinates_check)
    )


def _blinded_renders(reports_dir: pathlib.Path | None) -> tuple[dict, list[str]]:
    """sha256 -> the document that renders to it, over every JSON under the tree.

    A blinded page is found by **content**, never by path. `<period>.json`
    beside `<period>.blind.html` was a file-name convention and nothing more:
    the JSON could be replaced wholesale with a document saying the opposite
    of the page a reviewer rated, and this check would still have re-validated
    the replacement and called it the reviewed report. Re-rendering every
    document and keying on the sha closes document -> page -> sha, which is
    the binding a review record actually asserts.
    """
    from readiness import cite
    from readiness.plans import gap_report as gap_report_mod

    root = gap_report_mod.reports_root(reports_dir)
    found: dict[str, object] = {}
    notes: list[str] = []
    for path in sorted(root.rglob("*.json")) if root.exists() else []:
        try:
            doc = cite.from_json(path.read_text(encoding="utf-8"))
            _page, sha = gap_report_mod.render_blinded(doc)
        except (OSError, ValueError, KeyError, TypeError,
                gap_report_mod.GapReportError):
            notes.append(str(path))
            continue
        found.setdefault(sha, doc)
    return found, notes


def _reviews_check(
    reviews: list, reviews_dir: pathlib.Path | None, read_error: str
) -> tuple[Check, list]:
    """At least three distinct facilities rated useful or better, blinded."""
    from readiness.plans import reviews as reviews_mod

    name = "reviews"
    root = reviews_mod.reviews_root(reviews_dir)
    if read_error:
        return Check(name, False, f"reviews: {read_error}"), []
    counted = reviews_mod.counting(reviews)
    facilities = reviews_mod.distinct_facilities(counted)
    lines = [
        f"reviews: {len(facilities)}/{MIN_USEFUL_REVIEWS} distinct facility/facilities "
        f"with a blinded review by a practising emergency manager rated "
        f"{' or '.join(sorted(reviews_mod.USEFUL_OR_BETTER))} "
        f"({len(reviews)} review(s) read from {data_mod.relative(root)})"
    ]
    for review in counted:
        lines.append(
            f"  [ok]  {review.facility_label} {review.period}: {review.rating!r} from "
            f"{review.reviewer_role!r} ({review.organisation_type}, "
            f"{review.years_in_role} year(s))"
        )
    for review in reviews:
        if review.counts:
            continue
        why = []
        if not review.blinded:
            why.append("not blinded")
        if not review.by_emergency_manager:
            why.append(f"role {review.reviewer_role!r} is not an emergency manager")
        if not review.useful:
            why.append(f"rated {review.rating!r}")
        lines.append(f"  [ .. ] {review.facility_label}: {'; '.join(why)}")
    lines.append(
        "  the record is an attestation: nothing here can establish that a facility "
        "is real or that a reviewer practises emergency management"
    )
    return Check(name, len(facilities) >= MIN_USEFUL_REVIEWS, "\n".join(lines)), counted


def _reports_check(counted: list, reports_dir: pathlib.Path | None) -> Check:
    """Each counted review names a document that renders to exactly its sha.

    No path under the reports tree is ever printed. A line pairing a blinded
    label with `plans/reports/<slug>/...` is a de-blinding table, and this
    command's output is pasted into pull requests and reports. What is printed
    is the label, the first sixteen hex of the sha, and the shape of the
    document — which is what a reader needs and all a reviewer may have.
    """
    from readiness.plans import gap_report as gap_report_mod

    name = "reports"
    root = gap_report_mod.reports_root(reports_dir)
    if not counted:
        return Check(name, False, "reports: no review counts yet, so none can be traced "
                     f"to a blinded report under {data_mod.relative(root)}")
    rendered, unreadable = _blinded_renders(reports_dir)
    lines: list[str] = []
    ok = True
    for review in counted:
        short = review.report_sha256[:16]
        doc = rendered.get(review.report_sha256)
        if doc is None:
            ok = False
            lines.append(
                f"  [FAIL] {review.facility_label}: no document under "
                f"{data_mod.relative(root)} renders to sha256 {short}..., so the "
                "rating is of a document this tree does not hold"
            )
            continue
        page, _sha = gap_report_mod.render_blinded(doc)
        details = gap_report_mod.blind_details(page)
        wanted = (review.facility_label, review.period, gap_report_mod.KIND)
        if details != wanted:
            ok = False
            lines.append(
                f"  [FAIL] {review.facility_label}: the document with sha256 "
                f"{short}... is {details}, not {wanted}, so the review names a "
                "report about another facility, period or kind"
            )
            continue
        violations = _gap_report_violations(doc)
        if violations:
            ok = False
            lines.append(
                f"  [FAIL] {review.facility_label} ({short}...): "
                f"{len(violations)} violation(s), first: {violations[0]}"
            )
            continue
        lines.append(
            f"  [ok]  {review.facility_label} ({short}...): "
            f"{len(doc.sentences)} sentences, {len(doc.claims)} claims, "
            "validates with zero violations"
        )
    if unreadable:
        lines.append(
            f"  [ .. ] {len(unreadable)} file(s) under {data_mod.relative(root)} are "
            "not gap-report documents and were not considered"
        )
    head = (
        f"reports: {sum(1 for line in lines if line.startswith('  [ok]'))}/"
        f"{len(counted)} counted review(s) trace to a blinded render that still "
        f"validates under {data_mod.relative(root)}"
    )
    return Check(name, ok, "\n".join([head, *lines]))


def _gap_report_violations(doc) -> list:
    """Re-validate a written gap report against **its own** claim set.

    Say what this is, because it is easy to read as more. The facility record
    and the scenario a real report was built from are the planner's, not this
    repository's, and neither is committed — so nothing here can re-resolve
    `power.fuel_hours` to a building. What this checks is that the document is
    internally sound: every sentence cites, every citation exists in the
    document, every number is the rendered value of a cited claim, no
    forbidden phrasing, no place named by a key or a value anywhere in the
    JSON, and the kind is a gap report. A document that passed these when it
    was written and fails them now has been edited since.
    """
    from readiness import cite
    from readiness.plans import gap_report as gap_report_mod
    from readiness.plans import scenarios as scenarios_mod

    try:
        library = scenarios_mod.as_known(scenarios_mod.load_all())
    except scenarios_mod.ScenarioError:
        library = set()
    known: dict[str, object] = {
        "facility": {c.source.ref for c in doc.claims if c.source.kind == "facility"},
        "scenario": library
        | {c.source.ref for c in doc.claims if c.source.kind == "scenario"},
        "issued": {c.source.ref.split("#")[0] for c in doc.claims
                   if c.source.kind == "issued"},
        "ledger": {c.source.ref for c in doc.claims if c.source.kind == "ledger"},
        "manifest": {c.source.ref for c in doc.claims if c.source.kind == "manifest"},
    }
    found = gap_report_mod.check(doc, cite.DictResolver(known))
    if doc.kind != gap_report_mod.KIND:
        found.append(cite.Violation(
            cite.UNRESOLVED, None,
            f"the document is a {doc.kind!r}, not a {gap_report_mod.KIND!r}",
        ))
    return found


def _case_studies_check(case_studies_dir: pathlib.Path | None) -> Check:
    """Every committed case study reproduces the findings it expects."""
    from readiness.plans import case_studies as case_studies_mod

    name = "case studies"
    root = case_studies_mod.case_studies_dir(case_studies_dir)
    try:
        results = case_studies_mod.check_all(case_studies_dir)
    except (case_studies_mod.CaseStudyError, ValueError) as exc:
        return Check(name, False, f"case studies: {exc}")
    if not results:
        return Check(
            name, True,
            f"case studies: none committed under {data_mod.relative(root)}, which is "
            "the default — each is an example added deliberately, with a published "
            "investigation cited for every fact",
        )
    failed = [r for r in results if not r.passed]
    head = (
        f"case studies: {len(results) - len(failed)}/{len(results)} reproduce their "
        f"expected findings"
    )
    return Check(name, not failed, "\n".join([head, *(r.format() for r in results)]))


def _no_coordinates_check(plans_dir: pathlib.Path | None) -> Check:
    """No committed JSON under plans/ names a place, by key or by value.

    The same scan `gap_report.check` runs over a rendered document, so the
    rule is one rule rather than two that can drift — and so the no-coordinate
    guard is live over a tree whose keys nothing constrains.
    """
    from readiness.plans import facility as facility_mod
    from readiness.plans import gap_report as gap_report_mod

    name = "no coordinates"
    root = pathlib.Path(plans_dir) if plans_dir else data_mod.REPO_ROOT / "plans"
    if not root.exists():
        return Check(name, False, f"no coordinates: {data_mod.relative(root)} does not exist")
    scanned = 0
    problems: list[str] = []
    for path in sorted(root.rglob("*.json")):
        scanned += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"  [FAIL] {data_mod.relative(path)}: unreadable ({exc})")
            continue
        for key in gap_report_mod.forbidden_keys(payload):
            problems.append(f"  [FAIL] {data_mod.relative(path)}: carries {key}")
    head = (
        f"no coordinates: {scanned} committed JSON file(s) under "
        f"{data_mod.relative(root)} carry none of the keys "
        f"{list(facility_mod.FORBIDDEN_KEYS)} and no value that reads as "
        "a street address, a ZIP+4 or a coordinate pair"
    )
    return Check(name, not problems, "\n".join([head, *problems]))
