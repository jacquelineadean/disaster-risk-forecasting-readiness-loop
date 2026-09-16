"""The local loop end to end, on a synthetic dataset, for more than one contract.

No network: the dataset is injected. What is under test is that the loop writes
one sealed card per candidate into the *contract's own* ledger, that the canary
card is rejected, and that none of it depends on which hazard the contract names.
"""

import pathlib
import tempfile
import unittest

from unittest import mock

from readiness import data as data_mod
from readiness.agent import guard, orchestrator, subagents
from readiness.harness import scoring
from readiness.connectors.base import Manifest
from readiness.harness.labels import diagnose
from readiness.harness.ledger import Ledger
from readiness.harness.splits import SplitViolation, TouchBudget, get_split
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

    def test_system_prompt_forbids_every_guarded_path_and_names_the_guard(self):
        text = orchestrator.system_prompt(make_contract(name="flood-zz"))
        for path in ("readiness/connectors/", "readiness/verify.py",
                     "readiness/agent/guard.py", "readiness/harness/", "features"):
            self.assertIn(path, text)
        self.assertIn("guard hashes", text)

    def test_prompts_speak_the_contracts_vocabulary(self):
        c = make_contract(name="heat-yy", hazard="heat", period="month",
                          scope={"states": ["YY"]})
        text = orchestrator.system_prompt(c)
        for line in c.describe().splitlines():
            self.assertIn(line.strip(), text)
        agents = subagents.subagents_for(c)
        analyst = agents["hazard-analyst-heat"]["prompt"]
        self.assertIn("month", analyst)
        self.assertIn(c.scope_label, analyst)
        steward = agents["data-steward"]["prompt"]
        self.assertIn("heat", steward)
        self.assertIn("snapshots/manifest.json", steward)
        # The system prompt quotes describe(), whose zone line may say "county";
        # the hand-written parts of the subagent prompts must not.
        for prompt in (text, analyst, steward):
            self.assertNotIn("Storm Events", prompt)
        for prompt in (analyst, steward):
            self.assertNotIn("county", prompt.lower())
            self.assertNotIn("fips", prompt.lower())

    def test_no_subagent_holds_a_write_tool(self):
        for name, spec in subagents.subagents_for(make_contract()).items():
            with self.subTest(agent=name):
                self.assertFalse({"Write", "Edit"} & set(spec["tools"]))


class TestTestSplit(unittest.TestCase):
    """The one path `run_local` never takes: scoring a candidate on test.

    The touch budget lives on disk, so a second run of the same model version
    must be refused before anything is scored, and a run with no budget at all
    must not be able to score test by accident.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.contract = make_contract(name="flood-zz")
        self.dataset = synthetic_dataset(self.contract, self.dir)
        self.where = data_mod.paths(self.contract, experiments_dir=self.dir)
        self.split = get_split(self.contract, "test")
        self.candidate = orchestrator.BASELINE_QUEUE[1]

    def tearDown(self):
        self.tmp.cleanup()

    def budget(self) -> TouchBudget:
        return TouchBudget(self.where.touch_budget, self.contract.test_touch_budget)

    def score(self, budget):
        return orchestrator.run_experiment(
            self.candidate, self.dataset, self.split, Ledger(self.where.ledger),
            touch_budget=budget,
        )

    def test_scoring_on_test_spends_exactly_one_touch_and_writes_a_test_card(self):
        card = self.score(self.budget())
        self.assertEqual(card.split, "test")
        self.assertEqual(card.model, "climatology-seasonal")
        on_disk = self.budget()
        self.assertEqual(on_disk.count(card.model, card.version), 1)
        self.assertEqual(on_disk.as_dict(), {f"{card.model}@{card.version}": 1})
        ledger = Ledger(self.where.ledger)
        self.assertEqual(len(ledger), 1)
        self.assertTrue(ledger.verify().valid)

    def test_a_second_touch_of_the_same_version_is_refused_before_scoring(self):
        self.score(self.budget())
        with self.assertRaises(SplitViolation):
            self.score(self.budget())
        self.assertEqual(len(Ledger(self.where.ledger)), 1)
        self.assertEqual(self.budget().count("climatology-seasonal", "1.0.0"), 1)

    def test_test_split_without_a_budget_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.score(None)
        self.assertFalse(self.where.ledger.exists())
        self.assertFalse(self.where.touch_budget.exists())

    def test_the_touch_is_paid_before_the_model_sees_a_test_unit(self):
        """An interrupted test run cannot be re-run for free."""
        with mock.patch.object(scoring, "screen", side_effect=RuntimeError("cut")):
            with self.assertRaises(RuntimeError):
                self.score(self.budget())
        self.assertEqual(self.budget().count("climatology-seasonal", "1.0.0"), 1)
        self.assertFalse(self.where.ledger.exists())
        with self.assertRaises(SplitViolation):
            self.score(self.budget())

    def test_every_card_carries_the_harness_digest_it_was_scored_under(self):
        card = self.score(self.budget())
        self.assertEqual(card.data_snapshot["harness_digest"], guard.tree_digest())


if __name__ == "__main__":
    unittest.main()
