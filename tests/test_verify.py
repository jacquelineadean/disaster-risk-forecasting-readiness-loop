"""Phase 0 verification as a library: the fingerprint, and the four checks.

No network: the dataset is synthetic and every path is a temporary one, so the
blessed files under `harness_expected/` and the committed ledgers are never
read or written here. The CLI's aliases are checked to be the same objects,
because `harness_expected/*.json` was written through them.
"""

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from readiness import cli, data as data_mod, verify
from readiness.agent import orchestrator
from readiness.engine import build_model
from readiness.harness import scoring
from readiness.harness.ledger import Ledger
from tests.fixtures import make_contract
from tests.test_orchestrator import synthetic_dataset


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


if __name__ == "__main__":
    unittest.main()
