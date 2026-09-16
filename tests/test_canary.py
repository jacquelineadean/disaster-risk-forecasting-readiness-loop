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
    PersistenceLastYear,
)
from readiness.harness import canary, scoring
from readiness.harness.splits import TrainingView, split_panel
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

    def test_an_honest_climatology_on_a_rare_hazard_is_not_punished(self):
        # Regression: with a base rate well under 1%, an honest seasonal
        # climatology forecasts <= 0.01 for most units, and those forecasts
        # agree with the (mostly zero) outcomes almost always. A pooled
        # near-binary agreement check rejected it as a leak.
        c = make_contract(hazard="tropical_cyclone", period="month")
        panel = make_panel(contract=c, n_regions=40, rare=True)
        self.assertLess(panel.base_rate, 0.01)
        card, report = scoring.screen(ClimatologySeasonal(), panel, c, "validate")
        agreement = next(f for f in report.findings if f.check == "outcome agreement")
        self.assertFalse(agreement.tripped, agreement.detail)
        self.assertFalse(report.rejected, report.format())
        # ... and the oracle is still caught on the same rare panel.
        _card, leaked = scoring.screen(LeakyOracle(panel), panel, c, "validate")
        self.assertTrue(leaked.rejected)
        self.assertTrue(
            next(f for f in leaked.findings if f.check == "outcome agreement").tripped
        )

    def test_a_genuinely_skilful_model_is_not_punished(self):
        # The seasonal climatology should beat the pooled climatology on a
        # panel built with real spatial and seasonal structure — and clearing
        # the contract must not itself look like leakage.
        card, report = scoring.screen(ClimatologySeasonal(), self.panel, self.c, "validate")
        self.assertGreater(card.brier_skill_score, 0.0)
        self.assertFalse(report.rejected)


class TestCanaryChecks(unittest.TestCase):
    def view(self):
        c = make_contract()
        panel = make_panel(contract=c, n_regions=4)
        return TrainingView(split_panel(panel, c.splits.train), c.splits.train)

    def provenance(self, **kw):
        report = canary.run(
            probs=[0.2, 0.3], outcomes=[0, 1], brier_skill_score=0.05, auc=0.6, **kw
        )
        return report, next(f for f in report.findings if f.check == "train provenance")

    def test_no_view_means_the_check_is_skipped_not_tripped(self):
        # The harness supplied nothing to compare against, so there is no
        # finding to make either way.
        report, provenance = self.provenance()
        self.assertFalse(provenance.tripped)
        self.assertIn("skipped", provenance.detail)
        self.assertFalse(report.rejected)

    def test_an_undeclared_digest_trips_when_a_view_was_supplied(self):
        # Silence is not innocence: a model that was handed a training view
        # and declares nothing about it is rejected, not waved through.
        report, provenance = self.provenance(view=self.view())
        self.assertTrue(provenance.tripped)
        self.assertIn("declared no training digest", provenance.detail)
        self.assertTrue(report.rejected)

    def test_a_matching_digest_is_clear_and_a_mismatch_trips(self):
        view = self.view()
        report, provenance = self.provenance(view=view, declared_train_digest=view.digest)
        self.assertFalse(provenance.tripped)
        self.assertFalse(report.rejected)
        report, provenance = self.provenance(view=view, declared_train_digest="0" * 16)
        self.assertTrue(provenance.tripped)
        self.assertTrue(report.rejected)

    def test_every_baseline_declares_its_digest(self):
        # The base class sets it from the view; a baseline that bypassed the
        # base class would now be rejected by the check above.
        c = make_contract()
        panel = make_panel(contract=c, n_regions=6)
        for model in (ClimatologyPooled(), ClimatologySeasonal(), PersistenceLastYear()):
            with self.subTest(model=model.name):
                _card, report = scoring.screen(model, panel, c, "validate")
                provenance = next(
                    f for f in report.findings if f.check == "train provenance"
                )
                self.assertFalse(provenance.tripped, provenance.detail)

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

    def test_near_zero_forecasts_on_a_rare_outcome_do_not_trip(self):
        # 995 non-occurrences forecast at 0.005 and 5 occurrences forecast at
        # 0.3: honest, unsharp, and not a leak.
        probs = [0.005] * 995 + [0.3] * 5
        outcomes = [0] * 995 + [1] * 5
        report = canary.run(
            probs=probs, outcomes=outcomes, brier_skill_score=0.05, auc=0.8
        )
        agreement = next(f for f in report.findings if f.check == "outcome agreement")
        self.assertFalse(agreement.tripped, agreement.detail)
        self.assertIn("0.0% of occurrences", agreement.detail)


if __name__ == "__main__":
    unittest.main()
