"""The local loop end to end, on a synthetic dataset, for more than one contract.

No network: the dataset is injected. What is under test is that the loop writes
one sealed card per candidate into the *contract's own* ledger, that the canary
card is rejected, and that none of it depends on which hazard the contract names.
"""

import pathlib
import tempfile
import unittest

from readiness import data as data_mod
from readiness.agent import orchestrator
from readiness.connectors.base import Manifest
from readiness.harness.labels import diagnose
from readiness.harness.ledger import Ledger
from tests.fixtures import make_contract, make_panel


def synthetic_dataset(contract, tmp: pathlib.Path) -> data_mod.Dataset:
    panel = make_panel(contract=contract, n_regions=10)
    manifest = Manifest(path=tmp / "manifest.json")
    return data_mod.Dataset(
        contract=contract,
        panel=panel,
        regions=(),
        data_version="synthetic",
        manifest=manifest,
        diagnostics=diagnose([], panel.regions, panel.years, contract),
    )


class TestLocalLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_loop(self, contract):
        return orchestrator.run_local(
            contract,
            experiments_dir=self.dir,
            dataset=synthetic_dataset(contract, self.dir),
            progress=lambda _m: None,
        )

    def test_writes_one_card_per_candidate_into_the_contracts_ledger(self):
        c = make_contract(name="flood-zz")
        result = self.run_loop(c)
        self.assertEqual(len(result.cards), len(orchestrator.BASELINE_QUEUE) + 1)
        where = data_mod.paths(c, experiments_dir=self.dir)
        self.assertEqual(where.ledger, self.dir / "flood-zz" / "ledger.jsonl")
        ledger = Ledger(where.ledger)
        self.assertTrue(ledger.verify().valid)
        self.assertEqual(len(ledger), len(result.cards))

    def test_reference_scores_zero_and_the_canary_is_rejected(self):
        c = make_contract(name="flood-zz")
        result = self.run_loop(c)
        by_model = {card.model: card for card in result.cards}
        self.assertEqual(by_model["climatology-pooled"].scorecard["brier_skill_score"], 0.0)
        self.assertEqual(by_model["climatology-pooled"].scorecard["auc"], 0.5)
        self.assertTrue(by_model["leaky-oracle"].canary["rejected"])
        self.assertEqual(result.rejected, ["leaky-oracle"])
        self.assertIn("climatology-pooled", result.failed)

    def test_cards_carry_the_contract(self):
        c = make_contract(name="flood-zz")
        result = self.run_loop(c)
        for card in result.cards:
            self.assertEqual(card.contract_digest, c.digest())
            self.assertEqual(card.data_snapshot["contract"], "flood-zz")
            self.assertEqual(card.data_snapshot["hazard"], "inland_flood")
            self.assertEqual(card.data_snapshot["period"], "quarter")
            self.assertEqual(card.verdict["contract"], "flood-zz")

    def test_two_contracts_keep_separate_ledgers(self):
        a = make_contract(name="flood-zz")
        b = make_contract(name="heat-yy", hazard="heat", period="month",
                          scope={"states": ["YY"]})
        self.run_loop(a)
        self.run_loop(b)
        la = Ledger(data_mod.paths(a, experiments_dir=self.dir).ledger)
        lb = Ledger(data_mod.paths(b, experiments_dir=self.dir).ledger)
        self.assertTrue(la.verify().valid and lb.verify().valid)
        self.assertEqual([c.data_snapshot["hazard"] for c in la.read()],
                         ["inland_flood"] * 4)
        self.assertEqual([c.data_snapshot["hazard"] for c in lb.read()], ["heat"] * 4)
        self.assertNotEqual(next(la.read()).contract_digest, next(lb.read()).contract_digest)

    def test_the_loop_is_hazard_agnostic(self):
        for hazard, period in (("tornado", "quarter"), ("wildfire", "year"), ("heat", "month")):
            with self.subTest(hazard=hazard):
                c = make_contract(name=f"{hazard.replace('_', '-')}-zz", hazard=hazard,
                                  period=period)
                result = self.run_loop(c)
                self.assertEqual(result.rejected, ["leaky-oracle"])
                self.assertTrue(
                    Ledger(data_mod.paths(c, experiments_dir=self.dir).ledger).verify().valid
                )

    def test_ledger_summary_reads_back(self):
        c = make_contract(name="flood-zz")
        self.run_loop(c)
        summary = Ledger(data_mod.paths(c, experiments_dir=self.dir).ledger).summary()
        self.assertIn("REJECTED", summary)
        self.assertIn("chain intact", summary)

    def test_system_prompt_names_the_contract(self):
        c = make_contract(name="flood-zz")
        text = orchestrator.system_prompt(c)
        self.assertIn("flood-zz", text)
        self.assertIn(c.digest(), text)
        self.assertIn("contracts/", text)


if __name__ == "__main__":
    unittest.main()
