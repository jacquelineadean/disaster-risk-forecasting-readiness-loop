"""The local loop end to end, on a synthetic dataset, for more than one contract.

No network: the dataset is injected. What is under test is that the loop writes
one sealed card per candidate into the *contract's own* ledger, that the canary
card is rejected, and that none of it depends on which hazard the contract names.

The Phase 1 tests hand the dataset feature sources built by hand: the fakes
from `tests.test_features` for the skip rule, and the planted-signal series
from `tests.test_engine_models` where a candidate has to pass so that the
promotion can be tested.
"""

import dataclasses
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
from tests.fixtures import make_contract, make_panel, make_signal_panel
from tests.test_engine_models import FakeElevation, SignalSeries
from tests.test_features import FakeSeries, FakeStatic


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


def feature_dataset(
    contract, tmp: pathlib.Path, sources: dict, panel=None
) -> data_mod.Dataset:
    """A synthetic dataset with feature sources, as `data.build(features=...)` makes."""
    panel = panel or make_panel(contract=contract, n_regions=8)
    keys = tuple(sorted({k for s in sources.values() for k in s.manifest_keys}))
    return data_mod.Dataset(
        contract=contract,
        panel=panel,
        regions=(),
        data_version="synthetic",
        manifest=Manifest(path=tmp / "manifest.json"),
        diagnostics=diagnose([], panel.regions, panel.years, contract),
        sources=dict(sources),
        feature_version="synthetic-features",
        feature_inputs=keys,
    )


def signal_dataset(contract, tmp: pathlib.Path, n_regions: int = 8) -> data_mod.Dataset:
    """A dataset with planted antecedent-precipitation signal, so something can pass."""
    series = SignalSeries()
    panel = make_signal_panel(contract, series, n_regions=n_regions)
    sources = {"era5": series, "gazetteer": FakeStatic(), "elevation": FakeElevation()}
    return feature_dataset(contract, tmp, sources, panel)


#: Fewer steps than the registry defaults, so the queue runs in a second; the
#: properties under test (order, skipping, promotion) do not depend on them.
QUICK = {"logistic": {"iters": 60}, "logistic+iso": {"iters": 60},
         "gbm": {"rounds": 40}, "gbm+iso": {"rounds": 40}}


def quick(queue) -> list:
    return [
        dataclasses.replace(c, kwargs={**c.kwargs, **QUICK.get(c.model, {})})
        for c in queue
    ]


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


class TestCandidate(unittest.TestCase):
    def test_requires_reads_the_kwargs_then_the_registry_default(self):
        default = orchestrator.Candidate("logistic", "c", "h")
        self.assertEqual(default.requires, ("era5-antecedent", "terrain"))
        history = orchestrator.Candidate("logistic", "c", "h", {"feature_sets": []})
        self.assertEqual(history.requires, ())
        pooled = orchestrator.Candidate("climatology-pooled", "c", "h")
        self.assertEqual(pooled.requires, ())
        self.assertEqual(orchestrator.CANARY_CANDIDATE.requires, ())

    def test_sources_follow_the_engine_catalogue(self):
        self.assertEqual(orchestrator.sources_for(["terrain"]),
                         ("elevation", "gazetteer"))
        self.assertEqual(orchestrator.sources_for(["era5-antecedent", "terrain"]),
                         ("elevation", "era5", "gazetteer"))
        self.assertEqual(orchestrator.sources_for([]), ())

    def test_labels_tell_the_three_logistic_entries_apart(self):
        labels = [c.label for c in orchestrator.PHASE1_QUEUE]
        self.assertEqual(len(set(labels)), len(labels))
        self.assertIn("logistic[history-only]", labels)
        self.assertIn("logistic[era5-antecedent+terrain]", labels)
        self.assertEqual(orchestrator.BASELINE_QUEUE[0].label, "climatology-pooled")

    def test_every_phase1_candidate_has_a_real_changed_and_hypothesis(self):
        for c in orchestrator.PHASE1_QUEUE:
            with self.subTest(candidate=c.label):
                self.assertGreater(len(c.changed), 80)
                self.assertGreater(len(c.hypothesis), 120)
                self.assertIn("Falsified", c.hypothesis)
        n_baseline = len(orchestrator.BASELINE_QUEUE)
        self.assertEqual(orchestrator.QUEUES["phase1"][:n_baseline],
                         orchestrator.BASELINE_QUEUE)


class TestPhase1Queue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.contract = make_contract(name="flood-zz")
        self.lines: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_loop(self, dataset, **kw):
        return orchestrator.run_local(
            self.contract,
            experiments_dir=self.dir,
            dataset=dataset,
            queue=quick(orchestrator.PHASE1_QUEUE),
            include_canary=False,
            progress=self.lines.append,
            **kw,
        )

    def test_runs_every_candidate_and_records_kwargs_and_the_audit(self):
        sources = {"era5": FakeSeries(), "gazetteer": FakeStatic(),
                   "elevation": FakeStatic("elevation")}
        result = self.run_loop(feature_dataset(self.contract, self.dir, sources))
        self.assertEqual(result.skipped, [])
        self.assertEqual(len(result.cards), len(orchestrator.PHASE1_QUEUE))
        for candidate, card in zip(quick(orchestrator.PHASE1_QUEUE), result.cards):
            with self.subTest(candidate=candidate.label):
                self.assertEqual(card.split, "validate")
                self.assertEqual(card.data_snapshot["model_kwargs"],
                                 orchestrator.json_kwargs(candidate.kwargs))
                self.assertEqual(card.data_snapshot["feature_version"],
                                 "synthetic-features")
                if candidate.requires:
                    self.assertTrue(card.scorecard["feature_columns"])
                    self.assertTrue(card.scorecard["feature_audit"]["clean"])
                    self.assertFalse(card.canary["rejected"], card.canary)
                else:
                    self.assertFalse(card.scorecard["feature_columns"])
                    self.assertIsNone(card.scorecard["feature_audit"])
        where = data_mod.paths(self.contract, experiments_dir=self.dir)
        self.assertTrue(Ledger(where.ledger).verify().valid)
        self.assertIn("logistic[era5-antecedent+terrain]", result.passed + result.failed)

    def test_candidates_needing_missing_sources_are_skipped_and_listed(self):
        dataset = feature_dataset(self.contract, self.dir, {"era5": FakeSeries()})
        result = self.run_loop(dataset)
        ran = [c.model for c in result.cards]
        self.assertEqual(ran, ["logistic", "logistic"])
        self.assertEqual(result.skipped, [
            "logistic[era5-antecedent+terrain]", "logistic+iso[era5-antecedent+terrain]",
            "gbm[era5-antecedent+terrain]", "gbm+iso[era5-antecedent+terrain]",
        ])
        skips = [line for line in self.lines if line.startswith("skip")]
        self.assertEqual(len(skips), 4)
        self.assertIn("['elevation', 'gazetteer']", skips[0])
        where = data_mod.paths(self.contract, experiments_dir=self.dir)
        self.assertEqual(len(Ledger(where.ledger)), 2)

    def test_without_any_sources_only_the_history_model_runs(self):
        result = self.run_loop(synthetic_dataset(self.contract, self.dir))
        self.assertEqual([c.model for c in result.cards], ["logistic"])
        kwargs = result.cards[0].data_snapshot["model_kwargs"]
        self.assertEqual(kwargs["feature_sets"], [])
        self.assertEqual(len(result.skipped), 5)
        self.assertNotIn("feature_version", result.cards[0].data_snapshot)

    def test_nothing_passing_promotes_nobody_and_spends_nothing(self):
        result = self.run_loop(synthetic_dataset(self.contract, self.dir), promote=True)
        self.assertIsNone(result.promoted)
        self.assertFalse(data_mod.paths(self.contract, experiments_dir=self.dir)
                         .touch_budget.exists())
        self.assertIn("promote      nothing to promote: no candidate passed on validate",
                      self.lines)


class TestPromote(unittest.TestCase):
    """The one atomic test touch, and every way it is refused."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.contract = make_contract(name="flood-zz")
        self.dataset = signal_dataset(self.contract, self.dir)
        self.where = data_mod.paths(self.contract, experiments_dir=self.dir)
        self.queue = quick(orchestrator.PHASE1_QUEUE)

    def tearDown(self):
        self.tmp.cleanup()

    def budget(self) -> TouchBudget:
        return TouchBudget(self.where.touch_budget, self.contract.test_touch_budget)

    def validate_run(self, **kw):
        return orchestrator.run_local(
            self.contract, experiments_dir=self.dir, dataset=self.dataset,
            queue=self.queue, include_canary=False, progress=lambda _m: None, **kw,
        )

    def promote(self, model, kwargs):
        return orchestrator.promote(
            self.contract, model, kwargs, self.dataset, experiments_dir=self.dir
        )

    def test_promote_spends_exactly_one_touch_on_first_validate_pass(self):
        result = self.validate_run(promote=True)
        first_pass = next(c for c in result.cards if c.status == "PASS")
        promoted = result.promoted
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted.split, "test")
        self.assertEqual((promoted.model, promoted.version),
                         (first_pass.model, first_pass.version))
        self.assertEqual(promoted.data_snapshot["model_kwargs"],
                         first_pass.data_snapshot["model_kwargs"])
        self.assertIn(first_pass.experiment_id, promoted.changed)
        self.assertEqual(self.budget().as_dict(),
                         {f"{promoted.model}@{promoted.version}": 1})
        ledger = Ledger(self.where.ledger)
        cards = list(ledger.read())
        self.assertEqual(len(cards), len(result.cards) + 1)
        self.assertEqual(cards[-1].split, "test")
        self.assertEqual(cards[-1].card_hash, promoted.card_hash)
        self.assertTrue(ledger.verify().valid)
        self.assertIn("promoted to test", result.format())

    def test_promote_refuses_without_validate_pass(self):
        with self.assertRaises(orchestrator.PromotionRefused) as ctx:
            self.promote("logistic+iso", self.queue[3].kwargs)
        self.assertIn("no validate card", str(ctx.exception))
        self.assertFalse(self.where.touch_budget.exists())
        self.assertFalse(self.where.ledger.exists())

    def test_promote_refuses_a_validate_failure(self):
        self.validate_run()
        failed = self.queue[0]  # history-only logistic fails on this panel
        with self.assertRaises(orchestrator.PromotionRefused):
            self.promote(failed.model, failed.kwargs)
        self.assertFalse(self.where.touch_budget.exists())

    def test_promote_refuses_on_a_kwargs_mismatch(self):
        self.validate_run()
        passing = self.queue[3]
        with self.assertRaises(orchestrator.PromotionRefused) as ctx:
            self.promote(passing.model, {**passing.kwargs, "iters": 61})
        self.assertIn('"iters": 61', str(ctx.exception))
        self.assertFalse(self.where.touch_budget.exists())
        # The exact arguments, tuples or lists alike, are accepted.
        sets = tuple(passing.kwargs["feature_sets"])
        as_tuple = {**passing.kwargs, "feature_sets": sets}
        card = self.promote(passing.model, as_tuple)
        self.assertEqual(card.split, "test")

    def test_promote_refuses_when_a_test_card_already_exists(self):
        result = self.validate_run(promote=True)
        self.assertIsNotNone(result.promoted)
        n = len(Ledger(self.where.ledger))
        passing = self.queue[3]
        with self.assertRaises(orchestrator.PromotionRefused) as ctx:
            self.promote(passing.model, passing.kwargs)
        self.assertIn("already holds a test card", str(ctx.exception))
        # Not for another candidate either: one test card per contract version.
        gbm = self.queue[4]
        with self.assertRaises(orchestrator.PromotionRefused):
            self.promote(gbm.model, gbm.kwargs)
        self.assertEqual(len(Ledger(self.where.ledger)), n)
        promoted = result.promoted
        self.assertEqual(self.budget().count(promoted.model, promoted.version), 1)

    def test_promote_only_looks_at_cards_under_the_current_contract(self):
        self.validate_run()
        passing = self.queue[3]
        other = make_contract(name="flood-zz", thresholds={"min_auc": 0.71})
        self.assertNotEqual(other.digest(), self.contract.digest())
        with self.assertRaises(orchestrator.PromotionRefused):
            orchestrator.promote(
                other, passing.model, passing.kwargs, signal_dataset(other, self.dir),
                experiments_dir=self.dir,
            )


if __name__ == "__main__":
    unittest.main()
