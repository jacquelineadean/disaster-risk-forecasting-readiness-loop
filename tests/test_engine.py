"""The proposable models: registry hygiene and hazard-agnostic behaviour."""

import inspect
import unittest

from readiness.engine import REGISTRY, FittedModel, build_model, describe_registry
from readiness.engine.baseline import (
    ClimatologyPooled,
    ClimatologySeasonal,
    LeakyOracle,
    PersistenceLastYear,
)
from readiness.harness.scoring import climatology_reference
from readiness.harness.splits import PredictionRequest, TrainingView, split_panel
from tests.fixtures import make_contract, make_panel, region_id


class TestRegistry(unittest.TestCase):
    def test_the_reference_model_is_registered_under_the_contracts_name(self):
        self.assertIn(make_contract().reference_model, REGISTRY)

    def test_only_the_canary_target_is_flagged(self):
        self.assertTrue(REGISTRY["leaky-oracle"].is_canary_target)
        for name, spec in REGISTRY.items():
            if name != "leaky-oracle":
                self.assertFalse(spec.is_canary_target, name)

    def test_only_the_canary_target_receives_the_panel(self):
        # A call site passes the dataset's panel once, whatever it is building.
        # The registry hands it to the canary target and to nothing else, so
        # no forecaster can be constructed with its own outcomes in hand.
        panel = make_panel(n_regions=4)
        oracle = build_model("leaky-oracle", canary_panel=panel)
        self.assertIsInstance(oracle, LeakyOracle)
        for name, spec in REGISTRY.items():
            if spec.is_canary_target:
                continue
            with self.subTest(model=name):
                model = build_model(name, canary_panel=panel)
                self.assertFalse(
                    any(v is panel for v in vars(model).values()),
                    f"{name} was handed the panel",
                )

    def test_unknown_model(self):
        with self.assertRaises(KeyError):
            build_model("oracle-of-delphi")

    def test_canary_target_cannot_be_built_without_a_panel(self):
        with self.assertRaises(ValueError):
            build_model("leaky-oracle")

    def test_there_is_no_positional_panel_channel(self):
        with self.assertRaises(TypeError):
            build_model("leaky-oracle", make_panel(n_regions=4))  # type: ignore[misc]

    def test_every_registered_model_is_a_fitted_model(self):
        panel = make_panel(n_regions=4)
        for name in REGISTRY:
            model = build_model(name, canary_panel=panel)
            self.assertIsInstance(model, FittedModel, name)

    def test_describe_marks_the_canary_target(self):
        self.assertIn("[canary target]", describe_registry())

    def test_describe_lists_every_parameter(self):
        text = describe_registry()
        for spec in REGISTRY.values():
            for key, p in spec.params.items():
                self.assertIn(key, text)
                self.assertIn(p["help"], text)


class TestParameterSchemas(unittest.TestCase):
    """`ModelSpec.params` describes exactly what `build_model(**kwargs)` accepts."""

    TYPES = {"float": float, "int": int, "bool": bool, "str": str}

    def test_the_documented_parameters(self):
        self.assertEqual(list(REGISTRY["climatology-seasonal"].params), ["shrinkage"])
        self.assertEqual(list(REGISTRY["persistence-last-year"].params), ["hit", "miss"])
        self.assertEqual(REGISTRY["climatology-pooled"].params, {})
        self.assertEqual(list(REGISTRY["leaky-oracle"].params), ["confidence"])

    def test_every_entry_matches_the_constructor(self):
        for name, spec in REGISTRY.items():
            signature = inspect.signature(spec.factory)
            for key, p in spec.params.items():
                with self.subTest(model=name, param=key):
                    self.assertIn(key, signature.parameters)
                    self.assertEqual(p["default"], signature.parameters[key].default)
                    self.assertIn(p["type"], self.TYPES)
                    self.assertIsInstance(p["default"], self.TYPES[p["type"]])
                    self.assertTrue(p["help"])

    def test_defaults_build_the_same_model_as_no_arguments(self):
        panel = make_panel(n_regions=4)
        for name, spec in REGISTRY.items():
            with self.subTest(model=name):
                defaults = {k: p["default"] for k, p in spec.params.items()}
                explicit = build_model(name, canary_panel=panel, **defaults)
                implicit = build_model(name, canary_panel=panel)
                for key in spec.params:
                    self.assertEqual(getattr(explicit, key), getattr(implicit, key))


class TestFittedModel(unittest.TestCase):
    """The base class owns the guard and the digest, so every model gets both."""

    def setUp(self):
        self.c = make_contract()
        self.panel = make_panel(contract=self.c, n_regions=4)
        train = self.c.splits.train
        self.view = TrainingView(split_panel(self.panel, train), train)
        self.request = PredictionRequest.from_panel(
            split_panel(self.panel, self.c.splits.validate), self.c.splits.validate
        )

    def test_predict_before_fit_is_refused(self):
        for model in (ClimatologyPooled(), ClimatologySeasonal(), PersistenceLastYear()):
            with self.subTest(model=model.name), self.assertRaises(RuntimeError):
                model.predict(self.request)

    def test_fit_declares_the_views_digest(self):
        for model in (ClimatologyPooled(), ClimatologySeasonal(), PersistenceLastYear()):
            with self.subTest(model=model.name):
                self.assertIsNone(model.training_digest)
                model.fit(self.view)
                self.assertEqual(model.training_digest, self.view.digest)

    def test_the_oracle_declares_a_digest_that_cannot_match(self):
        oracle = LeakyOracle(self.panel)
        oracle.fit(self.view)
        self.assertEqual(oracle.training_digest, "0" * 16)
        self.assertNotEqual(oracle.training_digest, self.view.digest)

    def test_hooks_are_abstract(self):
        class Bare(FittedModel):
            name = "bare"
            version = "0"

        with self.assertRaises(NotImplementedError):
            Bare().fit(self.view)

    def test_persistence_is_constant_after_the_first_holdout_year(self):
        # No holdout label reaches the model, so only the first holdout year
        # has a predecessor in its history; every later year is all `miss`.
        model = PersistenceLastYear()
        model.fit(self.view)
        probs = model.predict(self.request)
        first = self.c.validate_years[0]
        later = [p for (_r, y, _p), p in zip(self.request, probs) if y > first]
        self.assertTrue(later)
        self.assertEqual(set(later), {model.miss})


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
