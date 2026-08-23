"""The Phase 0 exit criterion: the harness rejects a deliberately leaked model.

Report §6:

    Exit when [...] the harness rejects a deliberately leaked model
    (a canary test).
"""

import unittest

from readiness.engine.baseline import (
    ClimatologyCountyQuarter,
    ClimatologyGlobal,
    LeakyOracle,
)
from readiness.harness import canary, scoring
from readiness.harness.splits import TRAIN, VALIDATE, TrainingView, split_panel
from tests.fixtures import make_panel


def _screen(model, panel, split=VALIDATE):
    card = scoring.score(model, panel, split)
    _units, probs, outcomes = scoring.predictions_for(model, panel, split)
    view = TrainingView(split_panel(panel, TRAIN), TRAIN)
    report = canary.run(
        probs=probs,
        outcomes=outcomes,
        brier_skill_score=card.brier_skill_score,
        auc=card.auc,
        view=view,
        declared_train_digest=getattr(model, "training_digest", None),
    )
    return card, report


class TestCanaryRejectsLeakage(unittest.TestCase):
    def setUp(self):
        self.panel = make_panel(n_counties=10)

    def test_leaky_oracle_is_rejected(self):
        card, report = _screen(LeakyOracle(self.panel), self.panel)
        self.assertTrue(
            report.rejected,
            f"harness accepted a leaked model (BSS {card.brier_skill_score:+.4f}, "
            f"AUC {card.auc:.4f}) — Phase 0 does not exit",
        )

    def test_leaky_oracle_would_otherwise_look_spectacular(self):
        # The point of the canary: without it, the contract would happily pass
        # this model. That is what makes "iterate until the score clears"
        # dangerous.
        card, _ = _screen(LeakyOracle(self.panel), self.panel)
        self.assertGreater(card.brier_skill_score, 0.99)
        self.assertGreater(card.auc, 0.99)

    def test_multiple_independent_checks_trip(self):
        _card, report = _screen(LeakyOracle(self.panel), self.panel)
        tripped = {f.check for f in report.findings if f.tripped}
        self.assertIn("implausible skill", tripped)
        self.assertIn("train provenance", tripped)

    def test_a_subtler_oracle_still_trips_on_provenance(self):
        # Lower confidence keeps BSS under the skill ceiling on some samples,
        # but the model still cannot produce a matching training digest.
        _card, report = _screen(LeakyOracle(self.panel, confidence=0.62), self.panel)
        provenance = next(
            f for f in report.findings if f.check == "train provenance"
        )
        self.assertTrue(provenance.tripped)
        self.assertTrue(report.rejected)


class TestCanaryClearsHonestModels(unittest.TestCase):
    """A canary that rejects everything is not a canary, it is a wall."""

    def setUp(self):
        self.panel = make_panel(n_counties=10)

    def test_global_climatology_is_clear(self):
        _card, report = _screen(ClimatologyGlobal(), self.panel)
        self.assertFalse(report.rejected, report.format())

    def test_county_quarter_climatology_is_clear(self):
        _card, report = _screen(ClimatologyCountyQuarter(), self.panel)
        self.assertFalse(report.rejected, report.format())

    def test_a_genuinely_skilful_model_is_not_punished(self):
        # The county-quarter climatology should beat global climatology on a
        # panel built with real spatial and seasonal structure — and clearing
        # the contract must not itself look like leakage.
        card, report = _screen(ClimatologyCountyQuarter(), self.panel)
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
