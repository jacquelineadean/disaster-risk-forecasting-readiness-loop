"""Split isolation and the test-touch budget.

These tests encode the report's separation-of-powers argument (§4): the agent
may propose any model, but it cannot reach the holdout years or move the
threshold. The years themselves come from the contract; the enforcement lives
here.
"""

import json
import pathlib
import tempfile
import unittest

from readiness.harness.splits import (
    SplitViolation,
    TouchBudget,
    TrainingView,
    coverage_report,
    get_split,
    split_panel,
)
from tests.fixtures import make_contract, make_panel


class TestSplitDefinitions(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.s = self.c.splits

    def test_splits_are_ordered_in_time(self):
        # Training on the future to predict the past is the classic leak.
        self.assertLess(max(self.s.train.years), min(self.s.validate.years))
        self.assertLess(max(self.s.validate.years), min(self.s.test.years))

    def test_training_window_is_twenty_years_by_default(self):
        # The contract names a "20-year climatological baseline".
        self.assertEqual(len(self.s.train.years), 20)

    def test_unknown_split_raises(self):
        with self.assertRaises(SplitViolation):
            get_split(self.c, "holdout")

    def test_splits_follow_the_contract(self):
        c = make_contract(
            splits={"train": [2000, 2009], "validate": [2010, 2012], "test": [2013, 2015]}
        )
        self.assertEqual(c.splits.train.years, tuple(range(2000, 2010)))
        self.assertEqual(str(c.splits.test), "test (2013-2015)")


class TestTrainingView(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.panel = make_panel(contract=self.c, n_regions=6)
        self.train = self.c.splits.train

    def test_view_over_training_years_is_allowed(self):
        view = TrainingView(split_panel(self.panel, self.train), self.train)
        self.assertEqual(len(view), len(split_panel(self.panel, self.train)))

    def test_view_refuses_a_panel_containing_holdout_years(self):
        with self.assertRaises(SplitViolation) as ctx:
            TrainingView(self.panel, self.train)  # full panel: includes validate + test
        self.assertIn("out-of-split years", str(ctx.exception))

    def test_view_refuses_validate_years_in_a_train_view(self):
        mixed = self.panel.filter_years(self.train.years[-2:] + self.c.splits.validate.years[:1])
        with self.assertRaises(SplitViolation):
            TrainingView(mixed, self.train)

    def test_digest_identifies_the_exposed_data(self):
        train = split_panel(self.panel, self.train)
        self.assertEqual(TrainingView(train, self.train).digest, train.digest())

    def test_view_records_that_it_was_read(self):
        view = TrainingView(split_panel(self.panel, self.train), self.train)
        self.assertFalse(view.accessed)
        view.rows()
        self.assertTrue(view.accessed)


class TestSplitPanel(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()

    def test_slices_to_the_requested_years(self):
        panel = make_panel(contract=self.c, n_regions=4)
        for split in self.c.splits:
            with self.subTest(split=split.name):
                sliced = split_panel(panel, split)
                self.assertEqual(set(sliced.years), set(split.years))

    def test_empty_split_raises_rather_than_scoring_nothing(self):
        panel = make_panel(contract=self.c, n_regions=2, years=self.c.splits.train.years)
        with self.assertRaises(SplitViolation):
            split_panel(panel, self.c.splits.test)

    def test_coverage_report_flags_missing_years(self):
        panel = make_panel(contract=self.c, n_regions=2, years=self.c.splits.train.years)
        report = coverage_report(panel, self.c.splits)
        self.assertIn("MISSING", report)


class TestTouchBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "test_touches.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_touch_is_allowed(self):
        budget = TouchBudget(self.path, 1)
        budget.check("m", "1.0.0")
        self.assertEqual(budget.spend("m", "1.0.0"), 1)

    def test_second_touch_is_refused(self):
        budget = TouchBudget(self.path, 1)
        budget.spend("m", "1.0.0")
        with self.assertRaises(SplitViolation) as ctx:
            budget.check("m", "1.0.0")
        self.assertIn("already been scored", str(ctx.exception))

    def test_budget_survives_a_restart(self):
        TouchBudget(self.path, 1).spend("m", "1.0.0")
        reloaded = TouchBudget(self.path, 1)  # fresh object, same file
        self.assertEqual(reloaded.count("m", "1.0.0"), 1)
        with self.assertRaises(SplitViolation):
            reloaded.check("m", "1.0.0")

    def test_a_new_version_gets_a_fresh_budget(self):
        budget = TouchBudget(self.path, 1)
        budget.spend("m", "1.0.0")
        budget.check("m", "1.1.0")  # different version, not yet spent

    def test_budget_size_comes_from_the_contract(self):
        c = make_contract(test_touch_budget=2)
        budget = TouchBudget(self.path, c.test_touch_budget)
        budget.spend("m", "1.0.0")
        budget.spend("m", "1.0.0")
        with self.assertRaises(SplitViolation):
            budget.check("m", "1.0.0")

    def test_file_is_human_readable(self):
        TouchBudget(self.path, 1).spend("m", "1.0.0")
        self.assertEqual(json.loads(self.path.read_text()), {"m@1.0.0": 1})


if __name__ == "__main__":
    unittest.main()
