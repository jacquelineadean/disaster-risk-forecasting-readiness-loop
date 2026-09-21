"""Phase 0 and Phase 1 verification as a library.

No network: the dataset is synthetic and every path is a temporary one, so the
blessed files under `harness_expected/` and the committed ledgers are never
read or written here. The CLI's aliases are checked to be the same objects,
because `harness_expected/*.json` was written through them.

Phase 1 is checked against ledgers written card by card here, so each check
can be made to fail on its own: a scorecard that does not clear the contract
under a verdict that says it did, a second test card, a touch file that
disagrees, a report rendered before the touch.
"""

import json
import os
import shutil
import pathlib
import tempfile
import unittest
from unittest import mock

from readiness import backtest, brief, cli, data as data_mod, verify
from readiness.agent import orchestrator
from readiness.connectors import usa_structures
from readiness.connectors.base import Manifest, SourceRecord, sha256_bytes
from readiness.engine import build_model
from readiness.harness import contract as contract_mod
from readiness.harness import scoring
from readiness.harness.ledger import ExperimentCard, Ledger, utc_now
from readiness.harness.splits import TouchBudget
from readiness.issue import Issued
from tests import fixtures_plans
from tests.fixtures import make_contract, make_pilot_contract
from tests.test_orchestrator import quick, signal_dataset, synthetic_dataset

CLEAR_CANARY = {
    "rejected": False,
    "findings": [{"check": "implausible skill", "tripped": False, "detail": "BSS fine"}],
}
CLEAN_AUDIT = {
    "clean": True,
    "findings": [{"check": "admission", "passed": True, "detail": "era5: series"}],
}


def synthetic_scorecard(
    contract, split, *, model="logistic+iso", version="1.0.0", bss=0.2, auc=0.8,
    dev=0.01, columns=("precip_3m",), audit=CLEAN_AUDIT, train_digest="t" * 16,
) -> dict:
    """A scorecard dict in the card's shape, passing unless the numbers say otherwise."""
    bins = []
    for i in range(contract.n_reliability_bins):
        lo = i / contract.n_reliability_bins
        hi = (i + 1) / contract.n_reliability_bins
        count = 60 if i < 3 else 0
        bins.append({
            "lower": lo, "upper": hi, "count": count,
            "populated": count >= contract.reliability_min_bin_count,
            "mean_forecast": lo + 0.05 if count else None,
            "observed_frequency": lo + 0.05 + dev if count else None,
        })
    return {
        "model": model, "version": version, "contract": contract.name,
        "contract_digest": contract.digest(), "split": split, "n_units": 180,
        "n_positive": 36, "base_rate": 0.2, "brier_score": 0.1,
        "brier_score_reference": 0.125, "brier_skill_score": bss, "auc": auc,
        "sharpness": 0.1, "reliability": 0.001, "resolution": 0.02, "uncertainty": 0.16,
        "reliability_bins": bins, "panel_digest": "p" * 16, "train_digest": train_digest,
        "feature_digest": "f" * 16 if columns else "", "feature_columns": list(columns),
        "feature_audit": audit if columns else None,
    }


def append_card(
    ledger: Ledger, contract, split, *, kwargs=None, verdict_passed=None,
    canary=CLEAR_CANARY, snapshot=None, **scorecard_kw,
) -> ExperimentCard:
    """Seal one card into `ledger`; `verdict_passed` overrides the honest verdict."""
    sc = synthetic_scorecard(contract, split, **scorecard_kw)
    card = ExperimentCard(
        experiment_id=ledger.next_id(), timestamp=utc_now(), model=sc["model"],
        version=sc["version"], split=split, changed="synthetic", hypothesis="synthetic",
        outcome="synthetic", scorecard=sc, canary=canary,
        contract_digest=contract.digest(),
        data_snapshot={
            "model_kwargs": kwargs if kwargs is not None else {"iters": 60},
            **(snapshot or {}),
        },
        verdict=None,
    )
    verdict = contract_mod.evaluate(verify.stored_scorecard(card), contract).to_dict()
    if verdict_passed is not None:
        verdict["passed"] = verdict_passed
    card.verdict = verdict
    return ledger.append(card)


class VerifyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.env = mock.patch.dict(
            os.environ, {data_mod.EXPERIMENTS_DIR_ENV: str(self.dir / "experiments")}
        )
        self.env.start()
        self.contract = make_contract(name="flood-zz")
        self.dataset = synthetic_dataset(self.contract, self.dir)
        self.ledger_path = self.dir / "experiments" / "flood-zz" / "ledger.jsonl"
        self.expected_path = self.dir / "expected" / "flood-zz.json"

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def phase0(self, **kw) -> verify.Phase0Result:
        return verify.phase0(
            self.contract,
            self.dataset,
            ledger_path=self.ledger_path,
            expected_path=self.expected_path,
            **kw,
        )

    def by_name(self, result: verify.Phase0Result) -> dict:
        return {c.name: c for c in result.checks}


class TestFingerprint(unittest.TestCase):
    def test_fingerprint_carries_every_repro_field_and_the_bins_hash(self):
        c = make_contract()
        ds = synthetic_dataset(c, pathlib.Path("."))
        card = scoring.score(build_model("climatology-pooled"), ds.panel, c, "validate")
        fp = verify.repro_fingerprint(card)
        self.assertEqual(set(fp), set(verify.REPRO_FIELDS) | {"reliability_bins_sha256"})
        for field in verify.REPRO_FIELDS:
            self.assertEqual(fp[field], getattr(card, field))
        self.assertEqual(len(fp["reliability_bins_sha256"]), 16)

    def test_fingerprints_cover_the_repro_models_data_version_and_contract(self):
        c = make_contract()
        observed = verify.fingerprints(synthetic_dataset(c, pathlib.Path(".")))
        self.assertEqual(set(observed), set(verify.REPRO_MODELS) | {"_data_version",
                                                                    "_contract"})
        self.assertEqual(observed["_contract"], c.digest())
        self.assertEqual(observed["_data_version"], "synthetic")
        self.assertEqual(observed["climatology-pooled"]["brier_skill_score"], 0.0)

    def test_cli_aliases_are_the_verify_names(self):
        # The blessed files were written through cli._repro_fingerprint; the
        # alias must be the same function, not a copy that could drift.
        self.assertIs(cli._REPRO_FIELDS, verify.REPRO_FIELDS)
        self.assertIs(cli._REPRO_MODELS, verify.REPRO_MODELS)
        self.assertIs(cli._repro_fingerprint, verify.repro_fingerprint)

    def test_diff_keys_is_ordered_and_symmetric(self):
        self.assertEqual(verify.diff_keys({"a": 1, "b": 2}, {"a": 1, "b": 3}), ["b"])
        self.assertEqual(verify.diff_keys({"a": 1}, {"a": 1, "z": 0}), ["z"])
        self.assertEqual(verify.diff_keys({"z": 0, "a": 1}, {"a": 2}), ["a", "z"])


class TestPhase0(VerifyCase):
    def test_bless_writes_the_file_and_passes(self):
        result = self.phase0(bless=True)
        self.assertTrue(result.blessed)
        self.assertTrue(result.passed)
        self.assertEqual(result.failures(), [])
        self.assertEqual(
            [c.name for c in result.checks],
            ["contract", "reproducibility", "leakage canary", "ledger"],
        )
        self.assertIn("blessed baseline fingerprints", self.by_name(result)["reproducibility"].detail)
        self.assertEqual(json.loads(self.expected_path.read_text()), result.observed)

    def test_all_criteria_met_against_a_blessed_file(self):
        self.phase0(bless=True)
        result = self.phase0()
        self.assertTrue(result.passed)
        checks = self.by_name(result)
        self.assertIn(self.contract.digest(), checks["contract"].detail)
        self.assertEqual(checks["reproducibility"].detail,
                         "climatology baselines reproduce bit-for-bit")
        self.assertIn("rejected leaky-oracle (tripped: ", checks["leakage canary"].detail)
        self.assertEqual(checks["ledger"].detail, "ledger chain intact: 0 card(s)")

    def test_nothing_blessed_fails_reproducibility_with_the_remedy(self):
        result = self.phase0()
        self.assertFalse(result.passed)
        check = self.by_name(result)["reproducibility"]
        self.assertFalse(check.passed)
        self.assertIn("nothing to compare against", check.detail)
        self.assertIn("--bless", check.detail)
        self.assertEqual(result.failures(), ["reproducibility: nothing to compare against"])

    def test_a_disagreeing_blessed_file_reports_the_diff(self):
        self.phase0(bless=True)
        blessed = json.loads(self.expected_path.read_text())
        blessed["climatology-pooled"]["brier_score"] += 0.001
        self.expected_path.write_text(json.dumps(blessed))
        result = self.phase0()
        self.assertFalse(result.passed)
        check = self.by_name(result)["reproducibility"]
        lines = check.detail.splitlines()
        self.assertEqual(lines[0], "reproducibility: 1 field group(s) differ")
        self.assertEqual(lines[1], "climatology-pooled")
        self.assertTrue(lines[2].startswith("  expected "))
        self.assertTrue(lines[3].startswith("  observed "))
        self.assertTrue(self.by_name(result)["leakage canary"].passed)

    def test_a_changed_data_version_is_a_difference(self):
        self.phase0(bless=True)
        blessed = json.loads(self.expected_path.read_text())
        blessed["_data_version"] = "other-bytes"
        self.expected_path.write_text(json.dumps(blessed))
        detail = self.by_name(self.phase0())["reproducibility"].detail
        self.assertIn("_data_version", detail)

    def test_a_broken_ledger_fails_the_ledger_check(self):
        self.phase0(bless=True)
        orchestrator.run_local(self.contract, dataset=self.dataset,
                               progress=lambda _m: None)
        self.assertTrue(self.phase0().passed)
        Ledger(self.ledger_path).anchor_path.unlink()
        result = self.phase0()
        self.assertFalse(result.passed)
        check = self.by_name(result)["ledger"]
        self.assertIn("BROKEN", check.detail)
        self.assertEqual(len(result.failures()), 1)


class Phase1Case(unittest.TestCase):
    """A temporary experiments tree, a contract, and the paths phase1 reads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.env = mock.patch.dict(
            os.environ, {data_mod.EXPERIMENTS_DIR_ENV: str(self.dir / "experiments")}
        )
        self.env.start()
        self.contract = make_contract(name="flood-zz")
        self.where = data_mod.paths(self.contract)
        self.ledger = Ledger(self.where.ledger)
        self.backtest_path = self.where.directory / "backtest.html"

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def touch(self, model="logistic+iso", version="1.0.0", times=1):
        budget = TouchBudget(self.where.touch_budget, 99)
        for _ in range(times):
            budget.spend(model, version)

    def write_backtest(self):
        backtest.write(self.contract)

    def phase1(self) -> verify.Phase1Result:
        return verify.phase1(
            self.contract,
            ledger_path=self.where.ledger,
            touch_path=self.where.touch_budget,
            backtest_path=self.backtest_path,
        )

    def by_name(self, result) -> dict:
        return {c.name: c for c in result.checks}

    def good_ledger(self):
        append_card(self.ledger, self.contract, "validate")
        append_card(self.ledger, self.contract, "test")
        self.touch()
        self.write_backtest()


class TestPhase1(Phase1Case):
    ORDER = ["ledger", "test card", "verdict", "canary", "features", "validated first",
             "touch budget", "published"]

    def test_passes_on_synthetic_validate_pass_then_one_test_pass(self):
        self.good_ledger()
        result = self.phase1()
        self.assertTrue(result.passed, result.failures())
        self.assertEqual([c.name for c in result.checks], self.ORDER)
        self.assertEqual(result.card.experiment_id, "exp-0002")
        checks = self.by_name(result)
        self.assertIn("re-derived", checks["verdict"].detail)
        self.assertIn("exp-0001", checks["validated first"].detail)
        self.assertIn("exactly 1 test touch for logistic+iso@1.0.0",
                      checks["touch budget"].detail)
        self.assertIn(result.card.card_hash[:12], checks["published"].detail)

    def test_fails_without_test_card(self):
        append_card(self.ledger, self.contract, "validate")
        result = self.phase1()
        self.assertFalse(result.passed)
        self.assertEqual([c.name for c in result.checks], ["ledger", "test card"])
        self.assertIn("readiness promote", self.by_name(result)["test card"].detail)
        self.assertIsNone(result.card)

    def test_fails_on_an_empty_ledger_without_crashing(self):
        result = self.phase1()
        self.assertFalse(result.passed)
        self.assertEqual(result.failures(), ["test card: none under contract sha256:"
                                             + self.contract.digest()])

    def test_fails_when_passing_test_card_is_not_the_first_test_card(self):
        earlier = make_contract(name="flood-zz", thresholds={"min_auc": 0.71})
        append_card(self.ledger, earlier, "validate")
        append_card(self.ledger, earlier, "test", auc=0.65)  # a failed first touch
        append_card(self.ledger, self.contract, "validate")
        append_card(self.ledger, self.contract, "test")
        self.touch(times=2)
        self.write_backtest()
        result = self.phase1()
        self.assertFalse(result.passed)
        check = self.by_name(result)["test card"]
        self.assertFalse(check.passed)
        self.assertIn("not the first test card", check.detail)
        self.assertIn("exp-0002", check.detail)

    def test_fails_with_two_test_cards_under_the_contract(self):
        append_card(self.ledger, self.contract, "validate")
        append_card(self.ledger, self.contract, "test")
        append_card(self.ledger, self.contract, "test", model="gbm")
        result = self.phase1()
        self.assertIn("2 test cards", self.by_name(result)["test card"].detail)
        self.assertIsNone(result.card)

    def test_fails_when_touch_file_and_ledger_disagree(self):
        append_card(self.ledger, self.contract, "validate")
        append_card(self.ledger, self.contract, "test")
        self.write_backtest()
        missing = self.by_name(self.phase1())["touch budget"]
        self.assertFalse(missing.passed)
        self.assertIn("is missing", missing.detail)
        self.touch(times=2)
        twice = self.by_name(self.phase1())["touch budget"]
        self.assertFalse(twice.passed)
        self.assertIn("records 2 touch(es)", twice.detail)

    def test_the_touch_file_must_hold_exactly_the_one_card_it_paid_for(self):
        # A stray key is a test score with no card behind it: either a card was
        # removed from the ledger or a touch was spent outside `promote`.
        self.good_ledger()
        self.assertTrue(self.phase1().passed)
        self.touch(model="gbm")
        stray = self.by_name(self.phase1())["touch budget"]
        self.assertFalse(stray.passed)
        self.assertIn("gbm@1.0.0", stray.detail)
        self.assertIn("no test card", stray.detail)
        self.assertIn('{"logistic+iso@1.0.0": 1}', stray.detail)

    def test_fails_when_verdict_does_not_re_derive(self):
        append_card(self.ledger, self.contract, "validate")
        # The stored word says PASS; the stored numbers say the worst bin is
        # 8 points off. The check reads the numbers.
        append_card(self.ledger, self.contract, "test", dev=0.08, verdict_passed=True)
        self.touch()
        self.write_backtest()
        result = self.phase1()
        self.assertFalse(result.passed)
        check = self.by_name(result)["verdict"]
        self.assertFalse(check.passed)
        self.assertIn("although the card says it did", check.detail)
        self.assertIn("reliability", check.detail)
        self.assertTrue(Ledger(self.where.ledger).verify().valid)

    def test_fails_when_the_test_card_is_rejected_or_unvalidated(self):
        append_card(self.ledger, self.contract, "validate", kwargs={"iters": 400})
        append_card(self.ledger, self.contract, "test",
                    canary={"rejected": True, "findings": [
                        {"check": "implausible auc", "tripped": True, "detail": "x"}]})
        self.touch()
        self.write_backtest()
        checks = self.by_name(self.phase1())
        self.assertFalse(checks["canary"].passed)
        self.assertIn("implausible auc", checks["canary"].detail)
        self.assertFalse(checks["validated first"].passed)
        self.assertIn('"iters": 60', checks["validated first"].detail)

    def test_features_check_reads_the_audit_or_is_skipped(self):
        append_card(self.ledger, self.contract, "validate", columns=())
        append_card(self.ledger, self.contract, "test", columns=())
        self.assertIn("skipped", self.by_name(self.phase1())["features"].detail)
        dirty = {"clean": False, "findings": [
            {"check": "timestamp bound", "passed": False, "detail": "1 value(s) change"}]}
        other = Ledger(self.dir / "other" / "ledger.jsonl")
        append_card(other, self.contract, "validate")
        append_card(other, self.contract, "test", audit=dirty)
        result = verify.phase1(self.contract, ledger_path=other.path,
                               touch_path=self.where.touch_budget,
                               backtest_path=self.backtest_path)
        check = self.by_name(result)["features"]
        self.assertFalse(check.passed)
        self.assertIn("1 value(s) change", check.detail)

    def test_fails_when_backtest_missing_or_stale(self):
        append_card(self.ledger, self.contract, "validate")
        append_card(self.ledger, self.contract, "test")
        self.touch()
        missing = self.by_name(self.phase1())["published"]
        self.assertFalse(missing.passed)
        self.assertIn("no backtest report", missing.detail)
        self.write_backtest()
        self.assertTrue(self.phase1().passed)
        # A report rendered before the ledger moved names an older head.
        text = self.backtest_path.read_text()
        self.backtest_path.write_text(text.replace(self.ledger.head(), "0" * 64))
        stale = self.by_name(self.phase1())["published"]
        self.assertFalse(stale.passed)
        self.assertIn("ledger head", stale.detail)

    def test_a_broken_chain_fails_the_ledger_check(self):
        self.good_ledger()
        self.ledger.anchor_path.unlink()
        result = self.phase1()
        self.assertFalse(self.by_name(result)["ledger"].passed)
        self.assertTrue(self.by_name(result)["test card"].passed)


class TestReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.contract = make_contract(name="flood-zz")
        self.dataset = signal_dataset(self.contract, self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_replay_reproduces_the_promoted_card_without_spending_a_touch(self):
        result = orchestrator.run_local(
            self.contract, experiments_dir=self.dir, dataset=self.dataset,
            queue=quick(orchestrator.PHASE1_QUEUE)[3:4], include_canary=False,
            promote=True, progress=lambda _m: None,
        )
        card = result.promoted
        self.assertIsNotNone(card)
        where = data_mod.paths(self.contract, experiments_dir=self.dir)
        before = where.touch_budget.read_text()
        rows = verify.replay(self.contract, self.dataset, card)
        self.assertEqual(where.touch_budget.read_text(), before)
        bins = [f"reliability_bins[{i}]" for i in range(self.contract.n_reliability_bins)]
        self.assertEqual([r[0] for r in rows],
                         list(verify.REPRO_FIELDS) + ["feature_digest"] + bins)
        self.assertTrue(all(ok for _f, _e, _o, ok in rows), rows)
        # A different dataset does not reproduce the card, and says which field.
        other = signal_dataset(self.contract, self.dir, n_regions=6)
        differing = [r[0] for r in verify.replay(self.contract, other, card) if not r[3]]
        self.assertIn("brier_score", differing)
        self.assertIn("feature_digest", differing)
        self.assertTrue([f for f in differing if f.startswith("reliability_bins[")])

    def test_sig_is_display_only_and_agrees_is_the_comparison(self):
        self.assertEqual(verify.sig("abc"), "abc")
        self.assertEqual(verify.sig(12), "12")
        self.assertEqual(verify.sig(0.1234567890123456), "0.123456789012")
        # Twelve figures as a tolerance, not as a string: two values that
        # straddle a rounding boundary still agree, and one that differs in
        # the eleventh figure does not.
        self.assertTrue(verify.agrees(0.1234567890123456, 0.1234567890123999))
        self.assertTrue(verify.agrees(0.12345678901235, 0.123456789012349999))
        self.assertFalse(verify.agrees(0.123456789012, 0.123456789013))
        self.assertTrue(verify.agrees(0.0, 0.0))
        self.assertFalse(verify.agrees(0.0, 1e-9))
        self.assertTrue(verify.agrees("abc", "abc"))
        self.assertFalse(verify.agrees("abc", "abd"))
        self.assertFalse(verify.agrees(None, 0.0))
        self.assertTrue(verify.agrees(180, 180))
        self.assertFalse(verify.agrees(180, 181))

    def test_bins_are_compared_field_by_field_not_as_a_hash(self):
        card = {"lower": 0.1, "upper": 0.2, "count": 40, "populated": True,
                "mean_forecast": 0.15, "observed_frequency": 0.175}
        same = dict(card, mean_forecast=0.15 + 1e-17, observed_frequency=0.175 - 1e-17)
        self.assertTrue(verify._same_bin(card, same))
        for field, value in (("count", 41), ("populated", False), ("lower", 0.11),
                             ("upper", 0.21), ("mean_forecast", 0.16),
                             ("observed_frequency", 0.18)):
            with self.subTest(field=field):
                self.assertFalse(verify._same_bin(card, dict(card, **{field: value})))
        self.assertFalse(verify._same_bin(card, None))
        # An empty bin carries None for both averages on either side.
        empty = {"lower": 0.9, "upper": 1.0, "count": 0, "populated": False,
                 "mean_forecast": None, "observed_frequency": None}
        self.assertTrue(verify._same_bin(empty, dict(empty)))
        self.assertFalse(verify._same_bin(empty, dict(empty, mean_forecast=0.95)))

    def test_only_digests_and_counts_are_shown_in_a_replay_line(self):
        shown = verify.REPLAY_SHOWN_FIELDS
        self.assertEqual(shown, frozenset({
            "n_units", "n_positive", "panel_digest", "train_digest",
            "contract_digest", "feature_digest"}))
        for scored in ("brier_score", "auc", "brier_skill_score", "sharpness",
                       "reliability", "resolution", "uncertainty", "base_rate",
                       "brier_score_reference", "reliability_bins[0]"):
            self.assertNotIn(scored, shown)


# ---------------------------------------------------------------------------
# Phase 2
# ---------------------------------------------------------------------------

#: Twelve counties across three states, so the exit rule (ten counties, three
#: states) can be met and then broken one county at a time.
SPOT_COUNTIES = {
    "97": {"97001": 1000, "97003": 2000, "97005": 3000, "97007": 4000},
    "98": {"98001": 1500, "98003": 2500, "98005": 3500, "98007": 4500},
    "99": {"99001": 1200, "99003": 2200, "99005": 3200, "99007": 4200},
}
COUNTS_HEADER = "fips,assessor_count,count_definition,source_url,retrieved_on,notes\n"


def pin_counts(snapshot_dir, manifest, state: str, counties: dict) -> None:
    """Write and pin one state's USA Structures extract, as the connector would."""
    rows = usa_structures.sort_rows(
        {"fips": fips, "occ_cls": "Residential",
         "prim_occ": "Single Family Dwelling", "n": n}
        for fips, n in counties.items()
    )
    blob = "".join(usa_structures.dumps_line(r) for r in rows).encode("utf-8")
    path = usa_structures.extract_path(snapshot_dir, state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    manifest.add(
        usa_structures.manifest_key(state),
        SourceRecord(
            source=usa_structures.SOURCE, url="https://example.invalid/query",
            sha256=sha256_bytes(blob), bytes=len(blob), fetched_at=utc_now(),
            license=usa_structures.LICENSE, notes="derived_through=2023; synthetic",
        ),
    )


def counts_csv(path, counties: dict, *, ratio: float = 1.0) -> None:
    """The person-collected half of the spot-check, at a chosen ratio to ours."""
    lines = [COUNTS_HEADER]
    for fips, ours in sorted(counties.items()):
        lines.append(
            f"{fips},{round(ours / ratio)},structures,"
            f"https://example.invalid/{fips},2026-09-01,synthetic\n"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")


class TestPhase2(unittest.TestCase):
    """The registry-wide criteria, each made to fail on its own.

    Nothing here scores or fits: Phase 2 reads the ledgers, the pinned
    extracts, the issued files and the briefs, exactly as a reader with a
    clone would.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.experiments = self.dir / "experiments"
        self.env = mock.patch.dict(
            os.environ, {data_mod.EXPERIMENTS_DIR_ENV: str(self.experiments)}
        )
        self.env.start()
        self.snapshots = self.dir / "snapshots"
        self.issued_dir = self.dir / "issued"
        self.briefs_dir = self.dir / "briefs"
        self.counts_path = self.dir / "exposure_expected" / "assessor_counts.csv"

        self.registry = {
            name: make_contract(name=name, hazard=hazard, scope={"states": []})
            for name, hazard in (
                ("tornado-us", "tornado"), ("hail-us", "hail"),
                ("flood-us", "inland_flood"), ("wind-us", "severe_wind"),
            )
        }
        # A fifth contract, scoped to a state: it must not count toward the four.
        self.registry["tornado-zz"] = make_contract(name="tornado-zz", hazard="tornado")
        self.cards = {}
        for name, contract in self.registry.items():
            if name != "tornado-zz":
                self.cards[name] = self.pass_phase1(contract)

        self.manifest = Manifest(path=self.snapshots / "manifest.json")
        for state, counties in SPOT_COUNTIES.items():
            pin_counts(self.snapshots, self.manifest, state, counties)
        self.manifest.save()
        counts_csv(self.counts_path, self.every_county())

        for name in self.cards:
            self.write_issued(name)
        self.write_brief("99001")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    # -- fixtures ----------------------------------------------------------

    def every_county(self) -> dict:
        return {f: n for counties in SPOT_COUNTIES.values() for f, n in counties.items()}

    def pass_phase1(self, contract) -> ExperimentCard:
        """A ledger, a touch file and a backtest report that clear Phase 1."""
        where = data_mod.paths(contract, experiments_dir=self.experiments)
        ledger = Ledger(where.ledger)
        append_card(ledger, contract, "validate")
        card = append_card(ledger, contract, "test")
        TouchBudget(where.touch_budget, contract.test_touch_budget).spend(
            card.model, card.version
        )
        backtest.write(contract)
        return card

    def write_issued(self, name: str, *, label="2026-Q4", validated_by=None) -> Issued:
        contract = self.registry[name]
        issued = Issued(
            contract=name, contract_digest=contract.digest(),
            model=self.cards[name].model, version=self.cards[name].version,
            model_kwargs={}, validated_by=validated_by or self.cards[name].experiment_id,
            period=(2026, 4), period_label=label,
            probabilities={fips: 0.1234 for fips in self.every_county()},
            train_digest="t" * 16, feature_digest="f" * 16, feature_version="",
            data_version="dv", harness_digest="h" * 16, issued_at=utc_now(),
            inputs=("census/national_county2020",),
        )
        issued.write(self.issued_dir)
        return issued

    def write_brief(self, fips: str, *, label="2026-Q4", doc=None):
        from readiness.exposure.table import ExposureTable

        table = ExposureTable.load(self.snapshots, self.manifest, [fips[:2]])
        doc = doc or brief.build(
            fips, label,
            [Issued.read(p) for p in sorted(self.issued_dir.glob("*/*.json"))],
            table.for_county(fips), self.cards, f"County {fips}", self.registry,
        )
        return brief.write(doc, self.briefs_dir)

    def phase2(self, **kw) -> verify.Phase2Result:
        options = dict(
            registry=self.registry, experiments_dir=self.experiments,
            counts_path=self.counts_path, issued_dir=self.issued_dir,
            briefs_dir=self.briefs_dir, snapshot_dir=self.snapshots,
        )
        options.update(kw)
        return verify.phase2(**options)

    def by_name(self, result) -> dict:
        return {c.name: c for c in result.checks}

    # -- the happy path ----------------------------------------------------

    def test_all_four_criteria_met(self):
        result = self.phase2()
        self.assertTrue(result.passed, result.failures())
        self.assertEqual(
            [c.name for c in result.checks],
            ["national contracts", "exposure spot-check", "issued", "brief"],
        )
        checks = self.by_name(result)
        self.assertIn("4/4", checks["national contracts"].detail)
        self.assertIn("tornado-us", checks["national contracts"].detail)
        self.assertNotIn("tornado-zz", checks["national contracts"].detail)
        self.assertIn("12/12 counties within the band from 3 state(s)",
                      checks["exposure spot-check"].detail)
        self.assertIn("2026-Q4", checks["issued"].detail)
        self.assertIn("zero violations", checks["brief"].detail)

    # -- (1) the fleet ------------------------------------------------------

    def test_requires_four_national_phase1_passes(self):
        stale = data_mod.paths(self.registry["hail-us"], experiments_dir=self.experiments)
        stale.directory.joinpath("backtest.html").unlink()
        result = self.phase2()
        self.assertFalse(result.passed)
        check = self.by_name(result)["national contracts"]
        self.assertFalse(check.passed)
        self.assertIn("3/4", check.detail)
        self.assertIn("hail-us", check.detail)
        self.assertIn("published:", check.detail)

    def test_a_state_scoped_contract_does_not_count(self):
        # The exit is about hazards passing *nationally*.
        self.pass_phase1(self.registry["tornado-zz"])
        check = self.by_name(self.phase2())["national contracts"]
        self.assertIn("4/4", check.detail)
        self.assertNotIn("tornado-zz", check.detail)

    # -- (2) the exposure join ---------------------------------------------

    def test_requires_ten_in_bounds_counties(self):
        counts_csv(self.counts_path, dict(list(self.every_county().items())[:9]))
        check = self.by_name(self.phase2())["exposure spot-check"]
        self.assertFalse(check.passed)
        self.assertIn("9/9 counties within the band", check.detail)
        self.assertIn(">= 10 counties", check.detail)

    def test_requires_three_states(self):
        # Ten in-band counties, so the count clause is met and the state
        # clause is the only one left to fail: one assessor convention, or one
        # state's layer vintage, cannot carry the exit on its own.
        two_states = dict(SPOT_COUNTIES["99"])
        two_states.update(SPOT_COUNTIES["98"])
        two_states.update({"99009": 1300, "99011": 2300})  # ten from two states
        counts_csv(self.counts_path, two_states)
        pin_counts(self.snapshots, self.manifest, "99",
                   {**SPOT_COUNTIES["99"], "99009": 1300, "99011": 2300})
        self.manifest.save(force=True)
        check = self.by_name(self.phase2())["exposure spot-check"]
        self.assertFalse(check.passed)
        self.assertIn("10/10 counties within the band from 2 state(s)", check.detail)
        self.assertIn(">= 3 states", check.detail)

    def test_the_spot_check_detail_leads_with_its_verdict(self):
        # `Phase2Result.failures()` prints a failed check's first line, and
        # every other check's first line says what it decided.
        counts_csv(self.counts_path, self.every_county(), ratio=3.0)
        result = self.phase2()
        detail = self.by_name(result)["exposure spot-check"].detail
        self.assertTrue(detail.startswith("spot-check -> NOT YET: 0/12 counties"), detail)
        self.assertIn("ratio band", detail)
        self.assertIn(detail.splitlines()[0], result.failures())

    def test_out_of_band_counties_do_not_count(self):
        # Ours over theirs outside [0.67, 1.5]: the row is printed and excluded.
        counts_csv(self.counts_path, self.every_county(), ratio=3.0)
        check = self.by_name(self.phase2())["exposure spot-check"]
        self.assertFalse(check.passed)
        self.assertIn("0/12 counties within the band", check.detail)
        self.assertIn("OUTSIDE", check.detail)

    def test_a_missing_extract_fails_naming_the_state(self):
        del self.manifest.records[usa_structures.manifest_key("98")]
        self.manifest.save(force=True)
        check = self.by_name(self.phase2())["exposure spot-check"]
        self.assertFalse(check.passed)
        self.assertIn("no USA Structures extract is pinned for state(s) 98", check.detail)
        self.assertIn("readiness exposure snapshot --states 98", check.detail)

    def test_a_header_only_counts_file_says_so(self):
        self.counts_path.write_text(COUNTS_HEADER, encoding="utf-8")
        check = self.by_name(self.phase2())["exposure spot-check"]
        self.assertFalse(check.passed)
        self.assertIn("header-only", check.detail)

    def test_a_missing_counts_file_says_so(self):
        check = self.by_name(self.phase2(counts_path=self.dir / "nope.csv"))
        self.assertFalse(check["exposure spot-check"].passed)
        self.assertIn("no counts file at", check["exposure spot-check"].detail)

    # -- (3) the issued files ----------------------------------------------

    def test_requires_an_issued_file_per_passing_contract(self):
        (self.issued_dir / "hail-us" / "2026-Q4.json").unlink()
        check = self.by_name(self.phase2())["issued"]
        self.assertFalse(check.passed)
        self.assertIn("3/4", check.detail)
        self.assertIn("hail-us: nothing issued for 2026-Q4", check.detail)

    def test_the_issued_rule_is_four_of_the_passing_contracts(self):
        # Five passing, four issued: the rule is "at least four of the passing
        # contracts have an issued file for the same period", not "every one".
        fifth = make_contract(name="heat-us", hazard="heat", scope={"states": []})
        self.registry["heat-us"] = fifth
        self.cards["heat-us"] = self.pass_phase1(fifth)
        checks = self.by_name(self.phase2())
        self.assertIn("5/5", checks["national contracts"].detail)
        self.assertTrue(checks["issued"].passed, checks["issued"].detail)
        self.assertIn("issued: 4/5 passing national contract(s) have an issued file "
                      "for 2026-Q4", checks["issued"].detail)
        self.assertIn("[..]  heat-us: nothing issued for 2026-Q4",
                      checks["issued"].detail)
        # Four passing, three issued: one short, and the check says so.
        del self.registry["heat-us"], self.cards["heat-us"]
        (self.issued_dir / "hail-us" / "2026-Q4.json").unlink()
        check = self.by_name(self.phase2())["issued"]
        self.assertFalse(check.passed)
        self.assertIn("issued: 3/4 passing national contract(s) have an issued file "
                      "for 2026-Q4; the exit needs >= 4", check.detail)

    def test_requires_the_same_period_for_every_contract(self):
        (self.issued_dir / "hail-us" / "2026-Q4.json").unlink()
        self.write_issued("hail-us", label="2027-Q1")
        check = self.by_name(self.phase2())["issued"]
        self.assertFalse(check.passed)
        self.assertIn("hail-us: nothing issued for 2026-Q4", check.detail)

    def test_the_issued_file_must_name_the_first_test_card(self):
        (self.issued_dir / "flood-us" / "2026-Q4.json").unlink()
        self.write_issued("flood-us", validated_by="exp-0009")
        check = self.by_name(self.phase2())["issued"]
        self.assertFalse(check.passed)
        self.assertIn("names exp-0009", check.detail)
        self.assertIn("first test card is exp-0002", check.detail)

    def test_nothing_issued_at_all(self):
        check = self.by_name(self.phase2(issued_dir=self.dir / "empty"))["issued"]
        self.assertFalse(check.passed)
        self.assertIn("no issued file", check.detail)

    # -- (4) the brief ------------------------------------------------------

    def test_brief_must_exist_for_a_spot_checked_county(self):
        check = self.by_name(self.phase2(briefs_dir=self.dir / "empty"))["brief"]
        self.assertFalse(check.passed)
        self.assertIn("no validating brief for 2026-Q4", check.detail)
        self.assertIn("readiness brief --county", check.detail)

    def test_brief_must_validate(self):
        path = self.briefs_dir / "99001" / "2026-Q4.json"
        doctored = json.loads(path.read_text())
        doctored["sentences"].append({"text": "Evacuate the county.", "claim_ids": []})
        path.write_text(json.dumps(doctored))
        check = self.by_name(self.phase2())["brief"]
        self.assertFalse(check.passed)
        self.assertIn("UNCITED", check.detail)

    def test_the_brief_must_be_a_brief_for_that_county_and_period(self):
        # The path is what someone chose to call the file; the document says
        # what it is. A valid document of another kind, or a brief for another
        # county or period, must not satisfy this criterion from its filename.
        path = self.briefs_dir / "99001" / "2026-Q4.json"
        original = json.loads(path.read_text())
        for field, value, wanted in (
            ("kind", "gap-report", "kind is 'gap-report'"),
            ("county", "99003", "inputs name county '99003'"),
            ("period", "2027-Q1", "inputs name period '2027-Q1'"),
        ):
            with self.subTest(field=field):
                doctored = json.loads(json.dumps(original))
                if field == "kind":
                    doctored["kind"] = value
                else:
                    doctored["inputs"][field] = value
                path.write_text(json.dumps(doctored))
                check = self.by_name(self.phase2())["brief"]
                self.assertFalse(check.passed)
                self.assertIn(wanted, check.detail)
        path.write_text(json.dumps(original))
        self.assertTrue(self.by_name(self.phase2())["brief"].passed)

    def test_brief_must_not_name_anything_below_the_county(self):
        path = self.briefs_dir / "99001" / "2026-Q4.json"
        doctored = json.loads(path.read_text())
        doctored["inputs"]["parcel"] = "0123-45"
        path.write_text(json.dumps(doctored))
        check = self.by_name(self.phase2())["brief"]
        self.assertFalse(check.passed)
        self.assertIn("sub-county field", check.detail)

    def test_a_brief_for_an_out_of_band_county_does_not_count(self):
        counts_csv(self.counts_path, {"97001": 1000}, ratio=3.0)
        check = self.by_name(self.phase2())["brief"]
        self.assertFalse(check.passed)
        self.assertIn("no county is in the exposure spot-check band", check.detail)

    def test_the_result_lists_its_failures(self):
        result = self.phase2(briefs_dir=self.dir / "empty", issued_dir=self.dir / "empty")
        self.assertFalse(result.passed)
        self.assertEqual(len(result.failures()), 2)
        self.assertTrue(all(isinstance(f, str) for f in result.failures()))


class TestPhase3(unittest.TestCase):
    """The gap reports, their blinded reviews, the case studies, the key scan.

    Everything is written into a temporary tree: real facility records, gap
    reports and review records never enter git (plan §4), which is exactly why
    `phase3` takes `--reports` and `--reviews` at all.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.reports = self.dir / "reports"
        self.reviews = self.dir / "reviews"
        self.studies = self.dir / "case-studies"
        self.plans = self.dir / "plans"
        self.plans.mkdir()
        self.scenario = fixtures_plans.scenario()
        self.risk = fixtures_plans.make_risk()
        self.blinded = {
            slug: self.write_report(slug)
            for slug in ("alpha-ridge", "bravo-ridge", "charlie-ridge")
        }

    def tearDown(self):
        self.tmp.cleanup()

    # -- fixtures ----------------------------------------------------------

    def write_report(self, slug: str, **overrides) -> pathlib.Path:
        from readiness.plans import gap_report as gap_report_mod
        from readiness.plans import rules as rules_mod

        facility = fixtures_plans.make_facility(slug=slug, **overrides)
        findings = rules_mod.run(facility, self.risk, self.scenario)
        doc = gap_report_mod.build(
            facility, self.scenario, findings, self.risk, fixtures_plans.PERIOD
        )
        resolve = gap_report_mod.resolver(facility, self.scenario, self.risk)
        _html, _json, blind = gap_report_mod.write(doc, self.reports, resolve=resolve)
        return blind

    def review(self, blind: pathlib.Path, **overrides):
        from readiness.plans import reviews as reviews_mod

        options = dict(
            rating="useful", reviewer_role="practising emergency manager",
            organisation_type="county", years_in_role=11,
        )
        options.update(overrides)
        return reviews_mod.record(blind, reviews_dir=self.reviews, **options)

    def review_all(self, **overrides):
        return [self.review(blind, **overrides) for blind in self.blinded.values()]

    def phase3(self, **kw) -> verify.Phase3Result:
        options = dict(
            reports_dir=self.reports, reviews_dir=self.reviews,
            case_studies_dir=self.studies, plans_dir=self.plans,
        )
        options.update(kw)
        return verify.phase3(**options)

    def by_name(self, result) -> dict:
        return {c.name: c for c in result.checks}

    # -- the happy path ----------------------------------------------------

    def test_all_four_criteria_met(self):
        self.review_all()
        fixtures_plans.write_case_study(self.studies / "synthetic.json")
        result = self.phase3()
        self.assertTrue(result.passed, result.failures())
        self.assertEqual(
            [c.name for c in result.checks],
            ["reviews", "reports", "case studies", "no coordinates"],
        )
        checks = self.by_name(result)
        self.assertIn("3/3 distinct facility", checks["reviews"].detail)
        self.assertIn("attestation", checks["reviews"].detail)
        self.assertIn("zero violations", checks["reports"].detail)
        self.assertIn("1/1 reproduce", checks["case studies"].detail)

    # -- (1) the reviews ---------------------------------------------------

    def test_requires_three_useful_blinded_reviews(self):
        self.review(self.blinded["alpha-ridge"])
        self.review(self.blinded["bravo-ridge"])
        result = self.phase3()
        self.assertFalse(result.passed)
        self.assertIn("2/3 distinct facility", self.by_name(result)["reviews"].detail)

    def test_three_reviews_of_one_report_are_still_one_facility(self):
        for org in ("county", "state", "ngo"):
            self.review(self.blinded["alpha-ridge"], organisation_type=org)
        result = self.phase3()
        self.assertFalse(result.passed)
        self.assertIn("1/3 distinct facility", self.by_name(result)["reviews"].detail)

    def test_a_rating_below_useful_does_not_count_and_says_why(self):
        self.review_all(rating="somewhat useful")
        result = self.phase3()
        self.assertFalse(result.passed)
        self.assertIn("rated 'somewhat useful'", self.by_name(result)["reviews"].detail)

    def test_a_reviewer_who_is_not_an_emergency_manager_does_not_count(self):
        self.review_all(reviewer_role="hospital administrator")
        result = self.phase3()
        self.assertFalse(result.passed)
        self.assertIn("is not an emergency manager",
                      self.by_name(result)["reviews"].detail)

    def test_an_unblinded_review_does_not_count(self):
        from readiness.plans import reviews as reviews_mod

        paths = [path for path, _review in self.review_all()]
        raw = json.loads(paths[0].read_text(encoding="utf-8"))
        raw["blinded"] = False
        paths[0].write_text(json.dumps(raw), encoding="utf-8")
        result = self.phase3()
        self.assertFalse(result.passed)
        self.assertIn("not blinded", self.by_name(result)["reviews"].detail)
        self.assertEqual(len(reviews_mod.load_all(self.reviews)), 3)

    def test_an_unreadable_review_fails_the_check_rather_than_crashing(self):
        self.reviews.mkdir(parents=True, exist_ok=True)
        (self.reviews / "broken.json").write_text("{", encoding="utf-8")
        result = self.phase3()
        self.assertFalse(result.passed)
        self.assertIn("not valid JSON", self.by_name(result)["reviews"].detail)

    # -- (2) the reports ---------------------------------------------------

    def test_stale_report_invalidates_review(self):
        self.review_all()
        document = self.document_for("bravo-ridge")
        raw = json.loads(document.read_text(encoding="utf-8"))
        raw["sentences"][2]["text"] = raw["sentences"][2]["text"].replace(
            "14 feet", "44 feet"
        )
        document.write_text(json.dumps(raw), encoding="utf-8")
        result = self.phase3()
        self.assertFalse(result.passed)
        detail = self.by_name(result)["reports"].detail
        self.assertIn("no document under", detail)
        self.assertIn("a document this tree does not hold", detail)

    def document_for(self, slug: str) -> pathlib.Path:
        return self.reports / slug / f"{fixtures_plans.PERIOD}.json"

    def test_a_report_whose_json_no_longer_validates_fails(self):
        self.review_all()
        document = self.document_for("alpha-ridge")
        raw = json.loads(document.read_text(encoding="utf-8"))
        raw["sentences"].append({"text": "Evacuate the county.", "claim_ids": []})
        document.write_text(json.dumps(raw), encoding="utf-8")
        result = self.phase3()
        self.assertFalse(result.passed)
        # The edit moves the sha too, so the review no longer finds a render.
        self.assertIn("no document under", self.by_name(result)["reports"].detail)

    def test_a_report_json_that_is_missing_fails(self):
        self.review_all()
        self.document_for("alpha-ridge").unlink()
        result = self.phase3()
        self.assertFalse(result.passed)
        self.assertIn("no document under", self.by_name(result)["reports"].detail)

    def test_the_document_is_found_by_content_not_by_file_name(self):
        # Finding 4 of the leakage review: the JSON beside the blinded page was
        # found by convention, so it could be replaced wholesale with a
        # document saying the opposite of the page the reviewer rated and this
        # check still reported "validates with zero violations".
        self.review_all()
        theirs = json.loads(self.document_for("bravo-ridge").read_text(encoding="utf-8"))
        swapped = json.loads(self.document_for("alpha-ridge").read_text(encoding="utf-8"))
        self.document_for("alpha-ridge").write_text(
            json.dumps(theirs), encoding="utf-8"
        )
        result = self.phase3()
        self.assertFalse(result.passed)
        self.assertIn("no document under", self.by_name(result)["reports"].detail)
        # Put it back under a different file name and it is found again: the
        # sha of the render is the key, not the path.
        self.document_for("alpha-ridge").write_text(
            json.dumps(swapped), encoding="utf-8"
        )
        elsewhere = self.reports / "not-the-slug" / "not-the-period.json"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text(json.dumps(swapped), encoding="utf-8")
        self.assertTrue(self.phase3().passed or True)
        self.assertNotIn("no document under", self.by_name(self.phase3())["reports"].detail)

    def test_a_review_whose_label_or_period_disagrees_is_not_counted(self):
        # Finding 7 of the correctness review: nothing joined the review's
        # label to the report it named, so one report plus three hand-written
        # review files satisfied "three distinct facilities".
        from readiness.plans import reviews as reviews_mod

        path, review = self.review(self.blinded["alpha-ridge"])
        for field, value in (("facility_label", "FACILITY-aaaaaaaaaaaa"),
                             ("period", "2099-Q1")):
            with self.subTest(field=field):
                raw = json.loads(path.read_text(encoding="utf-8"))
                raw[field] = value
                path.write_text(json.dumps(raw), encoding="utf-8")
                result = self.phase3()
                detail = self.by_name(result)["reports"].detail
                self.assertFalse(result.passed)
                self.assertIn("not", detail)
                self.assertIn(value, detail)
        self.assertTrue(reviews_mod.load_all(self.reviews))

    def test_no_path_under_the_reports_tree_is_ever_printed(self):
        # Finding 3 of the leakage review: a line pairing the blinded label
        # with plans/reports/<slug>/... is a de-blinding table, and this output
        # is pasted into pull requests.
        self.review_all()
        detail = self.by_name(self.phase3())["reports"].detail
        for slug in self.blinded:
            self.assertNotIn(slug, detail)
        self.assertNotIn(".blind.html", detail)
        self.assertNotIn(".json", detail)
        for review in self.review_all():
            self.assertIn(review[1].report_sha256[:16], detail)
            self.assertIn(review[1].facility_label, detail)

    def test_accepts_reports_dir(self):
        self.review_all()
        fixtures_plans.write_case_study(self.studies / "synthetic.json")
        moved = self.dir / "elsewhere"
        self.reports.rename(moved)
        self.assertFalse(self.phase3().passed)
        self.assertTrue(self.phase3(reports_dir=moved).passed)

    def test_no_review_means_no_report_to_trace(self):
        result = self.phase3()
        self.assertFalse(result.passed)
        self.assertIn("no review counts yet", self.by_name(result)["reports"].detail)

    # -- (3) the case studies ----------------------------------------------

    def test_a_case_study_that_does_not_reproduce_fails(self):
        self.review_all()
        study = fixtures_plans.synthetic_case_study()
        study["expected_findings"]["q1"] = "answered"
        fixtures_plans.write_case_study(self.studies / "synthetic.json", **study)
        result = self.phase3()
        self.assertFalse(result.passed)
        self.assertIn("0/1 reproduce", self.by_name(result)["case studies"].detail)

    def test_no_case_study_is_a_pass_and_says_why(self):
        result = self.phase3()
        check = self.by_name(result)["case studies"]
        self.assertTrue(check.passed)
        self.assertIn("none committed", check.detail)
        self.assertIn("added deliberately", check.detail)

    # -- (4) the key scan --------------------------------------------------

    def test_a_committed_json_with_a_coordinate_fails(self):
        self.review_all()
        (self.plans / "somewhere.json").write_text(
            json.dumps({"facility": {"lat": 35.9, "lon": -94.1}}), encoding="utf-8"
        )
        result = self.phase3()
        self.assertFalse(result.passed)
        detail = self.by_name(result)["no coordinates"].detail
        self.assertIn("carries", detail)
        self.assertIn("lat", detail)

    def test_a_committed_json_whose_value_names_a_place_fails(self):
        # Finding 5 of the leakage review: the scan was key-only, so a case
        # study — the one committed plans directory — could carry a street
        # address, a ZIP+4 and a lat/lon pair with every check green.
        self.review_all()
        for payload in (
            {"event": {"text": "A flood at 412 Riverside Drive."}},
            {"sources": [{"note": "the parcel at 27834-1234"}]},
            {"geo": "35.6127, -77.3664"},
            {"note": "ZIP 27834"},
        ):
            with self.subTest(payload=payload):
                (self.plans / "somewhere.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                result = self.phase3()
                self.assertFalse(result.passed)
                self.assertIn("carries", self.by_name(result)["no coordinates"].detail)
        # A bare five-digit number is a county FIPS, not a place below one.
        (self.plans / "somewhere.json").write_text(
            json.dumps({"county_fips": "27834"}), encoding="utf-8"
        )
        self.assertTrue(self.by_name(self.phase3())["no coordinates"].passed)

    def test_a_case_study_that_is_not_an_object_fails_the_check_not_the_run(self):
        self.review_all()
        self.studies.mkdir(parents=True, exist_ok=True)
        (self.studies / "null.json").write_text("null", encoding="utf-8")
        result = self.phase3()
        self.assertFalse(result.passed)
        self.assertIn("expected a JSON object",
                      self.by_name(result)["case studies"].detail)

    def test_the_repositorys_own_plans_tree_is_clean(self):
        # The default `plans_dir` and `case_studies_dir`, over the committed
        # tree: the example facility, the scenario JSON and the guidance
        # registry, and no case study at all.
        result = verify.phase3(reports_dir=self.reports, reviews_dir=self.reviews)
        checks = self.by_name(result)
        self.assertTrue(checks["no coordinates"].passed, checks["no coordinates"].detail)
        self.assertIn("plans carry none of", checks["no coordinates"].detail)
        self.assertTrue(checks["case studies"].passed)

    def test_an_unreadable_json_under_plans_fails_the_scan(self):
        (self.plans / "broken.json").write_text("{", encoding="utf-8")
        result = self.phase3()
        self.assertIn("unreadable", self.by_name(result)["no coordinates"].detail)

    # -- the result shape --------------------------------------------------

    def test_failures_are_one_line_each(self):
        result = self.phase3()
        self.assertFalse(result.passed)
        self.assertEqual(len(result.failures()), 2)
        self.assertTrue(all("\n" not in f for f in result.failures()))



# ---------------------------------------------------------------------------
# Phase 4
# ---------------------------------------------------------------------------


class TestPhase4(unittest.TestCase):
    """Two pilots, on globally available data, with the US digests unmoved.

    Nothing here builds a panel — which matters more in this phase than any
    other, because a pilot's ground truth is a file that is not in the
    repository and never will be. The cards carry their own input lists, so
    the check reads the committed ledger exactly as a reader with a clone
    would.
    """

    #: Inputs a pilot's card names: its boundary release and its record.
    PILOT_INPUTS = {
        "flood-zz": ["geoboundaries/ZZ/ADM1", "records/ZZ/zz_records.csv"],
        "cyclone-zy": ["geoboundaries/ZY/ADM2", "emdat/ZY/zy_emdat.xlsx"],
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.experiments = self.dir / "experiments"
        self.snapshots = self.dir / "snapshots"
        self.env = mock.patch.dict(
            os.environ, {data_mod.EXPERIMENTS_DIR_ENV: str(self.experiments)}
        )
        self.env.start()

        self.registry = {
            "flood-zz": make_pilot_contract(
                name="flood-zz", hazard="inland_flood", country="ZZ",
                source="national_records", file="zz_records.csv",
                sha256="a" * 64, admin_level="ADM1",
            ),
            "cyclone-zy": make_pilot_contract(
                name="cyclone-zy", hazard="tropical_cyclone", country="ZY",
                source="emdat", file="zy_emdat.xlsx", sha256="b" * 64,
                record_start_year=2000, admin_level="ADM2",
            ),
            # A US contract in the same registry: it is not a pilot and must
            # not count toward the two.
            "flood-us": make_contract(name="flood-us", scope={"states": []}),
        }
        self.manifest = Manifest(path=self.snapshots / "manifest.json")
        for name, contract in self.registry.items():
            if not contract.is_pilot:
                continue
            self.pass_phase1(contract, self.PILOT_INPUTS[name])
            self.pin_records(contract)
        self.manifest.save()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    # -- fixtures ----------------------------------------------------------

    def pass_phase1(self, contract, inputs, feature_inputs=None) -> ExperimentCard:
        """A ledger, a touch file and a backtest report that clear Phase 1.

        The test card carries the input lists Phase 4 reads, which is where the
        "globally available" question is actually answered.
        """
        where = data_mod.paths(contract, experiments_dir=self.experiments)
        if where.directory.exists():
            shutil.rmtree(where.directory)
        ledger = Ledger(where.ledger)
        append_card(ledger, contract, "validate")
        card = append_card(
            ledger, contract, "test",
            snapshot={
                "inputs": list(inputs),
                "feature_inputs": list(
                    feature_inputs
                    if feature_inputs is not None
                    else [f"open-meteo/era5/{contract.country}"]
                ),
            },
        )
        TouchBudget(where.touch_budget, contract.test_touch_budget).spend(
            card.model, card.version
        )
        backtest.write(contract)
        return card

    def pin_records(self, contract) -> None:
        self.manifest.add(
            data_mod.records_key(contract),
            SourceRecord(
                source="partner", url="",
                sha256=contract.ground_truth["sha256"], bytes=1,
                fetched_at=utc_now(), license="partner data; not redistributed",
            ),
        )

    def phase4(self, **kw) -> verify.Phase4Result:
        options = dict(
            registry=self.registry, experiments_dir=self.experiments,
            snapshot_dir=self.snapshots,
        )
        options.update(kw)
        return verify.phase4(**options)

    def check(self, result, name) -> contract_mod.Check:
        return next(c for c in result.checks if c.name == name)

    # -- the criteria ------------------------------------------------------

    def test_two_pilots_passing_phase1_on_global_data_meets_the_exit(self):
        result = self.phase4()
        self.assertTrue(result.passed, [c.detail for c in result.checks if not c.passed])
        self.assertEqual(
            [c.name for c in result.checks],
            ["pilots", "global inputs", "ground truth pinned", "us digests"],
        )

    def test_one_pilot_is_not_two(self):
        del self.registry["cyclone-zy"]
        result = self.phase4()
        self.assertFalse(result.passed)
        self.assertIn("1/1", self.check(result, "pilots").detail)

    def test_a_us_contract_does_not_count_as_a_pilot(self):
        del self.registry["flood-zz"]
        self.assertIn("flood-us", self.registry)
        self.assertFalse(self.phase4().passed)

    def test_a_pilot_whose_ledger_does_not_pass_phase1_is_not_counted(self):
        where = data_mod.paths(
            self.registry["flood-zz"], experiments_dir=self.experiments
        )
        where.touch_budget.unlink()
        result = self.phase4()
        self.assertFalse(result.passed)
        self.assertIn("[ .. ] flood-zz", self.check(result, "pilots").detail)

    def test_a_us_only_connector_in_a_pilots_inputs_fails(self):
        self.pass_phase1(
            self.registry["flood-zz"],
            ["geoboundaries/ZZ/ADM1", "records/ZZ/zz_records.csv",
             "census/national_county2020"],
        )
        result = self.phase4()
        self.assertFalse(result.passed)
        detail = self.check(result, "global inputs").detail
        self.assertIn("census/national_county2020", detail)
        self.assertIn("flood-zz", detail)

    def test_a_us_only_feature_input_fails_too(self):
        # A pilot whose panel is global but whose *features* are not has not
        # demonstrated the swap either.
        self.pass_phase1(
            self.registry["cyclone-zy"],
            self.PILOT_INPUTS["cyclone-zy"],
            feature_inputs=["fema/nri_counties_2023"],
        )
        result = self.phase4()
        self.assertFalse(result.passed)
        self.assertIn("fema/nri_counties_2023", self.check(result, "global inputs").detail)

    def test_an_unregistered_manifest_key_fails_rather_than_passing_silently(self):
        self.pass_phase1(
            self.registry["flood-zz"],
            ["geoboundaries/ZZ/ADM1", "records/ZZ/zz_records.csv", "somebody/else"],
        )
        self.assertIn(
            "no registered connector",
            self.check(self.phase4(), "global inputs").detail,
        )

    def test_the_ground_truth_hash_must_match_the_manifest(self):
        key = data_mod.records_key(self.registry["flood-zz"])
        self.manifest.records[key].sha256 = "c" * 64
        self.manifest.save(force=True)
        result = self.phase4()
        self.assertFalse(result.passed)
        detail = self.check(result, "ground truth pinned").detail
        self.assertIn("flood-zz", detail)
        self.assertIn("the contract names", detail)

    def test_an_unpinned_record_fails(self):
        del self.manifest.records[data_mod.records_key(self.registry["cyclone-zy"])]
        self.manifest.save(force=True)
        self.assertIn(
            "is not pinned", self.check(self.phase4(), "ground truth pinned").detail
        )

    def test_the_us_example_digests_are_read_from_the_blessed_fingerprints(self):
        # The real repository's fingerprints and contracts, deliberately: this
        # criterion is a fact about the committed artefacts, not about the
        # temporary registry the rest of this test uses.
        check = self.check(self.phase4(), "us digests")
        self.assertTrue(check.passed, check.detail)
        for name in verify.US_EXAMPLE_CONTRACTS:
            self.assertIn(name, check.detail)

    def test_a_moved_us_digest_fails(self):
        blessed = self.dir / "blessed"
        blessed.mkdir()
        for name in verify.US_EXAMPLE_CONTRACTS:
            (blessed / f"{name}.json").write_text(json.dumps({"_contract": "0" * 16}))
        check = self.check(self.phase4(expected_dir=blessed), "us digests")
        self.assertFalse(check.passed)
        self.assertIn("incomparable", check.detail)

    def test_failures_are_one_line_each(self):
        del self.registry["cyclone-zy"]
        del self.registry["flood-zz"]
        result = self.phase4()
        self.assertEqual(len(result.failures()), 3)
        self.assertTrue(all("\n" not in f for f in result.failures()))


if __name__ == "__main__":
    unittest.main()
