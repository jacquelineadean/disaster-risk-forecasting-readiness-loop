"""Label construction — the part where a bug silently corrupts every score."""

import unittest

from readiness.harness.labels import (
    build_panel,
    county_fips,
    diagnose,
    is_damaging,
    parse_damage,
)
from tests.fixtures import make_contract, make_event


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
        # NOAA emits a lone unit letter for an otherwise-empty field. No
        # digits, no amount.
        for raw in ("K", "M", " B ", "t"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_damage(raw), 0.0)

    def test_garbage_raises_rather_than_becoming_zero(self):
        # A silent zero here is a mislabelled region-period, which is the one
        # class of bug the harness cannot detect downstream.
        for bad in ("unknown", "12X", "1.2.3K", "-5K"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_damage(bad)


class TestDamageThreshold(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()

    def test_property_at_threshold_counts(self):
        self.assertTrue(is_damaging(make_event(damage=self.c.damage_property_usd_min), self.c))

    def test_property_below_threshold_does_not(self):
        self.assertFalse(
            is_damaging(make_event(damage=self.c.damage_property_usd_min - 1), self.c)
        )

    def test_casualties_count_regardless_of_property_damage(self):
        self.assertTrue(is_damaging(make_event(damage=0.0, injuries=1), self.c))
        self.assertTrue(is_damaging(make_event(damage=0.0, deaths=1), self.c))

    def test_harmless_event_is_not_damaging(self):
        self.assertFalse(is_damaging(make_event(damage=0.0), self.c))

    def test_threshold_comes_from_the_contract(self):
        strict = make_contract(damaging={"property_usd_min": 1_000_000.0})
        self.assertFalse(is_damaging(make_event(damage=50_000.0), strict))
        no_casualties = make_contract(damaging={"count_casualties": False})
        self.assertFalse(is_damaging(make_event(injuries=3), no_casualties))


class TestCountyFips(unittest.TestCase):
    def test_pads_both_components(self):
        self.assertEqual(county_fips("48", "5"), "48005")
        self.assertEqual(county_fips("1", "1"), "01001")
        self.assertEqual(county_fips("06", "037"), "06037")


class TestPeriods(unittest.TestCase):
    def test_quarter_boundaries(self):
        for month, quarter in ((1, 1), (3, 1), (4, 2), (6, 2), (7, 3), (9, 3), (10, 4), (12, 4)):
            with self.subTest(month=month):
                self.assertEqual(make_event(month=month).period_index(4), quarter)

    def test_month_and_year_periods(self):
        for month in range(1, 13):
            self.assertEqual(make_event(month=month).period_index(12), month)
            self.assertEqual(make_event(month=month).period_index(1), 1)

    def test_unit_for_uses_the_period(self):
        e = make_event(year=2010, month=8, county="99003")
        self.assertEqual(e.unit_for(4), ("99003", 2010, 3))
        self.assertEqual(e.unit_for(12), ("99003", 2010, 8))


class TestPanel(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.regions = ["99001", "99003", "99005"]
        self.years = [2010, 2011]

    def build(self, events, contract=None, regions=None, years=None):
        return build_panel(
            events, regions or self.regions, years or self.years, contract or self.c
        )

    def test_panel_is_dense(self):
        panel = self.build([])
        self.assertEqual(len(panel), 3 * 2 * 4)
        self.assertEqual(sum(panel.labels), 0)
        self.assertEqual(panel.base_rate, 0.0)
        self.assertEqual(panel.regions, tuple(self.regions))

    def test_panel_density_follows_the_period(self):
        self.assertEqual(len(self.build([], make_contract(period="month"))), 3 * 2 * 12)
        self.assertEqual(len(self.build([], make_contract(period="year"))), 3 * 2 * 1)

    def test_damaging_event_marks_exactly_one_cell(self):
        event = make_event(year=2010, month=8, county="99003", damage=50_000)
        panel = self.build([event])
        self.assertEqual(sum(panel.labels), 1)
        positives = [u for u, y in panel if y == 1]
        self.assertEqual(positives, [("99003", 2010, 3)])  # August -> Q3

    def test_monthly_contract_marks_the_month(self):
        event = make_event(year=2010, month=8, county="99003", damage=50_000)
        panel = self.build([event], make_contract(period="month"))
        positives = [u for u, y in panel if y == 1]
        self.assertEqual(positives, [("99003", 2010, 8)])

    def test_two_events_in_one_period_still_mark_one_cell(self):
        events = [
            make_event(year=2010, month=7, county="99003", damage=50_000),
            make_event(year=2010, month=9, county="99003", damage=90_000),
        ]
        self.assertEqual(sum(self.build(events).labels), 1)

    def test_only_the_contracts_event_types_count(self):
        event = make_event(event_type="Tornado", year=2010, county="99003", damage=1e6)
        self.assertEqual(sum(self.build([event]).labels), 0)
        tornado = make_contract(hazard="tornado")
        self.assertEqual(sum(self.build([event], tornado).labels), 1)

    def test_zone_coded_rows_are_dropped(self):
        # CZ_TYPE 'Z' rows are forecast zones, which do not join to counties.
        event = make_event(year=2010, county="99003", damage=1e6, cz_type="Z")
        self.assertEqual(sum(self.build([event]).labels), 0)

    def test_events_outside_the_region_universe_are_dropped(self):
        event = make_event(year=2010, county="98999", damage=1e6)
        self.assertEqual(sum(self.build([event]).labels), 0)

    def test_panel_carries_the_contracts_identity(self):
        panel = self.build([])
        self.assertEqual(panel.hazard, self.c.hazard)
        self.assertEqual(panel.scope, self.c.scope_key)
        self.assertEqual(panel.period, self.c.period)

    def test_digest_is_order_independent_and_content_sensitive(self):
        a = self.build([make_event(year=2010, month=8, county="99003", damage=50_000)])
        b = build_panel(
            [make_event(year=2010, month=8, county="99003", damage=50_000)],
            list(reversed(self.regions)),
            list(reversed(self.years)),
            self.c,
        )
        self.assertEqual(a.digest(), b.digest())
        self.assertNotEqual(a.digest(), self.build([]).digest())

    def test_digest_distinguishes_contracts_over_identical_units(self):
        a = self.build([])
        b = self.build([], make_contract(hazard="tornado"))
        self.assertNotEqual(a.digest(), b.digest())

    def test_units_digest_ignores_labels(self):
        a = self.build([make_event(year=2010, month=8, county="99003", damage=50_000)])
        b = self.build([])
        self.assertEqual(a.units_digest(), b.units_digest())
        self.assertNotEqual(a.digest(), b.digest())


class TestDiagnostics(unittest.TestCase):
    def test_counts_where_events_went(self):
        c = make_contract()
        events = [
            make_event(year=2010, month=8, county="99003", damage=50_000),   # positive
            make_event(year=2010, month=8, county="99003", damage=0),        # harmless
            make_event(year=2010, month=2, county="99003", damage=1e6, cz_type="Z"),  # zone
            make_event(year=2010, month=2, county="98001", damage=1e6),      # outside
            make_event(year=1999, month=2, county="99003", damage=1e6),      # wrong year
            make_event(event_type="Hail", year=2010, county="99003", damage=1e6),  # other hazard
        ]
        d = diagnose(events, ["99001", "99003"], [2010, 2011], c)
        self.assertEqual(d.n_events, 5)
        self.assertEqual(d.n_in_years, 4)
        self.assertEqual(d.n_county_coded, 2)
        self.assertEqual(d.n_zone_coded, 1)
        self.assertEqual(d.n_outside_universe, 1)
        self.assertEqual(d.n_damaging, 1)
        self.assertEqual(d.n_positive_units, 1)
        self.assertNotIn("WARNING", d.format())

    def test_warns_when_a_hazard_is_mostly_zone_coded(self):
        c = make_contract(hazard="heat")
        events = [
            make_event(event_type="Heat", year=2010, county="99003", cz_type="Z", deaths=1)
            for _ in range(9)
        ] + [make_event(event_type="Heat", year=2010, county="99003", deaths=1)]
        d = diagnose(events, ["99003"], [2010], c)
        self.assertGreaterEqual(d.zone_share, 0.5)
        self.assertIn("WARNING", d.format())


if __name__ == "__main__":
    unittest.main()


class TestZonePolicy(unittest.TestCase):
    """Zone-coded events: dropped by default, expanded through the crosswalk on request."""

    from readiness.connectors import nws_zones as _nws

    CROSSWALK = _nws.parse(
        b"AA|001|X|Zone One|AA001|A|99001|C|nw|0|0\n"
        b"AA|001|X|Zone One|AA001|B|99003|C|nw|0|0\n"
        b"AA|002|X|Zone Two|AA002|C|99005|C|nw|0|0\n",
        edition="test",
    )
    REGIONS = ["99001", "99003", "99005"]
    YEARS = [2010]

    def zone_event(self, zone="1", **kw):
        e = make_event(event_type="Heat", year=2010, month=7, county="99" + zone.zfill(3),
                       cz_type="Z", deaths=1, **kw)
        return e

    def test_drop_policy_ignores_zone_events(self):
        c = make_contract(hazard="heat", zone_policy="drop")
        panel = build_panel([self.zone_event()], self.REGIONS, self.YEARS, c)
        self.assertEqual(sum(panel.labels), 0)

    def test_expand_policy_marks_every_county_in_the_zone(self):
        c = make_contract(hazard="heat", zone_policy="expand")
        panel = build_panel([self.zone_event()], self.REGIONS, self.YEARS, c, self.CROSSWALK)
        positives = sorted(u for u, y in panel if y == 1)
        self.assertEqual(positives, [("99001", 2010, 3), ("99003", 2010, 3)])

    def test_expand_respects_the_region_universe(self):
        c = make_contract(hazard="heat", zone_policy="expand")
        panel = build_panel([self.zone_event()], ["99001"], self.YEARS, c, self.CROSSWALK)
        self.assertEqual([u for u, y in panel if y == 1], [("99001", 2010, 3)])

    def test_expand_without_a_crosswalk_refuses(self):
        c = make_contract(hazard="heat", zone_policy="expand")
        with self.assertRaises(ValueError):
            build_panel([self.zone_event()], self.REGIONS, self.YEARS, c)
        with self.assertRaises(ValueError):
            diagnose([self.zone_event()], self.REGIONS, self.YEARS, c)

    def test_harmless_zone_events_do_not_expand(self):
        c = make_contract(hazard="heat", zone_policy="expand")
        harmless = make_event(event_type="Heat", year=2010, month=7, county="99001",
                              cz_type="Z", deaths=0)
        panel = build_panel([harmless], self.REGIONS, self.YEARS, c, self.CROSSWALK)
        self.assertEqual(sum(panel.labels), 0)

    def test_unmapped_zones_are_counted_not_guessed(self):
        c = make_contract(hazard="heat", zone_policy="expand")
        events = [self.zone_event("1"), self.zone_event("2"), self.zone_event("9")]
        panel = build_panel(events, self.REGIONS, self.YEARS, c, self.CROSSWALK)
        self.assertEqual(sum(panel.labels), 3)  # zone 1 -> 2 counties, zone 2 -> 1
        d = diagnose(events, self.REGIONS, self.YEARS, c, self.CROSSWALK)
        self.assertEqual(d.n_zone_coded, 3)
        self.assertEqual(d.n_zone_expanded, 2)
        self.assertEqual(d.n_zone_unmapped, 1)
        self.assertEqual(d.n_damaging, 2)
        self.assertEqual(d.n_positive_units, 3)
        self.assertEqual(d.crosswalk_edition, "test")
        self.assertIn("expanded via NWS crosswalk", d.format())
        self.assertIn("unmapped", d.format())
        self.assertNotIn("WARNING: 100%", d.format())

    def test_mostly_unmapped_zones_warn(self):
        c = make_contract(hazard="heat", zone_policy="expand")
        events = [self.zone_event("7"), self.zone_event("8"), self.zone_event("1")]
        d = diagnose(events, self.REGIONS, self.YEARS, c, self.CROSSWALK)
        self.assertGreaterEqual(d.unmapped_share, 0.25)
        self.assertIn("renumbered", d.format())

    def test_county_coded_events_are_unaffected_by_policy(self):
        for policy in ("drop", "expand"):
            with self.subTest(policy=policy):
                c = make_contract(hazard="heat", zone_policy=policy)
                county = make_event(event_type="Heat", year=2010, month=7, county="99005",
                                    deaths=1)
                panel = build_panel([county], self.REGIONS, self.YEARS, c, self.CROSSWALK)
                self.assertEqual([u for u, y in panel if y == 1], [("99005", 2010, 3)])

    def test_zone_policy_changes_the_panel_digest(self):
        drop = make_contract(hazard="heat", zone_policy="drop")
        expand = make_contract(hazard="heat", zone_policy="expand")
        a = build_panel([], self.REGIONS, self.YEARS, drop)
        b = build_panel([], self.REGIONS, self.YEARS, expand, self.CROSSWALK)
        # Same units, same (empty) labels — the digest header is per contract
        # hazard/scope/period, so the policy shows up on the contract digest
        # instead, which is what the card records.
        self.assertEqual(a.units_digest(), b.units_digest())
        self.assertNotEqual(drop.digest(), expand.digest())
