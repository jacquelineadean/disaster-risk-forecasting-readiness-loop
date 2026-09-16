"""The contract's verdict logic, and that it has teeth in both directions."""

import unittest

from readiness.engine.baseline import (
    ClimatologyPooled,
    ClimatologySeasonal,
    PersistenceLastYear,
)
from readiness.harness import contract as contract_mod
from readiness.harness import scoring
from tests.fixtures import make_contract, make_panel

CONTRACT = make_contract()


def card(contract=CONTRACT, **overrides) -> scoring.Scorecard:
    """A scorecard that passes everything, so each test can break one thing."""
    bins = tuple(
        {
            "lower": i / 10,
            "upper": (i + 1) / 10,
            "count": 100,
            "populated": True,
            "mean_forecast": i / 10 + 0.05,
            "observed_frequency": i / 10 + 0.05,
        }
        for i in range(10)
    )
    base = dict(
        model="m",
        version="1.0.0",
        contract=contract.name,
        contract_digest=contract.digest(),
        split="validate",
        n_units=1000,
        n_positive=100,
        base_rate=0.1,
        brier_score=0.05,
        brier_score_reference=0.09,
        brier_skill_score=0.44,
        auc=0.82,
        sharpness=0.15,
        reliability=0.001,
        resolution=0.03,
        uncertainty=0.09,
        reliability_bins=bins,
        panel_digest="a" * 16,
        train_digest="b" * 16,
    )
    base.update(overrides)
    return scoring.Scorecard(**base)


class TestVerdict(unittest.TestCase):
    def test_a_good_card_passes(self):
        verdict = contract_mod.evaluate(card(), CONTRACT)
        self.assertTrue(verdict.passed, verdict.format())
        self.assertEqual(verdict.contract, CONTRACT.name)

    def test_zero_skill_fails_because_the_clause_is_strict(self):
        # "BSS > 0", not ">= 0": matching climatology is not beating it.
        self.assertFalse(contract_mod.evaluate(card(brier_skill_score=0.0), CONTRACT).passed)

    def test_negative_skill_fails(self):
        self.assertFalse(contract_mod.evaluate(card(brier_skill_score=-0.01), CONTRACT).passed)

    def test_low_auc_fails(self):
        self.assertFalse(
            contract_mod.evaluate(card(auc=CONTRACT.min_auc - 0.001), CONTRACT).passed
        )

    def test_auc_exactly_at_threshold_passes(self):
        self.assertTrue(contract_mod.evaluate(card(auc=CONTRACT.min_auc), CONTRACT).passed)

    def test_thresholds_come_from_the_contract(self):
        lenient = make_contract(thresholds={"min_auc": 0.6})
        self.assertTrue(contract_mod.evaluate(card(lenient, auc=0.65), lenient).passed)
        strict = make_contract(thresholds={"min_auc": 0.9})
        self.assertFalse(contract_mod.evaluate(card(strict, auc=0.85), strict).passed)

    def test_miscalibrated_populated_bin_fails(self):
        bins = list(card().reliability_bins)
        bins[3] = dict(bins[3], observed_frequency=bins[3]["mean_forecast"] + 0.20)
        verdict = contract_mod.evaluate(card(reliability_bins=tuple(bins)), CONTRACT)
        self.assertFalse(verdict.passed)
        rel = next(c for c in verdict.checks if c.name == "reliability")
        self.assertFalse(rel.passed)

    def test_miscalibration_in_a_thin_bin_is_reported_not_failed(self):
        # A bin with 3 observations cannot fail a 5-point tolerance meaningfully.
        bins = list(card().reliability_bins)
        bins[3] = dict(
            bins[3],
            count=3,
            populated=False,
            observed_frequency=bins[3]["mean_forecast"] + 0.40,
        )
        self.assertTrue(
            contract_mod.evaluate(card(reliability_bins=tuple(bins)), CONTRACT).passed
        )

    def test_populated_is_read_from_the_card_not_recomputed(self):
        # The flag is written at scoring time from the contract's minimum bin
        # count. The verdict must judge the card it was handed: a bin the card
        # calls thin is not judged whatever its count says, and a bin the card
        # calls populated is judged even at n=3.
        bins = list(card().reliability_bins)
        bins[3] = dict(
            bins[3],
            count=1000,
            populated=False,
            observed_frequency=bins[3]["mean_forecast"] + 0.40,
        )
        self.assertTrue(
            contract_mod.evaluate(card(reliability_bins=tuple(bins)), CONTRACT).passed
        )
        bins[3] = dict(bins[3], count=3, populated=True)
        verdict = contract_mod.evaluate(card(reliability_bins=tuple(bins)), CONTRACT)
        rel = next(c for c in verdict.checks if c.name == "reliability")
        self.assertFalse(rel.passed)
        self.assertIn("n=3", rel.detail)

    def test_no_populated_bins_fails_rather_than_vacuously_passing(self):
        bins = tuple(dict(b, count=1, populated=False) for b in card().reliability_bins)
        verdict = contract_mod.evaluate(card(reliability_bins=bins), CONTRACT)
        self.assertFalse(verdict.passed)
        rel = next(c for c in verdict.checks if c.name == "reliability")
        self.assertIn("unmeasurable", rel.detail)

    def test_a_card_from_a_different_contract_fails_provenance(self):
        verdict = contract_mod.evaluate(card(contract_digest="0" * 16), CONTRACT)
        self.assertFalse(verdict.passed)
        prov = next(c for c in verdict.checks if c.name == "contract provenance")
        self.assertFalse(prov.passed)

    def test_a_card_judged_by_a_changed_contract_fails_provenance(self):
        # The card was produced under CONTRACT; the thresholds then moved.
        moved = make_contract(thresholds={"min_auc": 0.65})
        verdict = contract_mod.evaluate(card(), moved)
        prov = next(c for c in verdict.checks if c.name == "contract provenance")
        self.assertFalse(prov.passed)
        self.assertIn("contract changed", prov.detail)


class TestHoldoutSplit(unittest.TestCase):
    """A card scored on the training years may be printed but never PASS."""

    def test_a_train_card_fails_on_the_holdout_check_alone(self):
        verdict = contract_mod.evaluate(card(split="train"), CONTRACT)
        self.assertFalse(verdict.passed)
        failed = [c.name for c in verdict.checks if not c.passed]
        self.assertEqual(failed, ["holdout split"])
        self.assertIn("not a holdout", verdict.checks[-1].detail)

    def test_validate_and_test_cards_pass(self):
        for split in contract_mod.HOLDOUT_SPLITS:
            with self.subTest(split=split):
                self.assertTrue(contract_mod.evaluate(card(split=split), CONTRACT).passed)

    def test_the_check_is_appended_after_the_original_four(self):
        # Committed cards print their checks in this order; the new clause
        # must not reorder them.
        names = [c.name for c in contract_mod.evaluate(card(), CONTRACT).checks]
        self.assertEqual(
            names,
            ["brier skill score", "reliability", "auc", "contract provenance",
             "holdout split"],
        )


class TestAgainstRealModels(unittest.TestCase):
    """End to end on a synthetic panel: the contract must both pass and fail."""

    def setUp(self):
        self.c = make_contract()
        self.panel = make_panel(contract=self.c, n_regions=16)

    def test_reference_scored_against_itself_is_exactly_zero_skill(self):
        card = scoring.score(ClimatologyPooled(), self.panel, self.c, "validate")
        self.assertAlmostEqual(card.brier_skill_score, 0.0, places=6)
        self.assertAlmostEqual(card.auc, 0.5, places=9)
        self.assertFalse(contract_mod.evaluate(card, self.c).passed)

    def test_seasonal_climatology_beats_the_reference(self):
        card = scoring.score(ClimatologySeasonal(), self.panel, self.c, "validate")
        self.assertGreater(card.brier_skill_score, 0.0)

    def test_a_sharp_uncalibrated_model_fails_on_reliability(self):
        card = scoring.score(PersistenceLastYear(), self.panel, self.c, "validate")
        verdict = contract_mod.evaluate(card, self.c)
        rel = next(ch for ch in verdict.checks if ch.name == "reliability")
        self.assertFalse(rel.passed, card.format())
        self.assertFalse(verdict.passed)

    def test_scorecard_carries_the_contract(self):
        card = scoring.score(ClimatologyPooled(), self.panel, self.c, "validate")
        self.assertEqual(card.contract, self.c.name)
        self.assertEqual(card.contract_digest, self.c.digest())
        self.assertIn(self.c.name, card.format())

    def test_a_model_scored_on_its_own_training_years_cannot_pass(self):
        card = scoring.score(ClimatologySeasonal(), self.panel, self.c, "train")
        verdict = contract_mod.evaluate(card, self.c)
        self.assertFalse(verdict.passed)
        holdout = next(ch for ch in verdict.checks if ch.name == "holdout split")
        self.assertFalse(holdout.passed)

    def test_the_same_model_scores_under_a_monthly_contract(self):
        monthly = make_contract(period="month")
        panel = make_panel(contract=monthly, n_regions=8)
        card = scoring.score(ClimatologySeasonal(), panel, monthly, "validate")
        self.assertEqual(card.n_units, 8 * 5 * 12)
        self.assertGreater(card.brier_skill_score, 0.0)


if __name__ == "__main__":
    unittest.main()
