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
from readiness.connectors import (
    census,
    climada_layer,
    connector_for_key,
    gazetteer,
    geoboundaries,
    nri,
    nws_zones,
    open_meteo,
    storm_events,
)
from readiness.connectors.base import ConnectorError, Manifest, SourceRecord, sha256_bytes
from readiness.harness import features as F
from readiness.harness.labels import diagnose
from tests.fixtures import (
    STATE_FIPS,
    make_contract,
    make_geojson,
    make_panel,
    make_pilot_contract,
    make_records_csv,
    record_row,
    shape_id,
)

DATA = pathlib.Path(__file__).resolve().parent / "data"

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




class NoSession:
    """Stands in for the Open-Meteo session: any request is a test failure."""

    def get(self, url):
        raise AssertionError(f"build() tried to download {url}")


def era5_line(fips: str, first_year: int, last_year: int) -> dict:
    n = 12 * (last_year - first_year + 1)
    return {
        "id": fips,
        "elevation_m": 10.0 + int(fips[-3:]),
        "month0": F.month_index(first_year, 1),
        "precip_mm": [float(i % 7) for i in range(n)],
        "tmean_c": [15.0 + (i % 12) for i in range(n)],
    }


class FeatureSnapshotCase(SnapshotCase):
    """The Phase 0 snapshot plus a pinned Gazetteer and a pinned ERA5 extract."""

    def setUp(self):
        super().setUp()
        manifest = Manifest.load(self.root / "manifest.json")
        gaz = (DATA / "gazetteer_sample.txt").read_bytes()
        (self.root / "census" / gazetteer.CACHE_NAME).write_bytes(gaz)
        manifest.add(gazetteer.MANIFEST_KEY, record("gazetteer", sha256_bytes(gaz)))
        # The extract the connector would have written for this scope: every
        # county of state 99, from two years before the contract's first year
        # (the first period's trailing twelve months reach back that far).
        self.extract = open_meteo.extract_path(self.root, STATE_FIPS)
        self.extract.parent.mkdir()
        lines = [era5_line(f, 1998, 2007) for f in ("99001", "99003", "99005")]
        self.extract.write_text("".join(open_meteo._dumps(ln) for ln in lines))
        self.era5_key = open_meteo.manifest_key(STATE_FIPS)
        manifest.add(
            self.era5_key, record("era5", sha256_bytes(self.extract.read_bytes()))
        )
        manifest.save()
        more = [
            mock.patch.object(gazetteer, "fetch", no_fetch),
            mock.patch.object(nri, "fetch", no_fetch),
            mock.patch.object(open_meteo, "DEFAULT_SESSION", NoSession()),
        ]
        for p in more:
            p.start()
        self.patches.extend(more)

    def build(self, contract=None, **kw):
        return data_mod.build(contract or self.c, snapshot_dir=self.root, **kw)

    def repin(self):
        """Pin the extract's current bytes, as an honest re-pull would."""
        manifest = Manifest.load(self.root / "manifest.json")
        manifest.add(
            self.era5_key, record("era5", sha256_bytes(self.extract.read_bytes()))
        )
        manifest.save(force=True)


class TestBuildWithFeatures(FeatureSnapshotCase):
    def test_terrain_loads_the_gazetteer_and_the_pinned_elevation(self):
        ds = self.build(features=("terrain",))
        self.assertEqual(sorted(ds.sources), ["elevation", "gazetteer"])
        for src in ds.sources.values():
            F.admit(src, self.c)
        water = ds.sources["gazetteer"].static("99001")["water_share"]
        self.assertAlmostEqual(water, 0.2)
        self.assertEqual(ds.sources["elevation"].static("99003"), {"elevation_m": 13.0})
        self.assertEqual(ds.feature_inputs, (gazetteer.MANIFEST_KEY, self.era5_key))

    def test_feature_version_is_separate_from_and_stable_beside_data_version(self):
        plain = self.build()
        ds = self.build(features=("terrain",))
        again = self.build(features=("terrain",))
        self.assertEqual(ds.data_version, plain.data_version)  # features never move it
        self.assertNotEqual(ds.feature_version, ds.data_version)
        self.assertEqual(ds.feature_version, again.feature_version)
        self.assertEqual(ds.feature_version, ds.manifest.digest(ds.feature_inputs))
        self.assertEqual(plain.feature_version, "")
        self.assertEqual(plain.feature_inputs, ())
        self.assertEqual(plain.sources, {})

    def test_provenance_names_the_features_only_when_loaded(self):
        plain = self.build().provenance()
        self.assertNotIn("feature_version", plain)
        self.assertNotIn("feature_inputs", plain)
        prov = self.build(features=("terrain",)).provenance()
        added = sorted(set(prov) - set(plain))
        self.assertEqual(added, ["feature_inputs", "feature_version"])
        self.assertEqual(prov["feature_inputs"], [gazetteer.MANIFEST_KEY, self.era5_key])
        self.assertEqual(prov["data_version"], plain["data_version"])
        json.dumps(prov)

    def test_era5_reads_the_pinned_extract_without_fetching(self):
        ds = self.build(features=("era5",))
        self.assertEqual(sorted(ds.sources), ["elevation", "era5"])
        s = ds.sources["era5"].series("99005", "precip_mm")
        self.assertEqual(s.start, F.month_index(1998, 1))
        self.assertEqual(len(s.values), 10 * 12)
        F.admit(ds.sources["era5"], self.c)

    def test_an_extract_that_stops_two_years_short_is_completed(self):
        # The lookback moved from one year to two; an extract pulled under the
        # old rule no longer covers the first period's trailing year, so it is
        # fetched again rather than quietly reused.
        lines = [era5_line(f, 1999, 2007) for f in ("99001", "99003", "99005")]
        self.extract.write_text("".join(open_meteo._dumps(ln) for ln in lines))
        self.repin()
        with self.assertRaises(AssertionError) as ctx:
            self.build(features=("era5",))
        self.assertIn("start_date=1998-01-01", str(ctx.exception))

    def test_an_extract_that_no_longer_matches_its_pin_stops_the_build(self):
        # Edited bytes are not re-pinned behind the operator's back: the data
        # version on every card built from this extract names the old ones.
        self.extract.write_text(self.extract.read_text() + "\n")
        with self.assertRaises(ConnectorError) as ctx:
            self.build(features=("era5",))
        message = str(ctx.exception)
        self.assertIn("does not match its manifest record", message)
        self.assertIn("refresh", message)

    def test_a_county_missing_from_the_extract_means_a_download(self):
        lines = [era5_line(f, 1998, 2007) for f in ("99001", "99003")]
        self.extract.write_text("".join(open_meteo._dumps(ln) for ln in lines))
        self.repin()
        with self.assertRaises(AssertionError):
            self.build(features=("era5",))

    def test_terrain_without_an_era5_extract_has_no_elevation(self):
        self.extract.unlink()
        ds = self.build(features=("terrain",))
        self.assertEqual(sorted(ds.sources), ["gazetteer"])
        self.assertEqual(ds.feature_inputs, (gazetteer.MANIFEST_KEY,))

    def test_terrain_ignores_an_extract_that_no_longer_matches_its_record(self):
        # Bytes that do not hash to the manifest are unpinned; `era5` would
        # re-fetch them, and `terrain` must not quietly serve them either.
        self.extract.write_text(self.extract.read_text() + "\n")
        ds = self.build(features=("terrain",))
        self.assertEqual(sorted(ds.sources), ["gazetteer"])

    def test_a_read_only_feature_build_leaves_the_manifest_untouched(self):
        before = (self.root / "manifest.json").read_text()
        self.build(features=("terrain", "era5"))
        self.assertEqual((self.root / "manifest.json").read_text(), before)

    def test_nri_loads_and_is_refused_by_the_firewall(self):
        with self.assertRaises(AssertionError):  # not pinned: it would download
            self.build(features=("nri",))
        sample = (DATA / "nri_sample.csv").read_bytes()
        (self.root / "fema").mkdir()
        (self.root / "fema" / nri.CACHE_NAME).write_bytes(sample)
        manifest = Manifest.load(self.root / "manifest.json")
        manifest.add(nri.MANIFEST_KEY, record("nri", sha256_bytes(sample)))
        manifest.save()
        ds = self.build(features=("nri",))
        self.assertEqual(ds.sources["nri"].derived_through, 2023)
        with self.assertRaises(F.FeatureAdmissionError):
            F.admit(ds.sources["nri"], self.c)

    def test_climada_needs_the_layer_the_tool_produces(self):
        with self.assertRaises(ConnectorError) as ctx:
            self.build(features=("climada",))
        self.assertIn("tools/climada/run_event_set.py", str(ctx.exception))
        path = climada_layer.layer_path(self.root, self.c.hazard, self.c.scope_key)
        path.parent.mkdir()
        path.write_bytes((DATA / "climada_sample.jsonl").read_bytes())
        ds = self.build(features=("climada",))
        key = climada_layer.manifest_key(self.c.hazard, self.c.scope_key)
        self.assertEqual(ds.sources["climada"].derived_through, 2015)
        self.assertEqual(ds.feature_inputs, (key,))
        self.assertIn(key, Manifest.load(self.root / "manifest.json").records)

    def test_an_unknown_feature_name_is_an_error(self):
        with self.assertRaises(ValueError):
            self.build(features=("weather",))


class TestPinnedWithFeatures(FeatureSnapshotCase):
    def test_pinned_features(self):
        self.assertTrue(data_mod.pinned(self.c, self.root))
        self.assertTrue(data_mod.pinned(self.c, self.root, features=("terrain", "era5")))
        self.assertFalse(data_mod.pinned(self.c, self.root, features=("nri",)))
        self.assertFalse(data_mod.pinned(self.c, self.root, features=("climada",)))

    def test_an_unrecorded_extract_is_not_pinned(self):
        manifest = Manifest.load(self.root / "manifest.json")
        del manifest.records[self.era5_key]
        manifest.save(force=True)
        self.assertFalse(data_mod.pinned(self.c, self.root, features=("era5",)))
        self.assertTrue(data_mod.pinned(self.c, self.root, features=("terrain",)))

    def test_a_missing_extract_is_not_pinned(self):
        self.extract.unlink()
        self.assertFalse(data_mod.pinned(self.c, self.root, features=("era5",)))

    def test_the_national_scope_reads_the_all_extract(self):
        national = make_contract(scope={"states": []}, splits=SPLITS)
        self.assertEqual(data_mod.era5_parts(national, ["98", "99"]), ["all"])
        self.assertEqual(data_mod.era5_parts(self.c, ["99"]), ["99"])



# ---------------------------------------------------------------------------
# Phase 4: the same seam, dispatched on the contract's sources
# ---------------------------------------------------------------------------


class PilotSnapshotCase(unittest.TestCase):
    """A temporary snapshot laid out for a pilot, as the connectors would leave it.

    geoBoundaries in its cache path, the partner record where `data.records_path`
    looks for it, and a manifest pinning both by their real hashes — so `build`
    finds nothing missing and never reaches for `fetch`.
    """

    hazard = "inland_flood"
    source = "national_records"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.geojson = make_geojson(n_regions=3)
        draft = make_pilot_contract(hazard=self.hazard)
        # `records_path` is country-namespaced (`records/<CC>/<basename>`), the
        # same shape as the manifest key, so two countries whose agencies both
        # call their export `records.csv` cannot collide on one path.
        self.records_file = data_mod.records_path(draft, self.root)
        sha = make_records_csv(
            self.records_file,
            draft,
            record_row(event_id="E1", start_date="2006-08-14", region_id=shape_id(0),
                       damage_usd=50_000),
            record_row(event_id="E2", start_date="2006-09-02", region_id=shape_id(0),
                       damage_usd=1_000_000),
            record_row(event_id="E3", start_date="2007-02-11", region_id=shape_id(2),
                       deaths=1),
            record_row(event_id="E4", start_date="2007-02-11", region_id=shape_id(1),
                       damage_usd=10),                       # harmless
            record_row(event_id="E5", start_date="2008-05-01", region_id=shape_id(1),
                       hazard="tornado", damage_usd=900_000),  # another hazard
        )
        self.c = make_pilot_contract(
            hazard=self.hazard, sha256=sha,
            splits={"train": [2005, 2006], "validate": [2007, 2007], "test": [2008, 2008]},
        )
        gb = geoboundaries.cache_path(self.root, "ZZ", "ADM1")
        gb.parent.mkdir(parents=True, exist_ok=True)
        gb.write_bytes(self.geojson)
        manifest = Manifest(path=self.root / "manifest.json")
        manifest.add(
            data_mod.regions_key(self.c), record("geoboundaries", sha256_bytes(self.geojson))
        )
        manifest.add(data_mod.records_key(self.c), record("records", sha))
        manifest.save()
        self.patches = [
            mock.patch.object(geoboundaries, "fetch", no_fetch),
            mock.patch.object(census, "fetch", no_fetch),
            mock.patch.object(storm_events, "fetch", no_fetch),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def build(self, contract=None):
        return data_mod.build(contract or self.c, snapshot_dir=self.root)


class TestBuildDispatchesOnSources(PilotSnapshotCase):
    def test_the_pilot_path_builds_a_dense_panel_over_geoboundaries_regions(self):
        ds = self.build()
        self.assertEqual([r.id for r in ds.regions],
                         [shape_id(i) for i in range(3)])
        self.assertEqual([r.name for r in ds.regions],
                         ["Region 1", "Region 2", "Region 3"])
        self.assertEqual(len(ds.panel), 3 * 4 * 4)   # 3 regions x 4 years x 4 quarters
        self.assertEqual(
            sorted(u for u, y in ds.panel if y == 1),
            [(shape_id(0), 2006, 3), (shape_id(2), 2007, 1)],
        )

    def test_the_diagnostics_count_the_other_hazards_rows(self):
        d = self.build().diagnostics
        self.assertEqual(d.n_skipped_hazard, 1)      # the tornado row
        self.assertEqual(d.n_events, 4)
        self.assertEqual(d.n_damaging, 3)            # two of them share one cell
        self.assertEqual(d.n_zone_coded, 0)
        self.assertIn("other hazards", d.format())

    def test_input_keys_are_the_region_file_and_the_record(self):
        self.assertEqual(
            data_mod.input_keys(self.c),
            ["geoboundaries/ZZ/ADM1", "records/ZZ/zz_records.csv"],
        )
        ds = self.build()
        self.assertEqual(ds.data_version, ds.manifest.digest(data_mod.input_keys(self.c)))
        self.assertEqual(ds.provenance()["inputs"], data_mod.input_keys(self.c))
        self.assertEqual(ds.provenance()["scope"], "ZZ:all")
        json.dumps(ds.provenance())   # it goes onto a card

    def test_every_input_resolves_to_a_globally_available_connector(self):
        for key in data_mod.input_keys(self.c):
            with self.subTest(key=key):
                self.assertTrue(connector_for_key(key).global_coverage, key)

    def test_a_pilot_snapshot_is_pinned(self):
        self.assertTrue(data_mod.pinned(self.c, self.root))

    def test_a_record_that_no_longer_matches_its_hash_is_not_pinned(self):
        self.records_file.write_bytes(self.records_file.read_bytes() + b"# edit\n")
        self.assertFalse(data_mod.pinned(self.c, self.root))
        with self.assertRaises(ConnectorError) as ctx:
            self.build()
        self.assertIn("different contract", str(ctx.exception))

    def test_a_missing_boundary_file_is_not_pinned(self):
        geoboundaries.cache_path(self.root, "ZZ", "ADM1").unlink()
        self.assertFalse(data_mod.pinned(self.c, self.root))

    def test_an_edited_boundary_cache_is_not_pinned_either(self):
        # Checked by hash, like the record: bytes that no longer match their
        # manifest record are data of unknown provenance. `pinned()` used to
        # say True on `.exists()` alone, so the site build — which runs from
        # committed snapshots — silently went to the network for them.
        cache = geoboundaries.cache_path(self.root, "ZZ", "ADM1")
        blob = json.loads(cache.read_text())
        blob["features"][0]["properties"]["shapeName"] = "Edited"
        cache.write_text(json.dumps(blob))
        self.assertFalse(data_mod.pinned(self.c, self.root))
        # ...and that answer is load-bearing: `tools/build_site.py` builds a
        # contract only when `pinned()` is True, and a build run anyway goes
        # to the network, which this fixture's `no_fetch` reports as an
        # AssertionError naming the URL.
        with self.assertRaises(AssertionError) as ctx:
            self.build()
        self.assertIn("tried to download", str(ctx.exception))

    def test_a_us_only_feature_is_never_pinned_for_a_pilot(self):
        # `cli._features()` loads every connector whose data is already
        # pinned when no `--features` flag is given. Answering True here (an
        # empty file list used to answer vacuously) auto-selected `terrain`
        # for a pilot, and `build` then refused the whole run — turning the
        # documented per-candidate skip into a crash.
        for feature in data_mod.US_ONLY_FEATURES:
            with self.subTest(feature=feature):
                self.assertFalse(
                    data_mod.pinned(self.c, self.root, features=[feature])
                )
        # era5 is global; it is simply not pinned in this fixture either, and
        # the base data alone is.
        self.assertTrue(data_mod.pinned(self.c, self.root))
        self.assertFalse(data_mod.pinned(self.c, self.root, features=["era5"]))

    def test_the_us_path_still_pins_its_us_only_features(self):
        # The exclusion is about the contract's country, not about the names.
        us = make_contract(splits=SPLITS)
        self.assertFalse(data_mod.pinned(us, self.root, features=["terrain"]))

    def test_the_us_path_is_untouched_by_the_dispatch(self):
        # The same assertion `TestBuild` makes, restated here: dispatching on
        # the contract's sources did not move the census/storm-events branch.
        us = make_contract(splits=SPLITS)
        self.assertEqual(
            data_mod.input_keys(us),
            [data_mod.CENSUS_KEY] + [data_mod.storm_events_key(y) for y in us.all_years()],
        )
        self.assertEqual(data_mod.era5_parts(us, ["99"]), ["99"])
        self.assertEqual(data_mod.era5_parts(self.c, []), ["ZZ"])

    def test_a_us_only_feature_connector_is_refused_for_a_pilot(self):
        with self.assertRaises(ValueError) as ctx:
            data_mod.build(self.c, snapshot_dir=self.root, features=["terrain"])
        self.assertIn("US-only", str(ctx.exception))
        with self.assertRaises(ValueError):
            data_mod.build(self.c, snapshot_dir=self.root, features=["nri"])


class TestEmdatDispatch(PilotSnapshotCase):
    """The other ground truth, through the same seam."""

    def setUp(self):
        super().setUp()
        blob = (DATA / "emdat_sample.xlsx").read_bytes()
        self.c = make_pilot_contract(
            source="emdat", file="zz_emdat.xlsx", sha256=sha256_bytes(blob),
            record_start_year=2000,
            splits={"train": [2005, 2006], "validate": [2007, 2007], "test": [2008, 2008]},
        )
        export = data_mod.records_path(self.c, self.root)
        export.parent.mkdir(parents=True, exist_ok=True)
        export.write_bytes(blob)
        # The crosswalk stays flat: it is committed, one per country, and its
        # name already carries the country code.
        (self.root / "records" / "zz_emdat_regions.csv").write_bytes(
            (DATA / "zz_emdat_regions.csv").read_bytes()
        )
        manifest = Manifest.load(self.root / "manifest.json")
        manifest.add(data_mod.regions_key(self.c),
                     record("geoboundaries", sha256_bytes(self.geojson)))
        manifest.add(data_mod.records_key(self.c), record("emdat", sha256_bytes(blob)))
        manifest.save()

    def test_the_export_and_the_crosswalk_build_the_panel(self):
        ds = self.build()
        self.assertEqual(data_mod.input_keys(self.c),
                         ["geoboundaries/ZZ/ADM1", "emdat/ZZ/zz_emdat.xlsx"])
        self.assertEqual(
            sorted(u for u, y in ds.panel if y == 1),
            [(shape_id(0), 2006, 1), (shape_id(1), 2006, 3), (shape_id(2), 2006, 3)],
        )
        self.assertTrue(data_mod.pinned(self.c, self.root))

    def test_without_the_crosswalk_the_build_refuses_rather_than_guessing(self):
        (self.root / "records" / "zz_emdat_regions.csv").unlink()
        self.assertFalse(data_mod.pinned(self.c, self.root))
        with self.assertRaises(ConnectorError) as ctx:
            self.build()
        self.assertIn("written down by a person", str(ctx.exception))
if __name__ == "__main__":
    unittest.main()
