"""Label construction — the part where a bug silently corrupts every score."""

import unittest

from readiness.config import CONTRACT
from readiness.harness.labels import (
    build_panel,
    county_fips,
    is_damaging,
    parse_damage,
)
from tests.fixtures import make_event


class TestDamageParsing(unittest.TestCase):
    def test_magnitude_suffixes(self):
        self.assertEqual(parse_damage("10.00K"), 10_000.0)
        self.assertEqual(parse_damage("1.50M"), 1_500_000.0)
        self.assertEqual(parse_damage("2B"), 2_000_000_000.0)
        self.assertEqual(parse_damage("500"), 500.0)

    def test_zero_and_blank(self):
        self.assertEqual(parse_damage("0.00K"), 0.0)
        self.assertEqual(parse_damage(""), 0.0)
        self.assertEqual(parse_damage(None), 0.0)
        self.assertEqual(parse_damage("   "), 0.0)

    def test_lowercase_suffix(self):
        self.assertEqual(parse_damage("3.5k"), 3_500.0)

    def test_bare_magnitude_suffix_is_zero(self):
        # NOAA emits a lone unit letter for an otherwise-empty field; observed
        # once in ~25k Louisiana rows (DAMAGE_CROPS == "K"). No digits, no
        # amount.
        for raw in ("K", "M", " B ", "t"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_damage(raw), 0.0)

    def test_garbage_raises_rather_than_becoming_zero(self):
        # A silent zero here is a mislabelled county-quarter, which is the one
        # class of bug the harness cannot detect downstream.
        for bad in ("unknown", "12X", "1.2.3K", "-5K"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_damage(bad)


class TestDamageThreshold(unittest.TestCase):
    def test_property_at_threshold_counts(self):
        self.assertTrue(is_damaging(make_event(damage=CONTRACT.damage_property_usd_min)))

    def test_property_below_threshold_does_not(self):
        self.assertFalse(
            is_damaging(make_event(damage=CONTRACT.damage_property_usd_min - 1))
        )

    def test_casualties_count_regardless_of_property_damage(self):
        self.assertTrue(is_damaging(make_event(damage=0.0, injuries=1)))
        self.assertTrue(is_damaging(make_event(damage=0.0, deaths=1)))

    def test_harmless_event_is_not_damaging(self):
        self.assertFalse(is_damaging(make_event(damage=0.0)))


class TestCountyFips(unittest.TestCase):
    def test_pads_both_components(self):
        self.assertEqual(county_fips("22", "5"), "22005")
        self.assertEqual(county_fips("1", "1"), "01001")
        self.assertEqual(county_fips("06", "037"), "06037")


class TestPanel(unittest.TestCase):
    def setUp(self):
        self.counties = ["22001", "22003", "22005"]
        self.years = [2010, 2011]

    def test_panel_is_dense(self):
        panel = build_panel([], self.counties, self.years)
        self.assertEqual(len(panel), 3 * 2 * 4)
        self.assertEqual(sum(panel.labels), 0)
        self.assertEqual(panel.base_rate, 0.0)

    def test_damaging_event_marks_exactly_one_cell(self):
        event = make_event(year=2010, month=8, county="22003", damage=50_000)
        panel = build_panel([event], self.counties, self.years)
        self.assertEqual(sum(panel.labels), 1)
        positives = [u for u, y in panel if y == 1]
        self.assertEqual(positives, [("22003", 2010, 3)])  # August -> Q3

    def test_two_events_in_one_quarter_still_mark_one_cell(self):
        events = [
            make_event(year=2010, month=7, county="22003", damage=50_000),
            make_event(year=2010, month=9, county="22003", damage=90_000),
        ]
        panel = build_panel(events, self.counties, self.years)
        self.assertEqual(sum(panel.labels), 1)

    def test_non_hazard_event_types_are_ignored(self):
        event = make_event(event_type="Tornado", year=2010, county="22003", damage=1e6)
        panel = build_panel([event], self.counties, self.years)
        self.assertEqual(sum(panel.labels), 0)

    def test_zone_coded_rows_are_dropped(self):
        # CZ_TYPE 'Z' rows are forecast zones, which do not join to counties.
        event = make_event(year=2010, county="22003", damage=1e6)
        zoned = type(event)(**{**event.__dict__, "cz_type": "Z"})
        panel = build_panel([zoned], self.counties, self.years)
        self.assertEqual(sum(panel.labels), 0)

    def test_events_outside_the_county_universe_are_dropped(self):
        event = make_event(year=2010, county="48999", damage=1e6)
        panel = build_panel([event], self.counties, self.years)
        self.assertEqual(sum(panel.labels), 0)

    def test_quarter_boundaries(self):
        for month, quarter in ((1, 1), (3, 1), (4, 2), (6, 2), (7, 3), (9, 3), (10, 4), (12, 4)):
            with self.subTest(month=month):
                self.assertEqual(make_event(month=month).quarter, quarter)

    def test_digest_is_order_independent_and_content_sensitive(self):
        a = build_panel(
            [make_event(year=2010, month=8, county="22003", damage=50_000)],
            self.counties,
            self.years,
        )
        b = build_panel(
            [make_event(year=2010, month=8, county="22003", damage=50_000)],
            list(reversed(self.counties)),
            list(reversed(self.years)),
        )
        self.assertEqual(a.digest(), b.digest())

        c = build_panel([], self.counties, self.years)
        self.assertNotEqual(a.digest(), c.digest())

    def test_units_digest_ignores_labels(self):
        a = build_panel(
            [make_event(year=2010, month=8, county="22003", damage=50_000)],
            self.counties,
            self.years,
        )
        b = build_panel([], self.counties, self.years)
        self.assertEqual(a.units_digest(), b.units_digest())
        self.assertNotEqual(a.digest(), b.digest())

    def test_unknown_hazard_raises(self):
        with self.assertRaises(KeyError):
            build_panel([], self.counties, self.years, hazard="volcano")


if __name__ == "__main__":
    unittest.main()
