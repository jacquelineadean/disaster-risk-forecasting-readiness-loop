"""Split isolation and the test-touch budget.

These tests encode the report's separation-of-powers argument (§4): the agent
may propose any model, but it cannot reach the holdout years or move the
threshold.
"""

import json
import pathlib
import tempfile
import unittest

from readiness.config import CONTRACT
from readiness.harness.splits import (
    TEST,
    TRAIN,
    VALIDATE,
    SplitViolation,
    TouchBudget,
    TrainingView,
    assert_disjoint,
    coverage_report,
    get_split,
    split_panel,
)
from tests.fixtures import make_panel


class TestSplitDefinitions(unittest.TestCase):
    def test_splits_are_disjoint(self):
        assert_disjoint()  # raises if not

    def test_splits_are_ordered_in_time(self):
        # Training on the future to predict the past is the classic leak.
        self.assertLess(max(TRAIN.years), min(VALIDATE.years))
        self.assertLess(max(VALIDATE.years), min(TEST.years))

    def test_training_window_is_twenty_years(self):
        # The contract names a "20-year climatological baseline".
        self.assertEqual(len(TRAIN.years), 20)

    def test_unknown_split_raises(self):
        with self.assertRaises(SplitViolation):
            get_split("holdout")


class TestTrainingView(unittest.TestCase):
    def setUp(self):
        self.panel = make_panel(n_counties=6)

    def test_view_over_training_years_is_allowed(self):
        view = TrainingView(split_panel(self.panel, TRAIN), TRAIN)
        self.assertEqual(len(view), len(split_panel(self.panel, TRAIN)))

    def test_view_refuses_a_panel_containing_holdout_years(self):
        with self.assertRaises(SplitViolation) as ctx:
            TrainingView(self.panel, TRAIN)  # full panel: includes validate + test
        self.assertIn("out-of-split years", str(ctx.exception))

    def test_view_refuses_validate_years_in_a_train_view(self):
        mixed = self.panel.filter_years(TRAIN.years[-2:] + VALIDATE.years[:1])
        with self.assertRaises(SplitViolation):
            TrainingView(mixed, TRAIN)

    def test_digest_identifies_the_exposed_data(self):
        train = split_panel(self.panel, TRAIN)
        self.assertEqual(TrainingView(train, TRAIN).digest, train.digest())

    def test_view_records_that_it_was_read(self):
        view = TrainingView(split_panel(self.panel, TRAIN), TRAIN)
        self.assertFalse(view.accessed)
        view.rows()
        self.assertTrue(view.accessed)


class TestSplitPanel(unittest.TestCase):
    def test_slices_to_the_requested_years(self):
        panel = make_panel(n_counties=4)
        for split in (TRAIN, VALIDATE, TEST):
            with self.subTest(split=split.name):
                sliced = split_panel(panel, split)
                self.assertEqual(set(sliced.years), set(split.years))

    def test_empty_split_raises_rather_than_scoring_nothing(self):
        panel = make_panel(n_counties=2, years=TRAIN.years)
        with self.assertRaises(SplitViolation):
            split_panel(panel, TEST)

    def test_coverage_report_flags_missing_years(self):
        panel = make_panel(n_counties=2, years=TRAIN.years)
        report = coverage_report(panel)
        self.assertIn("MISSING", report)


class TestTouchBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "test_touches.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_touch_is_allowed(self):
        budget = TouchBudget(self.path)
        budget.check("m", "1.0.0")
        self.assertEqual(budget.spend("m", "1.0.0"), 1)

    def test_second_touch_is_refused(self):
        budget = TouchBudget(self.path)
        budget.spend("m", "1.0.0")
        with self.assertRaises(SplitViolation) as ctx:
            budget.check("m", "1.0.0")
        self.assertIn("already been scored", str(ctx.exception))

    def test_budget_survives_a_restart(self):
        TouchBudget(self.path).spend("m", "1.0.0")
        reloaded = TouchBudget(self.path)  # fresh object, same file
        self.assertEqual(reloaded.count("m", "1.0.0"), 1)
        with self.assertRaises(SplitViolation):
            reloaded.check("m", "1.0.0")

    def test_a_new_version_gets_a_fresh_budget(self):
        budget = TouchBudget(self.path)
        budget.spend("m", "1.0.0")
        budget.check("m", "1.1.0")  # different version, not yet spent

    def test_budget_matches_the_contract(self):
        self.assertEqual(CONTRACT.test_touch_budget, 1)

    def test_file_is_human_readable(self):
        TouchBudget(self.path).spend("m", "1.0.0")
        self.assertEqual(json.loads(self.path.read_text()), {"m@1.0.0": 1})


if __name__ == "__main__":
    unittest.main()
