"""The proposable models: registry hygiene and hazard-agnostic behaviour."""

import unittest

from readiness.engine import REGISTRY, build_model, describe_registry, needs_panel
from readiness.engine.baseline import ClimatologyPooled, ClimatologySeasonal
from readiness.harness.scoring import climatology_reference
from readiness.harness.splits import PredictionRequest, TrainingView, split_panel
from tests.fixtures import make_contract, make_panel, region_id


class TestRegistry(unittest.TestCase):
    def test_the_reference_model_is_registered_under_the_contracts_name(self):
        self.assertIn(make_contract().reference_model, REGISTRY)

    def test_only_the_canary_target_needs_a_panel(self):
        self.assertTrue(needs_panel("leaky-oracle"))
        for name in REGISTRY:
            if name != "leaky-oracle":
                self.assertFalse(needs_panel(name), name)

    def test_unknown_model(self):
        with self.assertRaises(KeyError):
            build_model("oracle-of-delphi")

    def test_canary_target_cannot_be_built_without_a_panel(self):
        with self.assertRaises(ValueError):
            build_model("leaky-oracle")

    def test_describe_marks_the_canary_target(self):
        self.assertIn("[canary target]", describe_registry())


class TestModelsAreAgnostic(unittest.TestCase):
    def fit(self, model, contract):
        panel = make_panel(contract=contract, n_regions=6)
        train = contract.splits.train
        view = TrainingView(split_panel(panel, train), train)
        model.fit(view)
        return panel, view

    def test_pooled_equals_the_harness_reference(self):
        c = make_contract()
        model = ClimatologyPooled()
        _panel, view = self.fit(model, c)
        rows = view.rows()
        self.assertEqual(model.p, climatology_reference(sum(y for _, y in rows), len(rows)))

    def test_seasonal_backs_off_for_unseen_regions_and_periods(self):
        c = make_contract()
        model = ClimatologySeasonal()
        self.fit(model, c)
        probs = model.predict(
            PredictionRequest(
                units=(("00000", 2016, 1), ("00000", 2016, 9), (region_id(0), 2016, 2)),
                split_name="validate",
            )
        )
        self.assertEqual(probs[0], model.by_period[1])       # region unseen -> period rate
        self.assertEqual(probs[1], model.pooled_rate)        # period unseen too -> pooled
        self.assertEqual(probs[2], model.by_region_period[(region_id(0), 2)])

    def test_seasonal_works_for_every_period_length(self):
        for period in ("month", "quarter", "year"):
            with self.subTest(period=period):
                c = make_contract(period=period)
                model = ClimatologySeasonal()
                self.fit(model, c)
                self.assertEqual(len(model.by_period), c.periods_per_year)


if __name__ == "__main__":
    unittest.main()
