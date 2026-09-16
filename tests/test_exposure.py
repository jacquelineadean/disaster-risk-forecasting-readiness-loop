"""The exposure layer: rollup arithmetic, the county-only row, the spot-check.

Report §7 says to publish county aggregates only; the dataclass test here is
that sentence as a tripwire. The spot-check tests cover the exit rule the plan
fixed: rows outside the band do not count toward the ten, and the ten must
come from three states.
"""

import dataclasses
import json
import pathlib
import tempfile
import unittest

from readiness import config
from readiness.connectors import usa_structures
from readiness.connectors.base import ConnectorError, Manifest
from readiness.exposure import CLASSES, UNCLASSIFIED, classify
from readiness.exposure import spotcheck
from readiness.exposure.occupancy import class_names
from readiness.exposure.table import CountyExposure, ExposureError, ExposureTable
from tests.test_connectors import FakeLayer, PAGES

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLE = REPO_ROOT / "tests" / "data" / "usa_structures_sample.jsonl"
EXPECTED_DIR = REPO_ROOT / "exposure_expected"


def sample_rows() -> list[dict]:
    return [json.loads(ln) for ln in SAMPLE.read_text().splitlines() if ln.strip()]


class TestClassify(unittest.TestCase):
    def test_rollup_by_declared_classes(self):
        self.assertEqual(classify("Residential", "Single Family Dwelling"), "residential")
        self.assertEqual(classify("Residential", "Multi - Family Dwelling"), "residential")
        self.assertEqual(classify("Education", "Grade Schools"), "school")
        self.assertEqual(classify("Education", "Colleges/Universities"), "education")
        self.assertEqual(classify("Commercial", "Hospital"), "hospital")
        self.assertEqual(classify("Commercial", "Medical Office/Clinic"), "medical")
        self.assertEqual(classify("Commercial", "Retail Trade"), "commercial")
        self.assertEqual(classify("Industrial", "Heavy"), "industrial")
        self.assertEqual(classify("Government", "Emergency Response"), "government")
        self.assertEqual(classify("Agriculture", "Agriculture"), "agriculture")
        self.assertEqual(classify("Utility and Misc", ""), "utility")
        self.assertEqual(classify("Assembly", "Church"), "other")

    def test_matching_is_case_and_spacing_insensitive(self):
        self.assertEqual(classify("RESIDENTIAL", "single family dwelling"), "residential")
        self.assertEqual(classify(" residential ", None), "residential")

    def test_unknown_values_are_unclassified_not_dropped(self):
        self.assertEqual(classify("Weird", "Spaceport"), UNCLASSIFIED)
        self.assertEqual(classify("Unclassified", ""), UNCLASSIFIED)
        self.assertEqual(classify(None, None), UNCLASSIFIED)

    def test_specific_classes_precede_the_broad_ones(self):
        order = list(CLASSES)
        self.assertLess(order.index("school"), order.index("education"))
        self.assertLess(order.index("hospital"), order.index("commercial"))
        self.assertLess(order.index("medical"), order.index("commercial"))
        self.assertEqual(class_names(), (*CLASSES, UNCLASSIFIED))


class TestFromCounts(unittest.TestCase):
    def setUp(self):
        self.table = ExposureTable.from_counts(sample_rows(), 2023, "fema/usa_structures/99")

    def test_unknown_values_counted_in_total_and_reported(self):
        row = self.table.for_county("99003")
        self.assertEqual(row.total, 125)                      # 120 + 5 unknown
        self.assertEqual(row.by_class["residential"], 120)
        self.assertEqual(row.by_class[UNCLASSIFIED], 5)
        self.assertAlmostEqual(row.unclassified_share, 5 / 125)

    def test_totals_and_classes(self):
        row = self.table.for_county("99001")
        self.assertEqual(row.total, 306)
        self.assertEqual(row.by_class["hospital"], 2)
        self.assertEqual(row.by_class["school"], 4)
        self.assertEqual(row.by_class["residential"], 300)
        self.assertEqual(row.unclassified_share, 0.0)
        self.assertEqual(sorted(row.by_class), sorted(class_names()))
        self.assertEqual((row.vintage, row.source_key), (2023, "fema/usa_structures/99"))
        self.assertIsNone(self.table.for_county("00000"))
        self.assertEqual(len(self.table), 2)

    def test_the_row_has_no_sub_county_field(self):
        names = [f.name for f in dataclasses.fields(CountyExposure)]
        self.assertEqual(
            names,
            ["fips", "total", "by_class", "unclassified_share", "vintage", "source_key"],
        )
        for name in names:
            for word in ("tract", "block", "parcel", "point", "address", "lat", "lon", "geom"):
                self.assertNotIn(word, name)
        self.assertEqual(sorted(self.table.for_county("99001").to_dict()), sorted(names))

    def test_digest_is_content_addressed(self):
        same = ExposureTable.from_counts(sample_rows(), 2023, "fema/usa_structures/99")
        self.assertEqual(self.table.digest(), same.digest())
        self.assertEqual(len(self.table.digest()), 16)
        later = ExposureTable.from_counts(sample_rows(), 2024, "fema/usa_structures/99")
        self.assertNotEqual(self.table.digest(), later.digest())
        rows = sample_rows()
        rows[0]["n"] += 1
        self.assertNotEqual(
            self.table.digest(),
            ExposureTable.from_counts(rows, 2023, "fema/usa_structures/99").digest(),
        )

    def test_totals_summary_and_vintage(self):
        totals = self.table.totals()
        self.assertEqual(totals["99001"]["total"], 306.0)
        self.assertEqual(totals["99001"]["hospital"], 2.0)
        self.assertEqual(self.table.vintage, 2023)
        self.assertEqual(self.table.source_keys, ("fema/usa_structures/99",))
        text = self.table.summary()
        self.assertIn("2 counties, 431 structures", text)
        self.assertIn("unclassified", text)
        self.assertEqual(ExposureTable().summary(), "exposure: no counties")
        with self.assertRaises(ExposureError):
            ExposureTable().vintage

    def test_merge_refuses_a_county_in_two_extracts(self):
        with self.assertRaises(ExposureError):
            self.table.merge(self.table)


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.manifest = Manifest(path=self.dir / "manifest.json")
        self.paths = usa_structures.snapshot(
            ["99"], self.dir, self.manifest, session=FakeLayer(PAGES), page=3
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_loads_the_pinned_extract(self):
        table = ExposureTable.load(self.dir, self.manifest, ["99"])
        self.assertEqual(sorted(table.rows), ["99001", "99003"])
        self.assertEqual(table.for_county("99001").total, 306)
        self.assertEqual(table.vintage, 2023)
        self.assertEqual(table.source_keys, ("fema/usa_structures/99",))

    def test_refuses_a_tampered_extract(self):
        path = self.paths[0]
        path.write_text(path.read_text().replace('"n":300', '"n":301'))
        with self.assertRaises(ConnectorError):
            ExposureTable.load(self.dir, self.manifest, ["99"])

    def test_refuses_an_unpinned_state(self):
        with self.assertRaises(ExposureError):
            ExposureTable.load(self.dir, self.manifest, ["98"])


# ---------------------------------------------------------------------------
# Spot-check
# ---------------------------------------------------------------------------


def count(fips: str, n: int, definition: str = "improved_parcels") -> spotcheck.AssessorCount:
    return spotcheck.AssessorCount(
        fips, n, definition, "https://example.invalid/assessor", "2026-09-01", ""
    )


def table_with(totals: dict[str, int]) -> ExposureTable:
    rows = [
        {"fips": fips, "occ_cls": "Residential", "prim_occ": "Single Family Dwelling", "n": n}
        for fips, n in totals.items()
    ]
    return ExposureTable.from_counts(rows, 2023, "fema/usa_structures/test")


class TestSpotCheck(unittest.TestCase):
    def test_ratio_bounds(self):
        low, high = config.EXPOSURE_SPOTCHECK_RATIO
        self.assertEqual((low, high), (0.67, 1.5))
        table = table_with({"01001": 150, "01003": 200, "01005": 50})
        checks = spotcheck.run(
            table, [count("01001", 100), count("01003", 100), count("01005", 100)]
        )
        self.assertEqual([c.ratio for c in checks], [1.5, 2.0, 0.5])
        self.assertEqual([c.within for c in checks], [True, False, False])
        self.assertEqual(checks[0].state, "01")
        self.assertEqual((checks[0].ours, checks[0].assessor), (150, 100))
        self.assertEqual(checks[0].definition, "improved_parcels")

    def test_a_county_we_do_not_hold_is_outside_not_zero(self):
        checks = spotcheck.run(table_with({"01001": 100}), [count("48001", 100)])
        self.assertIsNone(checks[0].ours)
        self.assertIsNone(checks[0].ratio)
        self.assertFalse(checks[0].within)
        self.assertIn("not pinned", checks[0].format())

    def test_out_of_bounds_rows_do_not_count_toward_ten(self):
        # Nine in-band counties across three states plus one out of band:
        # ten rows, but only nine count.
        fips = [f"{st}{i:03d}" for st in ("01", "06", "48") for i in (1, 3, 5)]
        totals = {f: 100 for f in fips}
        totals["12001"] = 300
        checks = spotcheck.run(table_with(totals), [count(f, 100) for f in totals])
        s = spotcheck.summary(checks)
        self.assertEqual((s.n_counties, s.n_within, s.n_states_within), (10, 9, 3))
        self.assertFalse(s.within_bounds)
        totals["12001"] = 120
        checks = spotcheck.run(table_with(totals), [count(f, 100) for f in totals])
        s = spotcheck.summary(checks)
        self.assertEqual((s.n_within, s.n_states_within), (10, 4))
        self.assertTrue(s.within_bounds)

    def test_ten_counties_from_two_states_are_not_enough(self):
        fips = [f"{st}{i:03d}" for st in ("01", "06") for i in (1, 3, 5, 7, 9)]
        checks = spotcheck.run(
            table_with({f: 100 for f in fips}), [count(f, 100) for f in fips]
        )
        s = spotcheck.summary(checks)
        self.assertEqual((s.n_within, s.n_states_within), (10, 2))
        self.assertFalse(s.within_bounds)

    def test_format_prints_every_ratio(self):
        table = table_with({"01001": 150, "01003": 200})
        checks = spotcheck.run(table, [count("01001", 100), count("01003", 100)])
        text = spotcheck.format(checks)
        self.assertIn("1.50", text)
        self.assertIn("2.00", text)
        self.assertIn("OUTSIDE", text)
        self.assertIn("[0.67, 1.5]", text)
        self.assertIn("NOT YET", text)
        self.assertIn("no assessor counts", spotcheck.format([]))


class TestAssessorCountsFile(unittest.TestCase):
    def write(self, text: str) -> pathlib.Path:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = pathlib.Path(self.tmp.name) / "assessor_counts.csv"
        path.write_text(text)
        return path

    header = ",".join(spotcheck.COLUMNS) + "\n"

    def test_the_committed_file_is_header_only_and_loads(self):
        path = EXPECTED_DIR / "assessor_counts.csv"
        self.assertEqual(path.read_text().strip(), ",".join(spotcheck.COLUMNS))
        self.assertEqual(spotcheck.load(path), [])
        readme = (EXPECTED_DIR / "README.md").read_text()
        for phrase in ("assessor", "retrieval date", "ten counties", "three states"):
            self.assertIn(phrase, readme)

    def test_a_good_row_loads(self):
        path = self.write(
            self.header
            + "22071,12345,improved_parcels,https://example.invalid/a,2026-09-01,note\n"
        )
        rows = spotcheck.load(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].fips, "22071")
        self.assertEqual(rows[0].assessor_count, 12345)
        self.assertEqual(rows[0].count_definition, "improved_parcels")
        self.assertEqual(rows[0].retrieved_on, "2026-09-01")

    def test_a_bad_count_definition_is_refused(self):
        path = self.write(
            self.header + "22071,12345,buildings,https://example.invalid/a,2026-09-01,\n"
        )
        with self.assertRaises(spotcheck.SpotCheckError):
            spotcheck.load(path)

    def test_other_malformed_rows_are_refused(self):
        bad = [
            "2271,12345,structures,https://example.invalid/a,2026-09-01,",     # fips
            "22071,-1,structures,https://example.invalid/a,2026-09-01,",       # count
            "22071,12345,structures,ftp://example.invalid/a,2026-09-01,",      # url
            "22071,12345,structures,https://example.invalid/a,September,",     # date
        ]
        for line in bad:
            with self.subTest(line=line), self.assertRaises(spotcheck.SpotCheckError):
                spotcheck.load(self.write(self.header + line + "\n"))
        twice = "22071,1,structures,https://example.invalid/a,2026-09-01,\n"
        with self.assertRaises(spotcheck.SpotCheckError):
            spotcheck.load(self.write(self.header + twice + twice))

    def test_wrong_columns_are_refused(self):
        with self.assertRaises(spotcheck.SpotCheckError):
            spotcheck.load(self.write("fips,count\n"))


if __name__ == "__main__":
    unittest.main()
