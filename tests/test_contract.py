"""The contract's verdict logic, and that it has teeth in both directions."""

import unittest

from readiness import config
from readiness.config import CONTRACT
from readiness.engine.baseline import (
    ClimatologyCountyQuarter,
    ClimatologyGlobal,
    PersistenceLastYear,
)
from readiness.harness import contract as contract_mod
from readiness.harness import scoring
from readiness.harness.splits import VALIDATE
from tests.fixtures import make_panel


def card(**overrides) -> scoring.Scorecard:
    """A scorecard that passes everything, so each test can break one thing."""
    bins = tuple(
        {
            "lower": i / 10,
            "upper": (i + 1) / 10,
            "count": 100,
            "mean_forecast": i / 10 + 0.05,
            "observed_frequency": i / 10 + 0.05,
        }
        for i in range(10)
    )
    base = dict(
        model="m",
        version="1.0.0",
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
        contract_digest=CONTRACT.digest(),
    )
    base.update(overrides)
    return scoring.Scorecard(**base)


class TestVerdict(unittest.TestCase):
    def test_a_good_card_passes(self):
        verdict = contract_mod.evaluate(card())
        self.assertTrue(verdict.passed, verdict.format())

    def test_zero_skill_fails_because_the_clause_is_strict(self):
        # "BSS > 0", not ">= 0": matching climatology is not beating it.
        self.assertFalse(contract_mod.evaluate(card(brier_skill_score=0.0)).passed)

    def test_negative_skill_fails(self):
        self.assertFalse(contract_mod.evaluate(card(brier_skill_score=-0.01)).passed)

    def test_low_auc_fails(self):
        self.assertFalse(
            contract_mod.evaluate(card(auc=CONTRACT.min_auc - 0.001)).passed
        )

    def test_auc_exactly_at_threshold_passes(self):
        self.assertTrue(contract_mod.evaluate(card(auc=CONTRACT.min_auc)).passed)

    def test_miscalibrated_populated_bin_fails(self):
        bins = list(card().reliability_bins)
        bins[3] = dict(bins[3], observed_frequency=bins[3]["mean_forecast"] + 0.20)
        verdict = contract_mod.evaluate(card(reliability_bins=tuple(bins)))
        self.assertFalse(verdict.passed)
        rel = next(c for c in verdict.checks if c.name == "reliability")
        self.assertFalse(rel.passed)

    def test_miscalibration_in_a_thin_bin_is_reported_not_failed(self):
        # A bin with 3 observations cannot fail a 5-point tolerance meaningfully.
        bins = list(card().reliability_bins)
        bins[3] = dict(
            bins[3], count=3, observed_frequency=bins[3]["mean_forecast"] + 0.40
        )
        self.assertTrue(contract_mod.evaluate(card(reliability_bins=tuple(bins))).passed)

    def test_no_populated_bins_fails_rather_than_vacuously_passing(self):
        bins = tuple(dict(b, count=1) for b in card().reliability_bins)
        verdict = contract_mod.evaluate(card(reliability_bins=bins))
        self.assertFalse(verdict.passed)
        rel = next(c for c in verdict.checks if c.name == "reliability")
        self.assertIn("unmeasurable", rel.detail)

    def test_a_card_from_a_different_contract_fails_provenance(self):
        verdict = contract_mod.evaluate(card(contract_digest="0" * 16))
        self.assertFalse(verdict.passed)
        prov = next(c for c in verdict.checks if c.name == "contract provenance")
        self.assertFalse(prov.passed)


class TestContractIdentity(unittest.TestCase):
    def test_digest_is_stable(self):
        self.assertEqual(CONTRACT.digest(), config.Contract().digest())

    def test_changing_a_threshold_changes_the_digest(self):
        import dataclasses

        altered = dataclasses.replace(CONTRACT, min_auc=0.65)
        self.assertNotEqual(altered.digest(), CONTRACT.digest())

    def test_changing_the_damage_threshold_changes_the_digest(self):
        import dataclasses

        altered = dataclasses.replace(CONTRACT, damage_property_usd_min=1.0)
        self.assertNotEqual(altered.digest(), CONTRACT.digest())


class TestAgainstRealModels(unittest.TestCase):
    """End to end on a synthetic panel: the contract must both pass and fail."""

    def setUp(self):
        self.panel = make_panel(n_counties=16)

    def test_reference_scored_against_itself_is_exactly_zero_skill(self):
        c = scoring.score(ClimatologyGlobal(), self.panel, VALIDATE)
        self.assertAlmostEqual(c.brier_skill_score, 0.0, places=6)
        self.assertAlmostEqual(c.auc, 0.5, places=9)
        self.assertFalse(contract_mod.evaluate(c).passed)

    def test_county_quarter_climatology_beats_the_reference(self):
        c = scoring.score(ClimatologyCountyQuarter(), self.panel, VALIDATE)
        self.assertGreater(c.brier_skill_score, 0.0)

    def test_a_sharp_uncalibrated_model_fails_on_reliability(self):
        c = scoring.score(PersistenceLastYear(), self.panel, VALIDATE)
        verdict = contract_mod.evaluate(c)
        rel = next(ch for ch in verdict.checks if ch.name == "reliability")
        self.assertFalse(rel.passed, c.format())
        self.assertFalse(verdict.passed)


if __name__ == "__main__":
    unittest.main()
