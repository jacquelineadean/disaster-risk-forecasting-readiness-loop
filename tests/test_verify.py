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
import pathlib
import tempfile
import unittest
from unittest import mock

from readiness import backtest, cli, data as data_mod, verify
from readiness.agent import orchestrator
from readiness.engine import build_model
from readiness.harness import contract as contract_mod
from readiness.harness import scoring
from readiness.harness.ledger import ExperimentCard, Ledger, utc_now
from readiness.harness.splits import TouchBudget
from tests.fixtures import make_contract
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
    dev=0.01, columns=("precip_3m",), audit=CLEAN_AUDIT,
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
        "reliability_bins": bins, "panel_digest": "p" * 16, "train_digest": "t" * 16,
        "feature_digest": "f" * 16 if columns else "", "feature_columns": list(columns),
        "feature_audit": audit if columns else None,
    }


def append_card(
    ledger: Ledger, contract, split, *, kwargs=None, verdict_passed=None,
    canary=CLEAR_CANARY, **scorecard_kw,
) -> ExperimentCard:
    """Seal one card into `ledger`; `verdict_passed` overrides the honest verdict."""
    sc = synthetic_scorecard(contract, split, **scorecard_kw)
    card = ExperimentCard(
        experiment_id=ledger.next_id(), timestamp=utc_now(), model=sc["model"],
        version=sc["version"], split=split, changed="synthetic", hypothesis="synthetic",
        outcome="synthetic", scorecard=sc, canary=canary,
        contract_digest=contract.digest(),
        data_snapshot={"model_kwargs": kwargs if kwargs is not None else {"iters": 60}},
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


if __name__ == "__main__":
    unittest.main()
