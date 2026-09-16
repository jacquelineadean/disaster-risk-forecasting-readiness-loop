"""readiness/fleet.py: the loop over many contracts, and the status read back.

No network. Every dataset is injected through `run_fleet(dataset_for=...)`:
a planted-signal dataset where a candidate has to pass so that a promotion
and a Phase 1 pass can be observed, a source-less one where nothing can pass,
and a callable that raises where the fleet has to keep going. The Phase 2
queue runs with fewer linear iterations than the registry default; the
properties under test (one ledger and one budget per contract, promotion
only where earned, the error map, the status table) do not depend on them.
"""

import dataclasses
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from readiness import backtest, data as data_mod, fleet
from readiness.agent import orchestrator
from readiness.harness.ledger import Ledger
from tests.fixtures import make_contract
from tests.test_orchestrator import signal_dataset, synthetic_dataset

#: The Phase 2 queue with the linear learners cut to 60 iterations. The GBM
#: budget is left as the queue declares it: 60 rounds over 16 bins is fast.
QUICK_PHASE2 = [
    dataclasses.replace(c, kwargs={**c.kwargs, "iters": 60})
    if c.model.startswith("logistic") else c
    for c in orchestrator.PHASE2_QUEUE
]


class FleetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.experiments = self.dir / "experiments"
        self.flood = make_contract(name="flood-us", scope={"states": []})
        self.tornado = make_contract(name="tornado-us", hazard="tornado",
                                     scope={"states": []})
        self.lines: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def where(self, contract):
        return data_mod.paths(contract, experiments_dir=self.experiments)

    def run_fleet(self, contracts, dataset_for, **kw):
        return fleet.run_fleet(
            contracts,
            queue=QUICK_PHASE2,
            promote=True,
            experiments_dir=self.experiments,
            progress=self.lines.append,
            dataset_for=dataset_for,
            **kw,
        )


class TestRunFleet(FleetCase):
    def test_one_ledger_and_budget_per_contract(self):
        results = self.run_fleet(
            [self.flood, self.tornado], lambda c: signal_dataset(c, self.dir)
        )
        self.assertEqual(list(results), ["flood-us", "tornado-us"])
        for contract in (self.flood, self.tornado):
            with self.subTest(contract=contract.name):
                result = results[contract.name]
                self.assertIsInstance(result, orchestrator.LoopResult)
                self.assertIsNotNone(result.promoted)
                where = self.where(contract)
                self.assertEqual(where.ledger, self.experiments / contract.name
                                 / "ledger.jsonl")
                ledger = Ledger(where.ledger)
                self.assertTrue(ledger.verify().valid)
                cards = list(ledger.read())
                self.assertEqual(len(cards), len(QUICK_PHASE2) + 2)  # canary, test
                self.assertTrue(all(c.contract_digest == contract.digest()
                                    for c in cards))
                self.assertEqual(cards[-1].split, "test")
                self.assertTrue(where.touch_budget.exists())
        # Two digests, two ledgers, two budgets: nothing shared between them.
        self.assertNotEqual(self.flood.digest(), self.tornado.digest())
        self.assertEqual(
            len({p.parent for p in self.experiments.glob("*/test_touches.json")}), 2
        )
        # The fleet's own progress lines, then the loop's, indented under them.
        self.assertIn("fleet        [flood-us] 1/2", self.lines)
        self.assertIn("fleet        [tornado-us] 2/2", self.lines)
        self.assertIn("  gather context", self.lines)

    def test_promotes_only_where_a_validate_pass_exists(self):
        datasets = {
            "flood-us": signal_dataset(self.flood, self.dir),
            "tornado-us": synthetic_dataset(self.tornado, self.dir),  # no sources
        }
        results = self.run_fleet([self.flood, self.tornado], lambda c: datasets[c.name])
        self.assertIsNotNone(results["flood-us"].promoted)
        self.assertEqual(results["flood-us"].promoted.split, "test")
        self.assertTrue(self.where(self.flood).touch_budget.exists())
        self.assertIsNone(results["tornado-us"].promoted)
        self.assertEqual(results["tornado-us"].passed, [])
        self.assertTrue(len(results["tornado-us"].skipped) >= 4)
        self.assertFalse(self.where(self.tornado).touch_budget.exists())
        self.assertTrue(self.where(self.tornado).ledger.exists())

    def test_every_card_records_wall_clock_seconds(self):
        self.run_fleet([self.flood], lambda c: signal_dataset(c, self.dir))
        for card in Ledger(self.where(self.flood).ledger).read():
            with self.subTest(card=card.experiment_id):
                seconds = card.data_snapshot["wall_clock_s"]
                self.assertIsInstance(seconds, float)
                self.assertGreaterEqual(seconds, 0.0)
                self.assertEqual(seconds, round(seconds, 3))

    def test_continues_past_a_contract_whose_dataset_raises(self):
        hail = make_contract(name="hail-us", hazard="hail", scope={"states": []})

        def dataset_for(contract):
            if contract.name == "tornado-us":
                raise FileNotFoundError("storm_events/all_2001.jsonl is not pinned")
            return synthetic_dataset(contract, self.dir)

        results = self.run_fleet([self.flood, self.tornado, hail], dataset_for)
        self.assertEqual(list(results), ["flood-us", "tornado-us", "hail-us"])
        self.assertIsInstance(results["tornado-us"], FileNotFoundError)
        self.assertIsInstance(results["flood-us"], orchestrator.LoopResult)
        self.assertIsInstance(results["hail-us"], orchestrator.LoopResult)
        self.assertTrue(self.where(self.flood).ledger.exists())
        self.assertFalse(self.where(self.tornado).ledger.exists())
        self.assertTrue(self.where(hail).ledger.exists())
        self.assertIn(
            "fleet        [tornado-us] not run: storm_events/all_2001.jsonl is not "
            "pinned", self.lines,
        )
        text = fleet.format_results(results)
        self.assertIn("tornado-us: not run (FileNotFoundError:", text)
        self.assertIn("ran 1 experiment(s) against hail-us", text)

    def test_queue_by_name_and_unknown_names_refused(self):
        results = fleet.run_fleet(
            [self.flood], queue="baseline", experiments_dir=self.experiments,
            progress=self.lines.append,
            dataset_for=lambda c: synthetic_dataset(c, self.dir),
        )
        self.assertEqual(
            [c.model for c in results["flood-us"].cards],
            [c.model for c in orchestrator.BASELINE_QUEUE] + ["leaky-oracle"],
        )
        with self.assertRaises(ValueError) as ctx:
            fleet.run_fleet([self.flood], queue="phase9",
                            experiments_dir=self.experiments,
                            dataset_for=lambda c: synthetic_dataset(c, self.dir))
        self.assertIn("phase9", str(ctx.exception))
        self.assertIn("phase2", str(ctx.exception))

    def test_features_may_be_a_rule_of_the_contract(self):
        seen = []

        def build(contract, **kw):
            seen.append((contract.name, list(kw["features"])))
            return synthetic_dataset(contract, self.dir)

        with mock.patch.object(data_mod, "build", build):
            fleet.run_fleet(
                [self.flood, self.tornado], queue="baseline",
                features=lambda c: ["era5"] if c.hazard == "tornado" else [],
                experiments_dir=self.experiments, progress=self.lines.append,
            )
            fleet.run_fleet(
                [self.flood], queue="baseline", features=["terrain"],
                experiments_dir=self.experiments, progress=self.lines.append,
            )
        self.assertEqual(seen, [("flood-us", []), ("tornado-us", ["era5"]),
                               ("flood-us", ["terrain"])])


class TestStatus(FleetCase):
    def setUp(self):
        super().setUp()
        datasets = {
            "flood-us": signal_dataset(self.flood, self.dir),
            "tornado-us": synthetic_dataset(self.tornado, self.dir),
        }
        self.results = self.run_fleet(
            [self.flood, self.tornado], lambda c: datasets[c.name]
        )
        self.registry = {"flood-us": self.flood, "tornado-us": self.tornado}

    def write_backtest(self, contract):
        # `backtest.write` reads the experiments tree the CLI would: the
        # environment override, not an argument.
        with mock.patch.dict(
            os.environ, {data_mod.EXPERIMENTS_DIR_ENV: str(self.experiments)}
        ):
            return backtest.write(contract)

    def test_status_reads_phase_results(self):
        self.write_backtest(self.flood)
        rows = {r.name: r for r in fleet.status(self.registry, self.experiments)}
        self.assertEqual(list(rows), ["flood-us", "tornado-us"])

        ok = rows["flood-us"]
        promoted = self.results["flood-us"].promoted
        self.assertTrue(ok.phase1_ok, ok.phase1_detail)
        self.assertEqual(ok.phase1_detail, "Phase 1 exit criteria met")
        self.assertEqual(ok.test_card, promoted.experiment_id)
        self.assertEqual(ok.promoted_model, f"{promoted.model}@{promoted.version}")
        self.assertEqual(ok.test_bss, promoted.scorecard["brier_skill_score"])
        self.assertIn(ok.promoted_model, ok.validate_passes)
        self.assertEqual(ok.n_cards, len(QUICK_PHASE2) + 2)
        self.assertEqual((ok.hazard, ok.scope_key, ok.period),
                         ("inland_flood", "US:all", "quarter"))

        no = rows["tornado-us"]
        self.assertFalse(no.phase1_ok)
        self.assertTrue(no.phase1_detail.startswith("test card: none under contract"))
        self.assertIsNone(no.test_card)
        self.assertIsNone(no.promoted_model)
        self.assertIsNone(no.test_bss)
        self.assertEqual(no.validate_passes, [])
        self.assertEqual(no.hazard, "tornado")

    def test_status_is_ledger_only(self):
        self.write_backtest(self.flood)
        with mock.patch.object(data_mod, "build", side_effect=AssertionError("built")):
            rows = fleet.status(self.registry, self.experiments)
        self.assertTrue(rows[0].phase1_ok)

    def test_a_missing_backtest_is_the_one_failure_left(self):
        row = fleet.status({"flood-us": self.flood}, self.experiments)[0]
        self.assertFalse(row.phase1_ok)
        self.assertTrue(row.phase1_detail.startswith("published: no backtest report"))
        self.assertIsNotNone(row.test_card)

    def test_status_of_an_empty_experiments_tree(self):
        empty = self.dir / "nowhere"
        rows = fleet.status(self.registry, empty)
        self.assertEqual([r.n_cards for r in rows], [0, 0])
        self.assertFalse(any(r.phase1_ok for r in rows))

    def test_format_status_renders_every_row(self):
        self.write_backtest(self.flood)
        rows = fleet.status(self.registry, self.experiments)
        text = fleet.format_status(rows)
        lines = text.splitlines()
        self.assertEqual(len(lines), 3)
        for needle in ("contract", "hazard", "scope", "period", "cards",
                       "validate pass", "test card", "test BSS", "phase 1"):
            self.assertIn(needle, lines[0])
        self.assertIn("flood-us", lines[1])
        self.assertIn(rows[0].test_card, lines[1])
        self.assertIn(f"{rows[0].test_bss:+.4f}", lines[1])
        self.assertTrue(lines[1].rstrip().endswith("ok"))
        self.assertIn("tornado-us", lines[2])
        self.assertIn("NOT met: test card: none", lines[2])
        self.assertEqual(fleet.format_status([]), "no contracts registered")


class TestNational(unittest.TestCase):
    def test_national_keeps_only_country_wide_contracts(self):
        registry = {
            "flood-us": make_contract(name="flood-us", scope={"states": []}),
            "flood-zz": make_contract(name="flood-zz"),
        }
        self.assertEqual(list(fleet.national(registry)), ["flood-us"])


if __name__ == "__main__":
    unittest.main()
