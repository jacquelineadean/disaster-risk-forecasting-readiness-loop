"""The data seam: `data.build` on a synthetic snapshot, and what it records.

No network. A temporary snapshot directory is populated through the connectors'
own writers — the Census county file in its cache path, one Storm Events
extract per state-year, and a manifest that pins every one of them — so
`build` finds nothing missing and never reaches for `fetch`. The fetchers are
patched to raise, so a build that *did* try to download fails loudly.
"""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from readiness import data as data_mod
from readiness.connectors import census, nws_zones, storm_events
from readiness.connectors.base import Manifest, SourceRecord, sha256_bytes
from readiness.harness.labels import diagnose
from tests.fixtures import STATE_FIPS, make_contract, make_panel

CENSUS_TEXT = (
    "STATE|STATEFP|COUNTYFP|COUNTYNS|COUNTYNAME|CLASSFP|FUNCSTAT\n"
    "ZZ|99|001|00000001|One County|H1|A\n"
    "ZZ|99|003|00000003|Three County|H1|A\n"
    "ZZ|99|005|00000005|Five County|H1|A\n"
    "YY|98|001|00000011|Other County|H1|A\n"
)

#: A short contract keeps the synthetic snapshot to eight year files.
SPLITS = {"train": [2000, 2003], "validate": [2004, 2005], "test": [2006, 2007]}


def storm_row(**over) -> dict:
    """One extract row with every column the connector writes."""
    row = {k: "" for k in storm_events._FIELDS}
    row.update(
        EVENT_ID="1",
        YEAR="2004",
        BEGIN_YEARMONTH="200408",
        EVENT_TYPE="Flood",
        STATE="ZZ",
        STATE_FIPS=STATE_FIPS,
        CZ_TYPE="C",
        CZ_FIPS="3",
        INJURIES_DIRECT="0",
        INJURIES_INDIRECT="0",
        DEATHS_DIRECT="0",
        DEATHS_INDIRECT="0",
        DAMAGE_PROPERTY="0.00K",
        DAMAGE_CROPS="0.00K",
    )
    row.update(over)
    return row


def record(source: str, sha256: str = "0" * 64) -> SourceRecord:
    return SourceRecord(
        source=source, url=f"https://example.invalid/{source}", sha256=sha256,
        bytes=1, fetched_at="2026-01-01T00:00:00+00:00", license="public domain",
    )


def write_snapshot(root: pathlib.Path, contract, rows_by_year: dict[int, list[dict]]):
    """Lay out a pinned snapshot the way the connectors would have left it."""
    (root / "census").mkdir(parents=True)
    (root / "census" / "national_county2020.txt").write_text(CENSUS_TEXT)
    extracts = root / "storm_events"
    extracts.mkdir()
    manifest = Manifest(path=root / "manifest.json")
    # The loaders reuse a cached file only when its bytes hash to the record,
    # so the county file's record must carry the real digest of what was written.
    manifest.add(data_mod.CENSUS_KEY, record("census", sha256_bytes(CENSUS_TEXT.encode())))
    for year in contract.all_years():
        storm_events._write_extract(
            extracts / f"{STATE_FIPS}_{year}.jsonl", rows_by_year.get(year, [])
        )
        manifest.add(data_mod.storm_events_key(year), record(f"storm-{year}"))
    manifest.save()
    return manifest


def no_fetch(url, **_kw):
    raise AssertionError(f"build() tried to download {url}")


class SnapshotCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.c = make_contract(splits=SPLITS)
        self.rows = {
            2004: [
                storm_row(EVENT_ID="1", DAMAGE_PROPERTY="50.00K"),           # 99003 Q3
                storm_row(EVENT_ID="2", BEGIN_YEARMONTH="200409",
                          DAMAGE_PROPERTY="1.00M"),                          # same cell
                storm_row(EVENT_ID="3", CZ_FIPS="1", DEATHS_DIRECT="1"),      # 99001 Q3
                storm_row(EVENT_ID="4", CZ_FIPS="5"),                         # harmless
                storm_row(EVENT_ID="5", EVENT_TYPE="Hail", DAMAGE_PROPERTY="9.00M"),
            ],
            2001: [storm_row(EVENT_ID="6", YEAR="2001", BEGIN_YEARMONTH="200101",
                             CZ_FIPS="5", DAMAGE_PROPERTY="20.00K")],         # 99005 Q1
        }
        write_snapshot(self.root, self.c, self.rows)
        self.patches = [
            mock.patch.object(census, "fetch", no_fetch),
            mock.patch.object(storm_events, "fetch", no_fetch),
            mock.patch.object(nws_zones, "fetch", no_fetch),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def build(self, contract=None):
        return data_mod.build(contract or self.c, snapshot_dir=self.root)


class TestBuild(SnapshotCase):
    def test_builds_a_dense_panel_over_the_scopes_counties(self):
        ds = self.build()
        self.assertEqual([c.fips for c in ds.regions], ["99001", "99003", "99005"])
        self.assertEqual(len(ds.panel), 3 * 8 * 4)
        positives = sorted(u for u, y in ds.panel if y == 1)
        self.assertEqual(
            positives, [("99001", 2004, 3), ("99003", 2004, 3), ("99005", 2001, 1)]
        )

    def test_diagnostics_come_from_the_same_walk_as_the_panel(self):
        ds = self.build()
        d = ds.diagnostics
        self.assertEqual(d.n_events, 5)          # the Hail row is another hazard
        self.assertEqual(d.n_damaging, 4)        # two rows share one cell
        self.assertEqual(d.n_positive_units, sum(ds.panel.labels))
        events = storm_events.load_events(
            self.root / "storm_events" / f"{STATE_FIPS}_extract.jsonl"
        )
        self.assertEqual(d, diagnose(events, ds.panel.regions, ds.panel.years, self.c))

    def test_data_version_is_hashed_over_the_contracts_inputs_only(self):
        ds = self.build()
        self.assertEqual(ds.data_version, ds.manifest.digest(data_mod.input_keys(self.c)))
        # An unrelated pin must not move it.
        ds.manifest.add("noaa/storm_events/1999", record("stray"))
        self.assertEqual(ds.manifest.digest(data_mod.input_keys(self.c)), ds.data_version)
        self.assertNotEqual(ds.manifest.digest(), ds.data_version)

    def test_a_read_only_build_leaves_the_manifest_untouched(self):
        before = (self.root / "manifest.json").read_text()
        self.build()
        self.assertEqual((self.root / "manifest.json").read_text(), before)

    def test_provenance_names_the_inputs(self):
        prov = self.build().provenance()
        self.assertEqual(
            sorted(prov),
            sorted([
                "contract", "contract_digest", "data_version", "panel_digest",
                "hazard", "scope", "period", "n_regions", "years", "inputs",
            ]),
        )
        self.assertEqual(prov["inputs"], data_mod.input_keys(self.c))
        self.assertEqual(prov["years"], [2000, 2007])
        self.assertEqual(prov["n_regions"], 3)
        self.assertEqual(prov["scope"], self.c.scope_key)
        json.dumps(prov)  # it goes onto a card

    def test_an_expand_contract_needs_the_crosswalk_pinned_too(self):
        c = make_contract(hazard="heat", zone_policy="expand", splits=SPLITS)
        with self.assertRaises(AssertionError):  # it would have downloaded it
            self.build(c)


class TestPinned(SnapshotCase):
    def test_a_complete_snapshot_is_pinned(self):
        self.assertTrue(data_mod.pinned(self.c, self.root))

    def test_a_missing_extract_is_not_pinned(self):
        (self.root / "storm_events" / f"{STATE_FIPS}_2003.jsonl").unlink()
        self.assertFalse(data_mod.pinned(self.c, self.root))

    def test_bytes_without_a_manifest_record_are_not_pinned(self):
        # On disk but unrecorded: the connectors would re-fetch, so `build`
        # is not free of the network.
        manifest = Manifest.load(self.root / "manifest.json")
        del manifest.records[data_mod.storm_events_key(2006)]
        manifest.save(force=True)
        self.assertFalse(data_mod.pinned(self.c, self.root))

    def test_no_county_file_is_not_pinned(self):
        (self.root / "census" / "national_county2020.txt").unlink()
        self.assertFalse(data_mod.pinned(self.c, self.root))

    def test_an_unknown_state_is_not_pinned(self):
        c = make_contract(scope={"states": ["QQ"]}, splits=SPLITS)
        self.assertFalse(data_mod.pinned(c, self.root))

    def test_expand_requires_the_crosswalk_on_disk_and_in_the_manifest(self):
        c = make_contract(hazard="heat", zone_policy="expand", splits=SPLITS)
        self.assertFalse(data_mod.pinned(c, self.root))
        manifest = Manifest.load(self.root / "manifest.json")
        manifest.add(nws_zones.MANIFEST_KEY, record("nws"))
        manifest.save()
        self.assertFalse(data_mod.pinned(c, self.root))  # recorded, not on disk
        (self.root / "nws").mkdir()
        (self.root / "nws" / "zone_county.dbx").write_bytes(b"")
        self.assertTrue(data_mod.pinned(c, self.root))

    def test_an_empty_snapshot_dir_is_not_pinned(self):
        self.assertFalse(data_mod.pinned(self.c, self.root / "nowhere"))

    def test_state_fips_reads_the_county_file(self):
        self.assertEqual(data_mod.state_fips(self.root), {"ZZ": "99", "YY": "98"})
        self.assertEqual(data_mod.state_fips(self.root / "nowhere"), {})


class TestInputKeys(unittest.TestCase):
    def test_census_and_one_key_per_year(self):
        c = make_contract(splits=SPLITS)
        self.assertEqual(
            data_mod.input_keys(c),
            ["census/national_county2020"]
            + [f"noaa/storm_events/{y}" for y in range(2000, 2008)],
        )

    def test_the_crosswalk_only_under_expand(self):
        drop = make_contract(hazard="heat", zone_policy="drop", splits=SPLITS)
        expand = make_contract(hazard="heat", zone_policy="expand", splits=SPLITS)
        self.assertNotIn(nws_zones.MANIFEST_KEY, data_mod.input_keys(drop))
        self.assertEqual(
            data_mod.input_keys(expand),
            data_mod.input_keys(drop) + [nws_zones.MANIFEST_KEY],
        )

    def test_keys_match_what_the_connectors_pin(self):
        # The keys are literals in three connectors; this is the tripwire.
        self.assertEqual(data_mod.CENSUS_KEY, "census/national_county2020")
        self.assertEqual(data_mod.storm_events_key(2010), "noaa/storm_events/2010")
        self.assertEqual(nws_zones.MANIFEST_KEY, "nws/zone_county")


class TestSyntheticProvenance(unittest.TestCase):
    def test_provenance_of_an_injected_dataset(self):
        c = make_contract()
        panel = make_panel(contract=c, n_regions=4)
        ds = data_mod.Dataset(
            contract=c,
            panel=panel,
            regions=(),
            data_version="synthetic",
            manifest=Manifest(path=pathlib.Path("unused")),
            diagnostics=diagnose([], panel.regions, panel.years, c),
        )
        prov = ds.provenance()
        self.assertEqual(prov["panel_digest"], panel.digest())
        self.assertEqual(prov["inputs"], data_mod.input_keys(c))
        self.assertEqual(len(prov["inputs"]), 1 + 30)


if __name__ == "__main__":
    unittest.main()
