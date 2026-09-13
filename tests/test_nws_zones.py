"""The NWS zone-county crosswalk: parsing, edition discovery, and pinning."""

import pathlib
import tempfile
import unittest
from unittest import mock

from readiness.connectors import nws_zones
from readiness.connectors.base import Manifest

SAMPLE = b"""\
AA|001|XYZ|Alpha Zone|AA001|Alpha|97001|C|nw|30.0|-90.0
AA|001|XYZ|Alpha Zone|AA001|Beta|97003|C|nw|30.0|-90.0
AA|002|XYZ|Gamma Zone|AA002|Gamma|97005|C|nw|30.0|-90.0
BB|001|XYZ|Delta Zone|BB001|Delta|98001|C|nw|30.0|-90.0
garbage line
AA|abc|XYZ|Bad Zone|AAabc|Bad|9700|C|nw|30.0|-90.0
"""


class TestParse(unittest.TestCase):
    def setUp(self):
        self.cw = nws_zones.parse(SAMPLE, edition="bp01ja26.dbx")

    def test_zone_with_several_counties(self):
        self.assertEqual(self.cw.counties_for("97", "1"), ("97001", "97003"))

    def test_zone_numbers_compare_numerically_and_states_pad(self):
        self.assertEqual(self.cw.counties_for("97", "001"), ("97001", "97003"))
        self.assertEqual(self.cw.counties_for("98", "1"), ("98001",))
        self.assertEqual(self.cw.counties_for("98", "01"), ("98001",))

    def test_unknown_zone_is_empty_not_an_error(self):
        self.assertEqual(self.cw.counties_for("97", "999"), ())
        self.assertEqual(self.cw.counties_for("55", "1"), ())
        self.assertEqual(self.cw.counties_for("97", ""), ())
        self.assertEqual(self.cw.counties_for("xx", "1"), ())

    def test_sizes(self):
        self.assertEqual(len(self.cw), 3)
        self.assertEqual(self.cw.n_states, 2)
        self.assertEqual(self.cw.edition, "bp01ja26.dbx")

    def test_empty_file_is_an_error(self):
        with self.assertRaises(nws_zones.ConnectorError):
            nws_zones.parse(b"nothing|here\n")


class TestEditions(unittest.TestCase):
    def test_edition_dates(self):
        self.assertEqual(nws_zones.edition_date("bp16ap26.dbx"), (2026, 4, 16))
        self.assertEqual(nws_zones.edition_date("bp18mr25.dbx"), (2025, 3, 18))
        self.assertEqual(nws_zones.edition_date("nonsense"), (0, 0, 0))

    def test_discover_picks_the_newest(self):
        page = (
            '<a href="/source/gis/Shapefiles/County/bp18mr25.dbx">old</a>'
            '<a href="/source/gis/Shapefiles/County/bp16ap26.dbx">new</a>'
            '<a href="/source/gis/Shapefiles/County/bp05fe26.dbx">middle</a>'
        ).encode()
        with mock.patch.object(nws_zones, "fetch", lambda _url, **_kw: page):
            self.assertTrue(nws_zones.discover_current().endswith("bp16ap26.dbx"))

    def test_discover_fails_loudly_when_the_page_has_moved(self):
        with mock.patch.object(nws_zones, "fetch", lambda _url, **_kw: b"<html>moved</html>"):
            with self.assertRaises(nws_zones.ConnectorError):
                nws_zones.discover_current()


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.manifest = Manifest(path=self.dir / "manifest.json")
        self.fetches: list[str] = []

        def fake_fetch(url, **_kw):
            self.fetches.append(url)
            if url == nws_zones.INDEX_URL:
                return b'<a href="/source/gis/Shapefiles/County/bp16ap26.dbx">x</a>'
            return SAMPLE

        self.patch = mock.patch.object(nws_zones, "fetch", fake_fetch)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_first_load_fetches_and_pins(self):
        cw = nws_zones.load(self.dir / "nws", self.manifest)
        self.assertEqual(cw.edition, "bp16ap26.dbx")
        self.assertIn(nws_zones.MANIFEST_KEY, self.manifest.records)
        self.assertIn("bp16ap26", self.manifest.records[nws_zones.MANIFEST_KEY].notes)
        self.assertEqual(len(self.fetches), 2)  # index, then the file

    def test_second_load_reuses_the_pinned_file(self):
        nws_zones.load(self.dir / "nws", self.manifest)
        self.fetches.clear()
        cw = nws_zones.load(self.dir / "nws", self.manifest)
        self.assertEqual(self.fetches, [])
        self.assertEqual(cw.edition, "bp16ap26.dbx")

    def test_unpinned_cache_is_refetched(self):
        nws_zones.load(self.dir / "nws", self.manifest)
        self.manifest.records.clear()  # bytes on disk, provenance gone
        self.fetches.clear()
        nws_zones.load(self.dir / "nws", self.manifest)
        self.assertEqual(len(self.fetches), 2)


if __name__ == "__main__":
    unittest.main()
