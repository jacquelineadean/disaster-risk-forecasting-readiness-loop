"""Scoring: one fit per screening, bare units at predict time, one fixed reference.

`scoring.py` is where a model's forecasts meet labels it was never shown. The
sequence — training view, fit, bare request, predict, *then* labels — is
written down once in `_fit_predict`, and `score`, `predictions_for` and
`screen` are all projections of it. These tests pin that: a screening fits
once and the card and the canary describe the same forecasts.
"""

import dataclasses
import unittest
from unittest import mock

from readiness.engine.baseline import ClimatologyPooled, ClimatologySeasonal
from readiness.harness import scoring
from readiness.harness.splits import PredictionRequest, split_panel
from tests.fixtures import make_contract, make_panel


class Counting(ClimatologySeasonal):
    """A seasonal climatology that counts its calls and keeps what it was handed."""

    def __init__(self):
        super().__init__()
        self.fits = 0
        self.predicts = 0
        self.requests = []

    def _fit(self, view):
        self.fits += 1
        super()._fit(view)

    def _predict(self, request):
        self.predicts += 1
        self.requests.append(request)
        return super()._predict(request)


class TestOneFit(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.panel = make_panel(contract=self.c, n_regions=8)

    def test_screen_fits_and_predicts_exactly_once(self):
        model = Counting()
        scoring.screen(model, self.panel, self.c, "validate")
        self.assertEqual((model.fits, model.predicts), (1, 1))

    def test_score_and_predictions_for_each_fit_once(self):
        for fn in (scoring.score, scoring.predictions_for):
            with self.subTest(fn=fn.__name__):
                model = Counting()
                fn(model, self.panel, self.c, "validate")
                self.assertEqual((model.fits, model.predicts), (1, 1))

    def test_the_card_and_the_canary_describe_the_same_forecasts(self):
        # The canary's skill and AUC findings quote the card's numbers, and
        # the arrays it inspected are the ones `predictions_for` exposes.
        from readiness.harness import canary as canary_mod

        seen = {}
        real_run = canary_mod.run

        def spy(**kwargs):
            seen["probs"] = list(kwargs["probs"])
            seen["outcomes"] = list(kwargs["outcomes"])
            return real_run(**kwargs)

        with mock.patch.object(canary_mod, "run", spy):
            card, report = scoring.screen(
                ClimatologySeasonal(), self.panel, self.c, "validate"
            )
        alone = scoring.score(ClimatologySeasonal(), self.panel, self.c, "validate")
        self.assertEqual(card, alone)
        _units, probs, outcomes = scoring.predictions_for(
            ClimatologySeasonal(), self.panel, self.c, "validate"
        )
        self.assertEqual(seen["probs"], probs)
        self.assertEqual(seen["outcomes"], outcomes)
        skill = next(f for f in report.findings if f.check == "implausible skill")
        self.assertIn(f"{card.brier_skill_score:+.4f}", skill.detail)
        provenance = next(f for f in report.findings if f.check == "train provenance")
        self.assertIn(card.train_digest, provenance.detail)
        self.assertFalse(provenance.tripped)


class TestTheSequence(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.panel = make_panel(contract=self.c, n_regions=8)

    def test_predict_is_handed_bare_units_of_the_scored_split_only(self):
        model = Counting()
        scoring.score(model, self.panel, self.c, "validate")
        (request,) = model.requests
        self.assertIsInstance(request, PredictionRequest)
        self.assertFalse(hasattr(request, "labels"))
        self.assertEqual(request.split_name, "validate")
        expected = split_panel(self.panel, self.c.splits.validate)
        self.assertEqual(request.units, expected.units)

    def test_predictions_for_returns_the_split_in_panel_order(self):
        units, probs, outcomes = scoring.predictions_for(
            ClimatologySeasonal(), self.panel, self.c, "validate"
        )
        expected = split_panel(self.panel, self.c.splits.validate)
        self.assertEqual(tuple(units), expected.units)
        self.assertEqual(tuple(outcomes), expected.labels)
        self.assertEqual(len(probs), len(units))

    def test_a_short_forecast_vector_is_refused(self):
        class Short(ClimatologyPooled):
            def _predict(self, request):
                return [0.1]

        with self.assertRaises(ValueError):
            scoring.score(Short(), self.panel, self.c, "validate")

    def test_a_split_object_and_its_name_score_identically(self):
        by_name = scoring.score(ClimatologyPooled(), self.panel, self.c, "validate")
        by_split = scoring.score(
            ClimatologyPooled(), self.panel, self.c, self.c.splits.validate
        )
        self.assertEqual(by_name, by_split)


class TestTheReference(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.panel = make_panel(contract=self.c, n_regions=8)

    def test_the_reference_cannot_be_overridden(self):
        # `reference_probs=` used to be accepted and never used. A caller who
        # could supply the yardstick could make any model look skilful.
        with self.assertRaises(TypeError):
            scoring.score(  # type: ignore[call-arg]
                ClimatologyPooled(), self.panel, self.c, "validate",
                reference_probs=[0.5] * 10,
            )

    def test_the_reference_is_the_pooled_climatology_itself(self):
        card = scoring.score(ClimatologyPooled(), self.panel, self.c, "validate")
        self.assertEqual(card.brier_score, card.brier_score_reference)
        self.assertEqual(card.brier_skill_score, 0.0)

    def test_the_reference_is_the_training_base_rate(self):
        train = split_panel(self.panel, self.c.splits.train)
        self.assertEqual(
            scoring.climatology_reference(sum(train.labels), len(train)),
            train.base_rate,
        )
        with self.assertRaises(ValueError):
            scoring.climatology_reference(0, 0)


class TestScorecardShape(unittest.TestCase):
    """The card's fields and their order are part of every committed ledger."""

    FIELDS = [
        "model", "version", "contract", "contract_digest", "split", "n_units",
        "n_positive", "base_rate", "brier_score", "brier_score_reference",
        "brier_skill_score", "auc", "sharpness", "reliability", "resolution",
        "uncertainty", "reliability_bins", "panel_digest", "train_digest",
    ]

    def test_fields_and_order_are_frozen(self):
        names = [f.name for f in dataclasses.fields(scoring.Scorecard)]
        self.assertEqual(names, self.FIELDS)

    def test_populated_follows_the_contracts_minimum_bin_count(self):
        c = make_contract(thresholds={"reliability_min_bin_count": 7})
        panel = make_panel(contract=c, n_regions=8)
        card = scoring.score(ClimatologySeasonal(), panel, c, "validate")
        self.assertEqual(len(card.reliability_bins), c.n_reliability_bins)
        for b in card.reliability_bins:
            self.assertEqual(b["populated"], b["count"] >= 7, b)
            if not b["count"]:
                self.assertIsNone(b["mean_forecast"])

    def test_digests_name_the_panel_scored_and_the_view_fitted(self):
        c = make_contract()
        panel = make_panel(contract=c, n_regions=8)
        card = scoring.score(ClimatologySeasonal(), panel, c, "validate")
        scored = split_panel(panel, c.splits.validate)
        self.assertEqual(card.panel_digest, scored.digest())
        self.assertEqual(card.train_digest, split_panel(panel, c.splits.train).digest())
        self.assertEqual(card.n_units, len(scored))


if __name__ == "__main__":
    unittest.main()
