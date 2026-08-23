"""Metrics are checked against values that can be worked out by hand.

If these drift, every score in the ledger becomes incomparable, so the
assertions are exact rather than approximate wherever the arithmetic allows.
"""

import unittest

from readiness.harness import metrics


class TestBrier(unittest.TestCase):
    def test_perfect_forecast_scores_zero(self):
        self.assertEqual(metrics.brier_score([1.0, 0.0, 1.0], [1, 0, 1]), 0.0)

    def test_worst_forecast_scores_one(self):
        self.assertEqual(metrics.brier_score([0.0, 1.0], [1, 0]), 1.0)

    def test_hand_computed(self):
        # (0.3-0)^2 + (0.8-1)^2 = 0.09 + 0.04 = 0.13; /2 = 0.065
        self.assertAlmostEqual(metrics.brier_score([0.3, 0.8], [0, 1]), 0.065, places=12)

    def test_constant_base_rate_forecast_equals_variance(self):
        # Forecasting the base rate p everywhere gives BS = p(1-p).
        outcomes = [1] * 30 + [0] * 70
        p = 0.3
        self.assertAlmostEqual(
            metrics.brier_score([p] * 100, outcomes), p * (1 - p), places=12
        )

    def test_rejects_bad_input(self):
        with self.assertRaises(metrics.MetricError):
            metrics.brier_score([0.5], [1, 0])
        with self.assertRaises(metrics.MetricError):
            metrics.brier_score([1.5], [1])
        with self.assertRaises(metrics.MetricError):
            metrics.brier_score([0.5], [2])
        with self.assertRaises(metrics.MetricError):
            metrics.brier_score([], [])


class TestSkill(unittest.TestCase):
    def test_identical_to_reference_is_zero_skill(self):
        self.assertEqual(metrics.brier_skill_score(0.21, 0.21), 0.0)

    def test_better_than_reference_is_positive(self):
        self.assertGreater(metrics.brier_skill_score(0.10, 0.21), 0.0)

    def test_worse_than_reference_is_negative(self):
        self.assertLess(metrics.brier_skill_score(0.30, 0.21), 0.0)

    def test_perfect_forecast_is_unit_skill(self):
        self.assertEqual(metrics.brier_skill_score(0.0, 0.21), 1.0)

    def test_zero_reference_is_an_error_not_an_infinity(self):
        with self.assertRaises(metrics.MetricError):
            metrics.brier_skill_score(0.1, 0.0)


class TestAUC(unittest.TestCase):
    def test_perfect_separation(self):
        self.assertEqual(metrics.roc_auc([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]), 1.0)

    def test_inverted_separation(self):
        self.assertEqual(metrics.roc_auc([0.1, 0.2, 0.8, 0.9], [1, 1, 0, 0]), 0.0)

    def test_constant_forecast_is_exactly_one_half(self):
        # This is why ties get half credit: climatology must score 0.5, and the
        # contract's AUC clause depends on that being exact.
        self.assertEqual(metrics.roc_auc([0.25] * 8, [1, 0, 1, 0, 1, 0, 1, 0]), 0.5)

    def test_hand_computed_with_ties(self):
        # positives {0.5, 0.5}, negatives {0.5, 0.1}: the 0.1 is beaten by both
        # positives (2 full), the 0.5 ties both (1 half each) -> 3/4.
        self.assertAlmostEqual(
            metrics.roc_auc([0.5, 0.5, 0.5, 0.1], [1, 1, 0, 0]), 0.75, places=12
        )

    def test_single_class_is_an_error(self):
        with self.assertRaises(metrics.MetricError):
            metrics.roc_auc([0.3, 0.4], [0, 0])


class TestReliability(unittest.TestCase):
    def test_bins_partition_the_sample(self):
        probs = [i / 100 for i in range(101)]
        outcomes = [i % 2 for i in range(101)]
        table = metrics.reliability_table(probs, outcomes, n_bins=10)
        self.assertEqual(sum(b.count for b in table), len(probs))

    def test_probability_one_lands_in_the_final_bin(self):
        table = metrics.reliability_table([1.0], [1], n_bins=10)
        self.assertEqual(table[-1].count, 1)

    def test_perfectly_calibrated_forecast_has_zero_deviation(self):
        # 100 forecasts of 0.3, exactly 30 of which occur.
        probs = [0.3] * 100
        outcomes = [1] * 30 + [0] * 70
        table = metrics.reliability_table(probs, outcomes, n_bins=10)
        bin3 = next(b for b in table if b.count)
        self.assertAlmostEqual(bin3.mean_forecast, 0.3, places=12)
        self.assertAlmostEqual(bin3.observed_frequency, 0.3, places=12)
        self.assertAlmostEqual(bin3.deviation, 0.0, places=12)

    def test_empty_bins_are_reported_not_dropped(self):
        table = metrics.reliability_table([0.05] * 10, [0] * 10, n_bins=10)
        self.assertEqual(len(table), 10)
        self.assertEqual(sum(1 for b in table if b.count == 0), 9)


class TestMurphy(unittest.TestCase):
    def test_decomposition_recomposes_for_binned_forecasts(self):
        # Forecasts that sit exactly on bin means, so the decomposition is exact.
        probs = [0.05] * 100 + [0.65] * 100
        outcomes = [1] * 5 + [0] * 95 + [1] * 65 + [0] * 35
        d = metrics.murphy_decomposition(probs, outcomes, n_bins=10)
        self.assertAlmostEqual(
            d.recomposed, metrics.brier_score(probs, outcomes), places=10
        )

    def test_climatology_has_zero_resolution(self):
        outcomes = [1] * 20 + [0] * 80
        d = metrics.murphy_decomposition([0.2] * 100, outcomes, n_bins=10)
        self.assertAlmostEqual(d.resolution, 0.0, places=12)
        self.assertAlmostEqual(d.reliability, 0.0, places=12)
        self.assertAlmostEqual(d.uncertainty, 0.16, places=12)


class TestSharpness(unittest.TestCase):
    def test_constant_forecast_is_perfectly_unsharp(self):
        self.assertEqual(metrics.sharpness([0.3] * 50), 0.0)

    def test_varied_forecast_is_sharper(self):
        self.assertGreater(metrics.sharpness([0.1, 0.9] * 25), 0.0)


if __name__ == "__main__":
    unittest.main()
