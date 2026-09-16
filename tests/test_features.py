"""The feature channel: the harness computes the cutoffs, and proves it.

Everything here is synthetic. A `FakeSeries` source hands the harness monthly
values for the fixture regions; a `FakeStatic` source hands a table. The
tests are about the firewall, not about any real dataset.
"""

from __future__ import annotations

import math
import unittest

from readiness.harness import features as F
from tests.fixtures import make_contract, make_panel, region_id


class FakeSeries:
    """A monthly series per region: value = month index, so windows are easy to check."""

    name = "era5"
    kind = "series"
    manifest_keys = ("open-meteo/era5/99",)
    derived_through = None
    global_coverage = True

    def __init__(self, start=F.month_index(1990, 1), end=F.month_index(2026, 1), missing=()):
        self.start, self.end, self.missing = start, end, set(missing)

    def series(self, region, variable):
        if variable != "precip_mm":
            return None
        values = tuple(
            F.NAN if m in self.missing else float(m) for m in range(self.start, self.end)
        )
        return F.Series(region, self.start, values)

    def static(self, region):
        return None


class FakeStatic:
    name = "gazetteer"
    kind = "static"
    manifest_keys = ("census/gazetteer_counties2020",)
    derived_through = None
    global_coverage = False

    def __init__(self, name="gazetteer", derived_through=None, keys=None):
        self.name, self.derived_through = name, derived_through
        self.manifest_keys = keys or self.manifest_keys

    def series(self, region, variable):
        return None

    def static(self, region):
        return {"water_share": 0.1 + int(region[-3:]) / 1000, "lat": 30.0}


class TestCalendar(unittest.TestCase):
    def test_period_start_for_year_quarter_month(self):
        self.assertEqual(F.period_start(2004, 1, 1), F.month_index(2004, 1))
        self.assertEqual(F.period_start(2004, 3, 4), F.month_index(2004, 7))
        self.assertEqual(F.period_start(2004, 8, 12), F.month_index(2004, 8))
        with self.assertRaises(ValueError):
            F.period_start(2004, 5, 4)

    def test_series_before_is_strict(self):
        s = F.Series("r", 100, (1.0, 2.0, 3.0))
        self.assertEqual(s.before(102).values, (1.0, 2.0))
        self.assertEqual(s.before(100).values, ())
        self.assertEqual(s.before(999).values, (1.0, 2.0, 3.0))

    def test_window_pads_with_nan_outside_the_series(self):
        s = F.Series("r", 100, (1.0, 2.0, 3.0))
        w = s.window(101, 3)
        self.assertTrue(math.isnan(w[0]) and math.isnan(w[1]))
        self.assertEqual(w[2], 1.0)


class TestSpec(unittest.TestCase):
    def test_lag_zero_is_refused_at_construction(self):
        with self.assertRaises(ValueError):
            F.FeatureSpec("p1", "era5", "precip_mm", "trailing_sum", 1, lag_months=0)

    def test_unknown_transform_is_refused(self):
        with self.assertRaises(ValueError):
            F.FeatureSpec("p1", "era5", "precip_mm", "future_sum", 1)

    def test_cutoff_is_period_start_minus_lag(self):
        spec = F.FeatureSpec("p3", "era5", "precip_mm", "trailing_sum", 3, lag_months=2)
        unit = ("99001", 2004, 3)  # Q3 starts in July
        self.assertEqual(spec.cutoff(unit, 4), F.month_index(2004, 7) - 2)


class TestBuildFrame(unittest.TestCase):
    def setUp(self):
        self.contract = make_contract()
        self.ppy = self.contract.periods_per_year
        self.sources = {"era5": FakeSeries(), "gazetteer": FakeStatic()}
        self.specs = (
            F.FeatureSpec("p1", "era5", "precip_mm", "trailing_sum", 1),
            F.FeatureSpec("p3", "era5", "precip_mm", "trailing_mean", 3, lag_months=1),
            F.FeatureSpec("pmax12", "era5", "precip_mm", "trailing_max", 12),
            F.FeatureSpec("same10", "era5", "precip_mm", "same_period_mean", 120),
            F.FeatureSpec("water", "gazetteer", "water_share"),
        )

    def test_trailing_windows_end_before_the_cutoff(self):
        # Q3 2004 starts in July; with lag 1 the cutoff is June, so June itself
        # is withheld and the last month a transform sees is May.
        unit = (region_id(0), 2004, 3)
        frame = F.build_frame(self.specs, self.sources, [unit], self.ppy)
        row = dict(zip(frame.columns, frame.row(unit)))
        may = F.month_index(2004, 5)
        self.assertEqual(row["p1"], float(may))
        self.assertEqual(row["p3"], (may - 2 + may) / 2)
        self.assertEqual(row["pmax12"], float(may))
        self.assertAlmostEqual(row["water"], 0.1 + 1 / 1000)

    def test_a_longer_lag_withholds_more(self):
        spec = F.FeatureSpec("p1", "era5", "precip_mm", "trailing_sum", 1, lag_months=3)
        unit = (region_id(0), 2004, 3)
        frame = F.build_frame((spec,), self.sources, [unit], self.ppy)
        self.assertEqual(frame.row(unit)[0], float(F.month_index(2004, 3)))

    def test_same_period_mean_uses_prior_years_only(self):
        unit = (region_id(0), 2004, 3)
        frame = F.build_frame(self.specs[3:4], self.sources, [unit], self.ppy)
        # Jul+Aug+Sep of 1994..2003, each sum = 3*Aug of that year.
        expected = sum(3 * F.month_index(y, 8) for y in range(1994, 2004)) / 10
        self.assertEqual(frame.row(unit)[0], expected)

    def test_missing_values_are_nan_never_zero(self):
        missing = {F.month_index(2004, 5)}  # the last month before the cutoff
        sources = {"era5": FakeSeries(missing=missing), "gazetteer": FakeStatic()}
        unit = (region_id(0), 2004, 3)
        frame = F.build_frame(self.specs, sources, [unit], self.ppy)
        row = dict(zip(frame.columns, frame.row(unit)))
        self.assertTrue(math.isnan(row["p1"]))
        self.assertTrue(math.isnan(row["p3"]))
        self.assertEqual(frame.missing_share()["p1"], 1.0)

    def test_frame_takes_units_and_ignores_labels(self):
        """Shuffling every holdout label cannot move a single feature value."""
        panel = make_panel(contract=self.contract, n_regions=3)
        frame = F.build_frame(self.specs, self.sources, panel.units, self.ppy)
        flipped = [1 - y for y in panel.labels]
        self.assertNotEqual(list(panel.labels), flipped)
        again = F.build_frame(self.specs, self.sources, panel.units, self.ppy)
        self.assertEqual(frame.digest(), again.digest())
        self.assertEqual(len(frame), len(panel))

    def test_digest_is_stable_across_unit_order(self):
        units = [(region_id(i), y, p) for i in range(3) for y in (2000, 2001) for p in (1, 4)]
        a = F.build_frame(self.specs, self.sources, units, self.ppy)
        b = F.build_frame(self.specs, self.sources, list(reversed(units)), self.ppy)
        self.assertEqual(a.digest(), b.digest())

    def test_restrict_keeps_only_the_named_units(self):
        units = [(region_id(0), 2000, p) for p in range(1, 5)]
        frame = F.build_frame(self.specs, self.sources, units, self.ppy)
        sub = frame.restrict(units[:2])
        self.assertEqual(sub.units, tuple(units[:2]))
        self.assertEqual(sub.columns, frame.columns)

    def test_source_kind_must_match_the_spec(self):
        bad = (F.FeatureSpec("x", "gazetteer", "water_share", "trailing_sum", 1),)
        with self.assertRaises(F.FeatureAdmissionError):
            F.build_frame(bad, self.sources, [(region_id(0), 2000, 1)], self.ppy)
        bad = (F.FeatureSpec("x", "era5", "precip_mm"),)
        with self.assertRaises(F.FeatureAdmissionError):
            F.build_frame(bad, self.sources, [(region_id(0), 2000, 1)], self.ppy)

    def test_unloaded_source_and_duplicate_columns_are_refused(self):
        with self.assertRaises(F.FeatureAdmissionError):
            F.build_frame(self.specs, {"era5": FakeSeries()}, [(region_id(0), 2000, 1)], self.ppy)
        dup = (self.specs[0], self.specs[0])
        with self.assertRaises(F.FeatureAdmissionError):
            F.build_frame(dup, self.sources, [(region_id(0), 2000, 1)], self.ppy)


class TestAdmission(unittest.TestCase):
    def setUp(self):
        self.contract = make_contract()  # validate starts 2016

    def test_timeless_geometry_is_admitted(self):
        F.admit(FakeStatic(), self.contract)

    def test_series_sources_are_admitted(self):
        F.admit(FakeSeries(), self.contract)

    def test_static_layer_through_validate_start_is_refused(self):
        with self.assertRaises(F.FeatureAdmissionError):
            F.admit(FakeStatic("nri", derived_through=2016), self.contract)
        with self.assertRaises(F.FeatureAdmissionError):
            F.admit(FakeStatic("nri", derived_through=2023), self.contract)

    def test_static_layer_before_validate_start_is_admitted(self):
        F.admit(FakeStatic("climada", derived_through=2015), self.contract)

    def test_untimed_static_outside_the_allow_list_is_refused(self):
        with self.assertRaises(F.FeatureAdmissionError):
            F.admit(FakeStatic("mystery", derived_through=None), self.contract)

    def test_label_origin_source_is_refused(self):
        for key in ("noaa/storm_events/2004", "records/zz/events.csv", "emdat/zz"):
            with self.subTest(key=key):
                with self.assertRaises(F.FeatureAdmissionError):
                    F.admit(FakeStatic("gazetteer", keys=(key,)), self.contract)


class TestAudit(unittest.TestCase):
    def setUp(self):
        self.contract = make_contract()
        self.ppy = self.contract.periods_per_year
        self.sources = {"era5": FakeSeries(), "gazetteer": FakeStatic()}
        self.units = [(region_id(i), y, p) for i in range(2) for y in (2000, 2017) for p in (1, 3)]
        self.specs = (
            F.FeatureSpec("p1", "era5", "precip_mm", "trailing_sum", 1),
            F.FeatureSpec("p12", "era5", "precip_mm", "trailing_mean", 12),
            F.FeatureSpec("same5", "era5", "precip_mm", "same_period_mean", 60),
            F.FeatureSpec("water", "gazetteer", "water_share"),
        )

    def test_every_registered_transform_is_clean(self):
        frame = F.build_frame(self.specs, self.sources, self.units, self.ppy)
        audit = F.audit_frame(self.specs, self.sources, self.units, self.contract, frame)
        self.assertTrue(audit.clean, audit.format())
        self.assertEqual([f.check for f in audit.findings], ["admission", "admission", "timestamp bound", "coverage"])

    def test_a_forward_looking_transform_is_caught(self):
        def peek(series, cutoff, spec, ppy):
            # An off-by-one transform: reads the month AT the cutoff, the first
            # forbidden month. The honest build hands it a cut series, where
            # that month is simply absent (NaN); the audit hands it the
            # poisoned one, where it is POISON, and the two disagree.
            return series.value(cutoff)

        F.TRANSFORMS["peek"] = peek
        try:
            specs = (F.FeatureSpec("leak", "era5", "precip_mm", "peek", 1),)
            frame = F.build_frame(specs, self.sources, self.units, self.ppy)
            audit = F.audit_frame(specs, self.sources, self.units, self.contract, frame)
        finally:
            del F.TRANSFORMS["peek"]
        self.assertFalse(audit.clean)
        bound = [f for f in audit.findings if f.check == "timestamp bound"][0]
        self.assertFalse(bound.passed)
        self.assertIn("leak", bound.detail)

    def test_an_inadmissible_source_makes_the_audit_unclean(self):
        sources = {"era5": FakeSeries(), "nri": FakeStatic("nri", derived_through=2023)}
        specs = (F.FeatureSpec("eal", "nri", "water_share"),)
        frame = F.build_frame(specs, sources, self.units, self.ppy)
        audit = F.audit_frame(specs, sources, self.units, self.contract, frame)
        self.assertFalse(audit.clean)
        self.assertIn("2023", audit.findings[0].detail)
        self.assertIn("clean", audit.to_dict())


if __name__ == "__main__":
    unittest.main()
