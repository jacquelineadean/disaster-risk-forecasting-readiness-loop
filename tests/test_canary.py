"""The Phase 0 exit criterion: the harness rejects a deliberately leaked model.

Report §6:

    Exit when [...] the harness rejects a deliberately leaked model
    (a canary test).
"""

import unittest

from readiness.engine.baseline import (
    ClimatologyPooled,
    ClimatologySeasonal,
    LeakyOracle,
)
from readiness.harness import canary, scoring
from tests.fixtures import make_contract, make_panel


class TestCanaryRejectsLeakage(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.panel = make_panel(contract=self.c, n_regions=10)

    def screen(self, model):
        return scoring.screen(model, self.panel, self.c, "validate")

    def test_leaky_oracle_is_rejected(self):
        card, report = self.screen(LeakyOracle(self.panel))
        self.assertTrue(
            report.rejected,
            f"harness accepted a leaked model (BSS {card.brier_skill_score:+.4f}, "
            f"AUC {card.auc:.4f}) — Phase 0 does not exit",
        )

    def test_leaky_oracle_would_otherwise_look_spectacular(self):
        # The point of the canary: without it, the contract would happily pass
        # this model. That is what makes "iterate until the score clears"
        # dangerous.
        card, _ = self.screen(LeakyOracle(self.panel))
        self.assertGreater(card.brier_skill_score, 0.99)
        self.assertGreater(card.auc, 0.99)

    def test_multiple_independent_checks_trip(self):
        _card, report = self.screen(LeakyOracle(self.panel))
        tripped = {f.check for f in report.findings if f.tripped}
        self.assertIn("implausible skill", tripped)
        self.assertIn("train provenance", tripped)

    def test_a_subtler_oracle_still_trips_on_provenance(self):
        # Lower confidence keeps BSS under the skill ceiling on some samples,
        # but the model still cannot produce a matching training digest.
        _card, report = self.screen(LeakyOracle(self.panel, confidence=0.62))
        provenance = next(f for f in report.findings if f.check == "train provenance")
        self.assertTrue(provenance.tripped)
        self.assertTrue(report.rejected)

    def test_rejection_does_not_depend_on_the_hazard(self):
        for hazard, period in (("tornado", "quarter"), ("heat", "month"), ("wildfire", "year")):
            with self.subTest(hazard=hazard):
                c = make_contract(hazard=hazard, period=period)
                panel = make_panel(contract=c, n_regions=6)
                _card, report = scoring.screen(LeakyOracle(panel), panel, c, "validate")
                self.assertTrue(report.rejected)


class TestCanaryClearsHonestModels(unittest.TestCase):
    """A canary that rejects everything is not a canary, it is a wall."""

    def setUp(self):
        self.c = make_contract()
        self.panel = make_panel(contract=self.c, n_regions=10)

    def test_pooled_climatology_is_clear(self):
        _card, report = scoring.screen(ClimatologyPooled(), self.panel, self.c, "validate")
        self.assertFalse(report.rejected, report.format())

    def test_seasonal_climatology_is_clear(self):
        _card, report = scoring.screen(ClimatologySeasonal(), self.panel, self.c, "validate")
        self.assertFalse(report.rejected, report.format())

    def test_a_genuinely_skilful_model_is_not_punished(self):
        # The seasonal climatology should beat the pooled climatology on a
        # panel built with real spatial and seasonal structure — and clearing
        # the contract must not itself look like leakage.
        card, report = scoring.screen(ClimatologySeasonal(), self.panel, self.c, "validate")
        self.assertGreater(card.brier_skill_score, 0.0)
        self.assertFalse(report.rejected)


class TestCanaryChecks(unittest.TestCase):
    def test_missing_declaration_is_reported_but_not_a_rejection(self):
        report = canary.run(
            probs=[0.2, 0.3], outcomes=[0, 1], brier_skill_score=0.05, auc=0.6
        )
        provenance = next(f for f in report.findings if f.check == "train provenance")
        self.assertFalse(provenance.tripped)
        self.assertIn("skipped", provenance.detail)
        self.assertFalse(report.rejected)

    def test_implausible_auc_alone_trips(self):
        report = canary.run(
            probs=[0.2, 0.3], outcomes=[0, 1], brier_skill_score=0.1, auc=1.0
        )
        self.assertTrue(report.rejected)

    def test_near_binary_agreement_trips(self):
        n = 200
        probs = [0.999 if i % 2 else 0.001 for i in range(n)]
        outcomes = [1 if i % 2 else 0 for i in range(n)]
        report = canary.run(
            probs=probs, outcomes=outcomes, brier_skill_score=0.5, auc=0.9
        )
        agreement = next(f for f in report.findings if f.check == "outcome agreement")
        self.assertTrue(agreement.tripped)


if __name__ == "__main__":
    unittest.main()
