"""The facility record: what it refuses, and why each refusal is the design.

Two of these tests are load-bearing rather than defensive. The forbidden-key
test is the schema saying out loud that nothing address-level exists here
(plan §4), and the evidence test is what makes a citation to `power.fuel_hours`
worth anything: the field path leads to a document, or the file is refused.
"""

import hashlib
import json
import pathlib
import subprocess
import tempfile
import unittest

from readiness.plans import facility as facility_mod
from readiness.plans.facility import Facility, FacilityError
from tests.fixtures_plans import facility_dict, make_facility, write_facility

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "plans" / "facilities" / "example-rural-hospital.json"


def git(*args: str) -> str:
    """git in this repository, or a clear failure. Never a skip.

    What is committed is the property these tests exist to hold, and a tree
    without git is a tree where that property cannot be checked at all.
    """
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def tracked(*paths: str) -> list[str]:
    """Every file git actually carries under these paths, sorted."""
    return sorted(line for line in git("ls-files", *paths).splitlines() if line)


def ignored(path: str) -> bool:
    """Whether `.gitignore` would keep this path out of git."""
    return bool(git("check-ignore", "--", path).strip())


class TestReading(unittest.TestCase):
    def test_reads_every_field_of_the_schema(self):
        f = make_facility()
        self.assertEqual(f.slug, "test-facility")
        self.assertEqual(f.occupancy_type, "hospital")
        self.assertEqual(f.county_fips, "99001")
        self.assertEqual(f.power.fuel_hours, 120)
        self.assertEqual(f.water.on_site_storage_hours, 96)
        self.assertEqual(f.design_intensity.flood_elevation_ft, 10)
        self.assertTrue(f.evacuation.trigger_written)
        self.assertEqual(len(f.transfer_agreements), 1)
        self.assertEqual(f.co_located_operators, ())

    def test_dotted_paths_read_through_lists(self):
        f = make_facility()
        self.assertEqual(f.get("power.fuel_hours"), 120)
        self.assertEqual(f.get("transfer_agreements.0.name"), "Far Ridge Hospital")
        self.assertIsNone(f.get("transfer_agreements.9.name"))
        self.assertIsNone(f.get("power.nonexistent"))

    def test_the_blind_label_is_the_record_s_own_random_id_not_the_slug(self):
        # Finding 2 of the leakage review: a six-hex digest of a slug is a
        # dictionary attack away from the slug, and the blinded page is the
        # artefact that leaves the building.
        f = make_facility(slug="some-slug")
        self.assertEqual(f.blind_label, f"FACILITY-{f.blind_id[:12]}")
        self.assertRegex(f.blind_label, r"^FACILITY-[0-9a-f]{12}$")
        self.assertEqual(facility_mod.BLIND_CHARS, 12)
        digest = hashlib.sha256(b"some-slug").hexdigest()
        self.assertNotIn(digest[:12], f.blind_label)
        # Two records with the same slug and different ids are two facilities.
        other = make_facility(slug="some-slug", blind_id="f" * 32)
        self.assertNotEqual(other.blind_label, f.blind_label)

    def test_a_missing_or_malformed_blind_id_is_refused_with_the_command(self):
        for value in (None, "", "abc", "A" * 32, "0" * 31, "0" * 33):
            with self.subTest(value=value):
                raw = facility_dict()
                raw["blind_id"] = value
                with self.assertRaises(FacilityError) as ctx:
                    Facility.from_json(raw, where="f.json")
                message = str(ctx.exception)
                self.assertIn("blind_id", message)
                self.assertIn("secrets.token_hex(16)", message)
        raw = facility_dict()
        del raw["blind_id"]
        with self.assertRaises(FacilityError) as ctx:
            Facility.from_json(raw, where="f.json")
        self.assertIn("'blind_id' is missing", str(ctx.exception))

    def test_the_blind_id_needs_no_evidence_and_may_not_have_any(self):
        # It is this repository's bookkeeping, not a fact read off a document.
        raw = facility_dict()
        self.assertNotIn("blind_id", raw["evidence"])
        Facility.from_json(raw)
        raw["evidence"]["blind_id"] = {"text": "t", "source_doc": "d", "page": None}
        with self.assertRaises(FacilityError) as ctx:
            Facility.from_json(raw, where="f.json")
        self.assertIn("names no populated field", str(ctx.exception))

    def test_partner_names_are_every_name_a_blinding_must_replace(self):
        f = make_facility(co_located_operators=[{"name": "Dialysis Co", "occupants": 8}])
        self.assertEqual(f.partner_names(), ("Far Ridge Hospital", "Dialysis Co"))

    def test_labels_are_positional_and_fixed_by_the_record(self):
        f = make_facility(
            transfer_agreements=[
                {"name": "Far Ridge Hospital", "county_fips": "99007", "signed": True,
                 "same_floodplain": False, "same_grid_feeder": False},
                {"name": "Second Hospital", "county_fips": "99009", "signed": True,
                 "same_floodplain": False, "same_grid_feeder": False},
            ],
            co_located_operators=[{"name": "Dialysis Co", "occupants": 8}],
        )
        self.assertEqual(f.partner_labels(), {
            "Far Ridge Hospital": "PARTNER-1",
            "Second Hospital": "PARTNER-2",
            "Dialysis Co": "PARTNER-3",
        })
        self.assertEqual(f.partner_label("Dialysis Co"), "PARTNER-3")
        # Transfer agreements first, then co-located operators, in record order.
        self.assertEqual(
            list(f.county_labels()),
            ["99001", "99007", "99009"],
        )
        self.assertEqual(
            list(f.county_labels().values()), ["COUNTY-A", "COUNTY-B", "COUNTY-C"]
        )

    def test_document_labels_cover_every_evidence_source(self):
        f = make_facility()
        docs = f.document_labels()
        self.assertIn("synthetic planning record", docs)
        self.assertIn(f.design_intensity.flood_elevation_source, docs)
        self.assertTrue(all(v.startswith("DOCUMENT-") for v in docs.values()))

    def test_names_are_whitespace_normalised_at_load(self):
        # Finding 1 of the correctness review: prose collapses whitespace, so a
        # record that does not leaves the blinding keyed on a string that never
        # appears in the page it is meant to clean.
        for spelling in ("Mercy  Hospital", " Mercy Hospital", "Mercy\tHospital",
                         "Mercy\nHospital", "Mercy Hospital "):
            with self.subTest(spelling=spelling):
                f = make_facility(**{"transfer_agreements.0.name": spelling})
                self.assertEqual(f.transfer_agreements[0].name, "Mercy Hospital")
                self.assertEqual(f.get("transfer_agreements.0.name"), "Mercy Hospital")
                self.assertEqual(f.partner_names(), ("Mercy Hospital",))

    def test_from_path_reads_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_facility(pathlib.Path(tmp) / "f.json", slug="from-disk")
            self.assertEqual(Facility.from_path(path).slug, "from-disk")


class TestRefusals(unittest.TestCase):
    def refuse(self, raw) -> str:
        with self.assertRaises(FacilityError) as ctx:
            Facility.from_json(raw, where="f.json")
        return str(ctx.exception)

    def test_an_unknown_key_is_refused_by_name(self):
        raw = facility_dict()
        raw["sprinklers"] = True
        message = self.refuse(raw)
        self.assertIn("unknown field 'sprinklers'", message)
        self.assertIn("must not be silently dropped", message)

    def test_an_unknown_nested_key_is_refused_by_its_path(self):
        raw = facility_dict()
        raw["power"]["battery_hours"] = 4
        self.assertIn("unknown field 'power.battery_hours'", self.refuse(raw))

    def test_every_forbidden_key_is_refused_with_the_reason(self):
        for key in facility_mod.FORBIDDEN_KEYS:
            with self.subTest(key=key):
                raw = facility_dict()
                raw[key] = "anything"
                message = self.refuse(raw)
                self.assertIn(f"field {key!r} is refused", message)
                self.assertIn("no address or coordinate field", message)
                self.assertIn("Elevation Certificate", message)

    def test_a_forbidden_key_is_refused_however_deep_it_hides(self):
        raw = facility_dict()
        raw["transfer_agreements"][0]["lat"] = 35.9
        message = self.refuse(raw)
        self.assertIn("'transfer_agreements.0.lat' is refused", message)

    def test_a_place_in_a_string_value_is_refused_too(self):
        # Finding 5 of the leakage review: a key scan is a fail-open, because
        # no key list can cover a free-text note.
        cases = {
            "412 Riverside Drive": "a street address",
            "1 Main St": "a street address",
            "27834-1234": "a ZIP+4",
            "35.6127, -77.3664": "a decimal-degree coordinate pair",
            "ZIP 27834": "the token 'ZIP'",
        }
        for value, what in cases.items():
            with self.subTest(value=value):
                raw = facility_dict()
                raw["evidence"]["power.fuel_hours"]["text"] = f"read from {value}"
                message = self.refuse(raw)
                self.assertIn("is refused", message)
                self.assertIn("evidence.power.fuel_hours.text", message)
        # ... and a bare five-digit number is a county FIPS, not a ZIP.
        raw = facility_dict()
        raw["evidence"]["power.fuel_hours"]["text"] = "county 27834, twelve hours"
        Facility.from_json(raw)

    def test_the_forbidden_key_list_covers_the_vocabulary_of_a_place(self):
        for key in ("zip", "zipcode", "postal_code", "address_line1",
                    "address_line2", "geometry", "coordinates", "block_group",
                    "apn", "easting", "northing", "plus_code", "geohash", "gps",
                    "latlon", "lat_lon"):
            with self.subTest(key=key):
                self.assertIn(key, facility_mod.FORBIDDEN_KEYS)

    def test_a_negative_hour_elevation_or_count_is_refused(self):
        for path in ("power.fuel_hours", "water.on_site_storage_hours",
                     "evacuation.transport_lead_hours",
                     "power.switchgear_elevation_ft", "census",
                     "co_located_operators.0.occupants"):
            with self.subTest(path=path):
                raw = facility_dict(
                    co_located_operators=[{"name": "Co", "occupants": 4}],
                )
                from tests.fixtures_plans import deep_set

                deep_set(raw, path, -5)
                message = self.refuse(raw)
                self.assertIn(path.rsplit(".", 1)[-1], message)
                self.assertIn("must not be negative", message)
        # Zero is a real reading and stays legal.
        make_facility(**{"power.fuel_hours": 0})

    def test_the_schema_has_no_address_or_coordinate_field_at_all(self):
        # The refusal above is a tripwire; this is the property it guards.
        blob = json.dumps(facility_mod.SCHEMA)
        for key in facility_mod.FORBIDDEN_KEYS:
            self.assertNotIn(f'"{key}"', blob)

    def test_a_missing_field_is_refused_by_name(self):
        raw = facility_dict()
        del raw["water"]
        self.assertIn("field 'water' is missing", self.refuse(raw))

    def test_a_populated_field_without_evidence_is_refused_naming_it(self):
        raw = facility_dict()
        del raw["evidence"]["power.fuel_hours"]
        message = self.refuse(raw)
        self.assertIn("'power.fuel_hours' is populated but no evidence entry", message)
        self.assertIn("source_doc", message)

    def test_an_evidence_key_naming_no_field_is_refused(self):
        raw = facility_dict()
        raw["evidence"]["power.battery_hours"] = {
            "text": "t", "source_doc": "d", "page": None
        }
        self.assertIn("names no populated field", self.refuse(raw))

    def test_a_list_leaf_may_be_evidenced_by_the_list(self):
        raw = facility_dict()
        self.assertIn("transfer_agreements", raw["evidence"])
        self.assertNotIn("transfer_agreements.0.signed", raw["evidence"])
        Facility.from_json(raw)  # does not raise

    def test_evidence_needs_a_source_document(self):
        raw = facility_dict()
        raw["evidence"]["power.fuel_hours"]["source_doc"] = "  "
        self.assertIn("needs a non-empty 'source_doc'", self.refuse(raw))

    def test_an_unknown_evidence_field_is_refused(self):
        raw = facility_dict()
        raw["evidence"]["power.fuel_hours"]["confidence"] = "high"
        self.assertIn("unknown field", self.refuse(raw))

    def test_type_errors_name_the_field(self):
        cases = {
            "county_fips": ("9901", "five-digit county FIPS"),
            "occupancy_type": ("clinic", "must be one of"),
            "census": (30.5, "whole number"),
            "slug": ("a slug/with slash", "must be a slug"),
        }
        for field, (value, needle) in cases.items():
            with self.subTest(field=field):
                raw = facility_dict(**{field: value})
                message = self.refuse(raw)
                self.assertIn(field, message)
                self.assertIn(needle, message)

    def test_a_required_field_may_not_be_null(self):
        raw = facility_dict(**{"power.fuel_hours": None})
        self.assertIn("'power.fuel_hours' must not be null", self.refuse(raw))

    def test_an_optional_field_may_be_null(self):
        f = make_facility(**{"design_intensity.flood_elevation_ft": None})
        self.assertIsNone(f.design_intensity.flood_elevation_ft)

    def test_a_file_that_is_not_json_is_refused_with_its_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "f.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(FacilityError) as ctx:
                Facility.from_path(path)
            self.assertIn("not valid JSON", str(ctx.exception))

    def test_a_missing_file_is_refused_with_its_path(self):
        with self.assertRaises(FacilityError) as ctx:
            Facility.from_path("/nonexistent/facility.json")
        self.assertIn("cannot be read", str(ctx.exception))


class TestCommittedExample(unittest.TestCase):
    """The one facility file git carries, which must be fictional and complete."""

    def setUp(self):
        self.raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.facility = Facility.from_path(EXAMPLE)

    def test_it_loads(self):
        self.assertEqual(self.facility.slug, "example-rural-hospital")
        self.assertEqual(self.facility.occupancy_type, "hospital")

    def test_it_is_in_a_county_that_does_not_exist(self):
        self.assertTrue(self.facility.county_fips.startswith("99"))
        for agreement in self.facility.transfer_agreements:
            self.assertTrue(agreement.county_fips.startswith("99"))

    def test_every_populated_field_has_its_own_evidence_entry(self):
        for path in facility_mod.populated_paths(self.raw):
            head = path.split(".", 1)[0]
            listed = isinstance(self.raw.get(head), list)
            with self.subTest(path=path):
                self.assertTrue(
                    path in self.facility.evidence
                    or (listed and head in self.facility.evidence),
                    f"{path} has no evidence entry",
                )

    def test_every_evidence_entry_says_it_is_fictional_or_names_a_document(self):
        for key, evidence in self.facility.evidence.items():
            with self.subTest(key=key):
                self.assertTrue(evidence.source_doc.strip())
                self.assertIn("fictional", (evidence.text + evidence.source_doc).lower())

    def test_it_has_a_blind_id_that_is_not_a_digest_of_its_slug(self):
        self.assertRegex(self.facility.blind_id, r"^[0-9a-f]{32}$")
        self.assertNotEqual(
            self.facility.blind_id,
            hashlib.sha256(self.facility.slug.encode()).hexdigest()[:32],
        )

    def test_it_is_the_only_committed_facility(self):
        # `git ls-files`, not a filesystem glob: the glob sees the planner's
        # own (correctly ignored) records and misses exactly the files that
        # would be committed — a record in a subdirectory, or one named .JSON.
        self.assertEqual(tracked("plans/facilities"),
                         ["plans/facilities/README.md",
                          "plans/facilities/example-rural-hospital.json"])

    def test_no_record_escapes_the_ignore_rules(self):
        for name in ("2026/mercy-general.json", "mercy-general.JSON",
                     "mercy-general.yaml", "roster.md", "sub/dir/deep.json"):
            with self.subTest(name=name):
                self.assertTrue(
                    ignored(f"plans/facilities/{name}"),
                    f"plans/facilities/{name} would be committed by default",
                )


if __name__ == "__main__":
    unittest.main()
