"""Contracts as data: construction, validation, identity, and the registry.

The digest is the load-bearing property. It must cover every criterion and
nothing but the criteria, so that two experiments are comparable exactly when
they were judged by the same rules.
"""

import dataclasses
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from readiness import contracts
from readiness.config import (
    GROUND_TRUTH_SOURCES,
    HAZARD_CATEGORIES,
    HAZARDS,
    RECORD_START_YEAR,
)
from readiness.contracts import Contract, ContractError
from tests.fixtures import PILOT_SPLITS, make_contract, make_pilot_contract


class TestDefaults(unittest.TestCase):
    """One mapping of documented defaults, read by every constructor."""

    def test_defaults_are_read_only(self):
        with self.assertRaises(TypeError):
            contracts.DEFAULTS["min_auc"] = 0.5

    def test_from_spec_fills_every_gap_from_defaults(self):
        d = contracts.DEFAULTS
        c = Contract.from_spec(
            {"name": "x", "hazard": "tornado",
             "splits": {"train": d["train"], "validate": d["validate"], "test": d["test"]}}
        )
        self.assertEqual(c.version, d["version"])
        self.assertEqual(c.country, d["country"])
        self.assertEqual(c.period, d["period"])
        self.assertEqual(c.damage_property_usd_min, d["property_usd_min"])
        self.assertEqual(c.damage_count_casualties, d["count_casualties"])
        self.assertEqual(c.zone_policy, d["zone_policy"])
        self.assertEqual(c.test_touch_budget, d["test_touch_budget"])
        self.assertEqual(c.reference_model, d["reference_model"])
        self.assertEqual(c.min_brier_skill_score, d["min_brier_skill_score"])
        self.assertEqual(c.reliability_tolerance_pp, d["reliability_tolerance_pp"])
        self.assertEqual(c.reliability_min_bin_count, d["reliability_min_bin_count"])
        self.assertEqual(c.min_auc, d["min_auc"])
        self.assertEqual(c.n_reliability_bins, d["n_reliability_bins"])

    def test_new_with_nothing_but_a_hazard_is_the_default_contract(self):
        d = contracts.DEFAULTS
        want = Contract.from_spec(
            {"name": "x", "hazard": "tornado",
             "splits": {"train": d["train"], "validate": d["validate"], "test": d["test"]}}
        )
        self.assertEqual(contracts.new("x", hazard="tornado"), want)
        self.assertEqual(contracts.new("x", hazard="tornado").digest(), want.digest())

    def test_the_documented_defaults_are_the_committed_ones(self):
        # The README and docs/contracts.md quote these; a change here is a
        # documentation change too, and moves every new contract's digest.
        d = contracts.DEFAULTS
        self.assertEqual((d["train"], d["validate"], d["test"]),
                         ("1996-2015", "2016-2020", "2021-2025"))
        self.assertEqual(d["property_usd_min"], 10_000.0)
        self.assertEqual(d["min_auc"], 0.70)
        self.assertEqual(d["reliability_tolerance_pp"], 0.05)


class TestConstruction(unittest.TestCase):
    def test_defaults_fill_in(self):
        c = Contract.from_spec(
            {"name": "x", "hazard": "tornado", "splits": {"train": [1996, 2015],
             "validate": [2016, 2020], "test": [2021, 2025]}}
        )
        self.assertEqual(c.event_types, HAZARDS["tornado"].event_types)
        self.assertEqual(c.states, ())
        self.assertEqual(c.period, "quarter")
        self.assertEqual(c.zone_policy, "drop")
        self.assertEqual(c.reference_model, "climatology-pooled")
        self.assertEqual(c.min_auc, 0.70)

    def test_year_ranges_accept_both_forms(self):
        a = make_contract(splits={"train": "1996-2015"})
        b = make_contract(splits={"train": [1996, 2015]})
        self.assertEqual(a.train_years, b.train_years)
        self.assertEqual(len(a.train_years), 20)

    def test_states_are_upper_cased_and_scope_key_is_stable(self):
        c = make_contract(scope={"states": ["zz", "yy"]})
        self.assertEqual(c.states, ("ZZ", "YY"))
        self.assertEqual(c.scope_key, "US:ZZ+YY")
        self.assertEqual(make_contract(scope={"states": []}).scope_key, "US:all")

    def test_periods_per_year(self):
        self.assertEqual(make_contract(period="month").periods_per_year, 12)
        self.assertEqual(make_contract(period="quarter").periods_per_year, 4)
        self.assertEqual(make_contract(period="year").periods_per_year, 1)

    def test_custom_hazard_needs_event_types(self):
        with self.assertRaises(ContractError):
            make_contract(hazard="volcano")
        c = make_contract(hazard="volcano", event_types=["Volcanic Ash"])
        self.assertEqual(c.event_types, ("Volcanic Ash",))

    def test_splits_are_exposed(self):
        s = make_contract().splits
        self.assertEqual(s.names, ("train", "validate", "test"))
        self.assertEqual(s.get("validate").years, tuple(range(2016, 2021)))
        with self.assertRaises(ContractError):
            s.get("holdout")

    def test_describe_mentions_the_essentials(self):
        text = make_contract().describe()
        for needle in ("test-hazard", "inland_flood", "US, ZZ", "region x quarter", "AUC",
                       "zone events"):
            self.assertIn(needle, text)
        self.assertIn("crosswalk", make_contract(zone_policy="expand").describe())


class TestValidation(unittest.TestCase):
    def assert_rejected(self, message_fragment: str, **overrides):
        with self.assertRaises(ContractError) as ctx:
            make_contract(**overrides)
        self.assertIn(message_fragment, str(ctx.exception))

    def test_name_must_be_a_slug(self):
        self.assert_rejected("lowercase", name="Bad Name")
        self.assert_rejected("lowercase", name="")

    def test_overlapping_splits(self):
        self.assert_rejected("disjoint", splits={"train": [1996, 2016]})

    def test_unordered_splits(self):
        self.assert_rejected(
            "ordered in time",
            splits={"train": [2016, 2020], "validate": [1996, 2015], "test": [2021, 2025]},
        )

    def test_splits_before_the_record_starts(self):
        self.assert_rejected(str(RECORD_START_YEAR), splits={"train": [1990, 2015]})

    def test_backwards_range(self):
        self.assert_rejected("backwards", splits={"train": [2015, 1996]})

    def test_bad_period(self):
        self.assert_rejected("period", period="week")

    def test_event_types_outside_the_catalogue(self):
        self.assert_rejected("not part of", hazard="tornado", event_types=["Hail"])

    def test_a_country_outside_the_us_must_name_its_own_ground_truth(self):
        # Storm Events is a US archive. Before Phase 4 any other country was
        # refused outright; now it is refused unless it says what it is
        # scored against instead.
        self.assert_rejected(
            "Storm Events is a US record", scope={"country": "FR", "states": []}
        )

    def test_state_format(self):
        self.assert_rejected("two-letter", scope={"states": ["Somewhere"]})

    def test_duplicate_state(self):
        self.assert_rejected("duplicate", scope={"states": ["ZZ", "zz"]})

    def test_vacuous_damage_definition(self):
        self.assert_rejected(
            "not a damage definition",
            damaging={"property_usd_min": 0, "count_casualties": False},
        )

    def test_thresholds(self):
        self.assert_rejected("AUC", thresholds={"min_auc": 0.4})
        self.assert_rejected("tolerance", thresholds={"reliability_tolerance_pp": 1.5})
        self.assert_rejected("bins", thresholds={"n_reliability_bins": 1})
        self.assert_rejected("budget", test_touch_budget=0)

    def test_zone_policy_must_be_known(self):
        self.assert_rejected("zone_policy", zone_policy="guess")

    def test_reference_model_must_be_computable(self):
        self.assert_rejected("reference model", reference_model="oracle")

    def test_malformed_values_name_the_field(self):
        # A value of the wrong shape is a ContractError that says which field,
        # never a bare TypeError or ValueError traceback from a cast.
        for overrides, field in (
            ({"thresholds": {"min_auc": "high"}}, "thresholds.min_auc"),
            ({"thresholds": {"n_reliability_bins": None}},
             "thresholds.n_reliability_bins"),
            ({"damaging": {"property_usd_min": "lots"}}, "damaging.property_usd_min"),
            ({"test_touch_budget": "one"}, "test_touch_budget"),
            ({"splits": {"train": ["a", 2015]}}, "splits.train"),
            ({"scope": {"states": 7}}, "scope.states"),
            ({"event_types": 5}, "event_types"),
            ({"scope": "US"}, "scope"),
            ({"thresholds": [0.7]}, "thresholds"),
        ):
            with self.subTest(field=field), self.assertRaises(ContractError) as ctx:
                make_contract(**overrides)
            self.assertIn(field, str(ctx.exception))

    def test_a_non_object_spec_is_refused(self):
        for spec in ([], "flood", 3):
            with self.subTest(spec=spec), self.assertRaises(ContractError):
                Contract.from_spec(spec)

    def test_missing_required_field(self):
        with self.assertRaises(ContractError):
            Contract.from_spec({"name": "x", "hazard": "tornado"})


class TestIdentity(unittest.TestCase):
    def test_digest_is_stable(self):
        self.assertEqual(make_contract().digest(), make_contract().digest())

    def test_labels_do_not_change_the_digest(self):
        base = make_contract().digest()
        self.assertEqual(make_contract(name="other-name").digest(), base)
        self.assertEqual(make_contract(version="9.9.9").digest(), base)
        self.assertEqual(make_contract(description="different words").digest(), base)

    def test_every_criterion_changes_the_digest(self):
        base = make_contract()
        variants = {
            "hazard": make_contract(hazard="tornado"),
            "event_types": make_contract(event_types=["Flood"]),
            "states": make_contract(scope={"states": ["YY"]}),
            "period": make_contract(period="month"),
            "damage": make_contract(damaging={"property_usd_min": 1.0}),
            "casualties": make_contract(damaging={"count_casualties": False}),
            "zone_policy": make_contract(zone_policy="expand"),
            "train": make_contract(splits={"train": [1997, 2015]}),
            "validate": make_contract(splits={"validate": [2016, 2019], "test": [2020, 2025]}),
            "budget": make_contract(test_touch_budget=2),
            "min_bss": make_contract(thresholds={"min_brier_skill_score": 0.01}),
            "tolerance": make_contract(thresholds={"reliability_tolerance_pp": 0.1}),
            "min_bin": make_contract(thresholds={"reliability_min_bin_count": 10}),
            "min_auc": make_contract(thresholds={"min_auc": 0.65}),
            "bins": make_contract(thresholds={"n_reliability_bins": 5}),
        }
        seen = {base.digest()}
        for label, variant in variants.items():
            with self.subTest(criterion=label):
                self.assertNotEqual(variant.digest(), base.digest())
                self.assertNotIn(variant.digest(), seen)
                seen.add(variant.digest())

    def test_dataclass_replace_changes_the_digest_too(self):
        base = make_contract()
        altered = dataclasses.replace(base, min_auc=0.65)
        self.assertNotEqual(altered.digest(), base.digest())

    def test_spec_round_trips(self):
        c = make_contract(period="month", scope={"states": ["ZZ", "YY"]})
        again = Contract.from_spec(json.loads(json.dumps(c.to_spec())))
        self.assertEqual(again, c)
        self.assertEqual(again.digest(), c.digest())


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_registry(self):
        self.assertEqual(contracts.registered(self.dir), {})
        self.assertIn("no contracts registered", contracts.describe_registry(self.dir))

    def test_save_load_and_list(self):
        c = make_contract(name="flood-zz")
        path = c.save(self.dir)
        self.assertEqual(path.name, "flood-zz.json")
        self.assertEqual(contracts.load("flood-zz", self.dir), c)
        self.assertEqual(list(contracts.registered(self.dir)), ["flood-zz"])
        self.assertIn("flood-zz", contracts.describe_registry(self.dir))

    def test_load_by_path(self):
        path = make_contract(name="flood-zz").save(self.dir)
        self.assertEqual(contracts.load(str(path)).name, "flood-zz")

    def test_saving_twice_is_refused_without_force(self):
        c = make_contract(name="flood-zz")
        c.save(self.dir)
        with self.assertRaises(ContractError):
            c.save(self.dir)
        c.save(self.dir, force=True)

    def test_file_name_must_match_contract_name(self):
        (self.dir / "other.json").write_text(json.dumps(make_contract(name="flood-zz").to_spec()))
        with self.assertRaises(ContractError) as ctx:
            contracts.registered(self.dir)
        self.assertIn("must agree", str(ctx.exception))

    def test_broken_file_is_an_error_not_a_skip(self):
        (self.dir / "broken.json").write_text("{not json")
        with self.assertRaises(ContractError):
            contracts.registered(self.dir)

    def test_unknown_name_lists_the_registry(self):
        make_contract(name="flood-zz").save(self.dir)
        with self.assertRaises(ContractError) as ctx:
            contracts.load("nope", self.dir)
        self.assertIn("flood-zz", str(ctx.exception))

    def test_resolve_explicit_then_env_then_sole(self):
        make_contract(name="flood-zz").save(self.dir)
        self.assertEqual(contracts.resolve("flood-zz", self.dir).name, "flood-zz")
        with mock.patch.dict(os.environ, {contracts.CONTRACT_ENV: "flood-zz"}):
            self.assertEqual(contracts.resolve(None, self.dir).name, "flood-zz")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(contracts.CONTRACT_ENV, None)
            self.assertEqual(contracts.resolve(None, self.dir).name, "flood-zz")

    def test_resolve_refuses_to_guess_between_several(self):
        make_contract(name="flood-zz").save(self.dir)
        make_contract(name="tornado-zz", hazard="tornado").save(self.dir)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(contracts.CONTRACT_ENV, None)
            with self.assertRaises(ContractError) as ctx:
                contracts.resolve(None, self.dir)
        self.assertIn("several", str(ctx.exception))

    def test_resolve_with_nothing_registered_explains_how_to_register(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(contracts.CONTRACT_ENV, None)
            with self.assertRaises(ContractError) as ctx:
                contracts.resolve(None, self.dir)
        self.assertIn("readiness register", str(ctx.exception))

    def test_contracts_dir_env_override(self):
        with mock.patch.dict(os.environ, {contracts.CONTRACTS_DIR_ENV: str(self.dir)}):
            self.assertEqual(contracts.contracts_dir(), self.dir)

    def test_new_builds_from_options(self):
        c = contracts.new(
            "heat-zz", hazard="heat", states=["zz"], period="month",
            train="1996-2010", validate="2011-2015", test="2016-2020",
            min_auc=0.65, reliability_tolerance_pp=None,
        )
        self.assertEqual(c.hazard, "heat")
        self.assertEqual(c.states, ("ZZ",))
        self.assertEqual(c.periods_per_year, 12)
        self.assertEqual(c.min_auc, 0.65)
        self.assertEqual(c.reliability_tolerance_pp, 0.05)  # None means default


class TestNationalContracts(unittest.TestCase):
    """The six Phase 2 contracts: registered as data, national, digests pinned.

    Pinned the way `test_repro_guard` pins the three state contracts: a
    digest that moves means a criterion moved, and every card written
    against the old one would be visibly incomparable with the new.
    """

    NATIONAL = {
        "inland-flood-us": ("inland_flood", "quarter", "drop", "a4b5c7b4dd2804d2"),
        "tornado-us": ("tornado", "quarter", "drop", "8a63025f6ca8c8a5"),
        "hail-us": ("hail", "quarter", "drop", "1343cbc001c74d51"),
        "severe-wind-us": ("severe_wind", "quarter", "drop", "ec252a590cf66cad"),
        "winter-storm-us": ("winter_storm", "month", "expand", "b247720c4661c278"),
        "heat-us": ("heat", "month", "expand", "28efcaf8861a6f45"),
    }

    def test_all_six_are_registered_and_validate(self):
        registry = contracts.registered()
        for name in self.NATIONAL:
            with self.subTest(contract=name):
                self.assertIn(name, registry)
                self.assertEqual(registry[name].name, name)
                registry[name].validate()

    def test_digests_are_pinned(self):
        for name, (hazard, period, policy, digest) in self.NATIONAL.items():
            with self.subTest(contract=name):
                c = contracts.load(name)
                self.assertEqual((c.hazard, c.period, c.zone_policy),
                                 (hazard, period, policy))
                self.assertEqual(c.digest(), digest)

    def test_they_are_national_and_otherwise_default(self):
        for name, (hazard, period, policy, _) in self.NATIONAL.items():
            with self.subTest(contract=name):
                c = contracts.load(name)
                self.assertEqual(c.states, ())
                self.assertEqual(c.scope_key, "US:all")
                want = contracts.new(name, hazard=hazard, period=period,
                                     zone_policy=policy)
                self.assertEqual(c.digest(), want.digest())

    def test_describe_names_the_national_scope(self):
        for name in self.NATIONAL:
            with self.subTest(contract=name):
                text = contracts.load(name).describe()
                self.assertIn("geography       US, every region", text)
                self.assertIn(name, text)

    def test_the_registry_holds_nine(self):
        self.assertEqual(len(contracts.registered()), 9)
        national = [n for n, c in contracts.registered().items() if not c.states]
        self.assertEqual(sorted(national), sorted(self.NATIONAL))


# ---------------------------------------------------------------------------
# Phase 4: the two source fields
# ---------------------------------------------------------------------------


class TestSchemaDefaultsAreElided(unittest.TestCase):
    """The hard constraint: adding the fields moved no committed digest.

    `criteria()` drops a schema field still at its documented default, so a
    contract that predates Phase 4 hashes exactly as it did — and a spec that
    spells the defaults out hashes the same as one that leaves them implicit,
    because the criteria are what the contract means, not how much of it the
    author typed.
    """

    def test_a_us_contract_carries_the_defaults(self):
        c = make_contract()
        self.assertEqual(c.ground_truth, {"source": "storm_events"})
        self.assertEqual(c.regions, {"source": "census"})
        self.assertEqual(c.ground_truth_source, "storm_events")
        self.assertEqual(c.regions_source, "census")
        self.assertFalse(c.is_pilot)

    def test_the_defaults_are_not_hashed(self):
        criteria = make_contract().criteria()
        self.assertNotIn("ground_truth", criteria)
        self.assertNotIn("regions", criteria)

    def test_naming_the_defaults_explicitly_gives_the_same_digest(self):
        implicit = make_contract()
        explicit = make_contract(
            ground_truth={"source": "storm_events"}, regions={"source": "census"}
        )
        self.assertEqual(explicit.digest(), implicit.digest())
        self.assertEqual(explicit.canonical_json(), implicit.canonical_json())

    def test_the_nine_committed_digests_have_not_moved(self):
        # The same nine pinned in tests/test_repro_guard.py (the three
        # examples) and TestNationalContracts (the six national ones), asserted
        # here as one statement: Phase 4 moved none of them.
        pinned = {
            "inland-flood-la": "477c493035c9c70d",
            "tornado-ok": "827313e1273a5e76",
            "tropical-cyclone-gulf": "d01b00aa1baedc98",
            "inland-flood-us": "a4b5c7b4dd2804d2",
            "tornado-us": "8a63025f6ca8c8a5",
            "hail-us": "1343cbc001c74d51",
            "severe-wind-us": "ec252a590cf66cad",
            "winter-storm-us": "b247720c4661c278",
            "heat-us": "28efcaf8861a6f45",
        }
        for name, digest in pinned.items():
            with self.subTest(contract=name):
                self.assertEqual(contracts.load(name).digest(), digest)

    def test_a_pilots_sources_are_hashed(self):
        base = make_pilot_contract()
        for change in (
            {"admin_level": "ADM2"},
            {"release": "gbOpen 5.0.0"},
        ):
            with self.subTest(**change):
                variant = make_pilot_contract(**change)
                self.assertNotEqual(variant.digest(), base.digest())
        self.assertNotEqual(
            make_pilot_contract(sha256="b" * 64).digest(), base.digest()
        )
        self.assertNotEqual(
            make_pilot_contract(source="emdat", file="zz.xlsx").digest(), base.digest()
        )

    def test_a_pilot_round_trips_through_its_spec(self):
        c = make_pilot_contract(admin_level="ADM2")
        again = Contract.from_spec(json.loads(json.dumps(c.to_spec())))
        self.assertEqual(again.digest(), c.digest())
        self.assertEqual(again.ground_truth, c.ground_truth)
        self.assertEqual(again.regions, c.regions)

    def test_a_us_spec_does_not_grow_the_two_fields(self):
        # The committed contract files must not change under `--force`.
        spec = make_contract().to_spec()
        self.assertNotIn("ground_truth", spec)
        self.assertNotIn("regions", spec)


class TestNonUsRequiresExplicitSources(unittest.TestCase):
    def reject(self, fragment, **over):
        with self.assertRaises(ContractError) as ctx:
            make_pilot_contract(**over)
        self.assertIn(fragment, str(ctx.exception))

    def test_a_valid_pilot(self):
        c = make_pilot_contract()
        c.validate()
        self.assertTrue(c.is_pilot)
        self.assertEqual(c.scope_key, "ZZ:all")
        self.assertEqual(c.admin_level, "ADM1")

    def test_storm_events_is_refused_outside_the_us(self):
        with self.assertRaises(ContractError) as ctx:
            make_contract(scope={"country": "ZZ", "states": []})
        self.assertIn("Storm Events is a US record", str(ctx.exception))

    def test_the_ground_truth_source_must_be_one_the_harness_reads(self):
        self.reject("is not one the harness can read", source="desinventar")

    def test_the_sha256_is_required_and_checked(self):
        self.reject("64 lowercase hex", sha256="deadbeef")
        self.reject("64 lowercase hex", sha256="A" * 64)

    def test_the_records_basename_is_required(self):
        self.reject("basename", file="")
        self.reject("basename", file="/home/someone/zz_records.csv")

    def test_the_record_start_year_is_required(self):
        c = make_pilot_contract()
        spec = c.to_spec()
        del spec["ground_truth"]["record_start_year"]
        with self.assertRaises(ContractError) as ctx:
            Contract.from_spec(spec)
        self.assertIn("record_start_year is required", str(ctx.exception))

    def test_the_regions_source_must_be_geoboundaries(self):
        c = make_pilot_contract()
        spec = c.to_spec()
        spec["regions"]["source"] = "census"
        with self.assertRaises(ContractError) as ctx:
            Contract.from_spec(spec)
        self.assertIn("geoBoundaries", str(ctx.exception))

    def test_the_admin_level_and_release_are_required(self):
        self.reject("admin_level must be one of", admin_level="ADM3")
        self.reject("regions.release", release="  ")

    def test_states_must_be_empty_outside_the_us(self):
        self.reject("scope.states", scope={"country": "ZZ", "states": ["ZZ"]})

    def test_zone_policy_expand_is_refused_outside_the_us(self):
        self.reject("must be 'drop' outside the US", zone_policy="expand")

    def test_a_hazard_with_no_global_mapping_is_refused(self):
        self.assertNotIn("dust_storm", HAZARD_CATEGORIES)
        self.reject("has no global mapping", hazard="dust_storm")
        # ...and every catalogue hazard the plan names does have one.
        for hazard in ("inland_flood", "tropical_cyclone", "drought", "wildfire",
                       "heat", "extreme_cold", "winter_storm", "tornado", "hail",
                       "severe_wind", "storm_surge", "coastal_flood", "tsunami",
                       "avalanche", "debris_flow"):
            with self.subTest(hazard=hazard):
                self.assertIn(hazard, HAZARD_CATEGORIES)
                make_pilot_contract(hazard=hazard).validate()

    def test_event_types_are_not_required_outside_the_us(self):
        c = make_pilot_contract()
        self.assertEqual(c.event_types, ())
        # The hazard reaches the record through the global catalogue instead.
        self.assertEqual(
            c.hazard_values(),
            list(HAZARD_CATEGORIES["inland_flood"]["national_records"]),
        )

    def test_a_us_contract_may_not_carry_a_pilots_fields(self):
        with self.assertRaises(ContractError) as ctx:
            make_contract(ground_truth={"source": "storm_events", "sha256": "a" * 64})
        self.assertIn("a US contract declares only the source", str(ctx.exception))

    def test_the_country_must_be_an_iso_code(self):
        with self.assertRaises(ContractError) as ctx:
            make_pilot_contract(scope={"country": "Zzland", "states": []})
        self.assertIn("alpha-2", str(ctx.exception))


class TestRecordStartYear(unittest.TestCase):
    """No split may begin before the record it is judged against does."""

    def test_the_us_start_year_is_unchanged(self):
        self.assertEqual(GROUND_TRUTH_SOURCES["storm_events"], RECORD_START_YEAR)
        self.assertEqual(make_contract().record_start_year, RECORD_START_YEAR)
        with self.assertRaises(ContractError) as ctx:
            make_contract(splits={**PILOT_SPLITS, "train": [1990, 2014]})
        self.assertIn("before the record can be trusted", str(ctx.exception))

    def test_a_partners_archive_says_when_it_starts(self):
        self.assertIsNone(GROUND_TRUTH_SOURCES["national_records"])
        c = make_pilot_contract(record_start_year=2005)
        self.assertEqual(c.record_start_year, 2005)
        with self.assertRaises(ContractError) as ctx:
            make_pilot_contract(
                record_start_year=2010, splits={**PILOT_SPLITS, "train": [2005, 2014]}
            )
        self.assertIn("2010, national_records", str(ctx.exception))

    def test_emdat_has_a_floor_a_contract_may_not_undercut(self):
        self.assertEqual(GROUND_TRUTH_SOURCES["emdat"], 2000)
        make_pilot_contract(source="emdat", file="zz.xlsx", record_start_year=2000)
        with self.assertRaises(ContractError) as ctx:
            make_pilot_contract(
                source="emdat", file="zz.xlsx", record_start_year=1995,
                splits={**PILOT_SPLITS, "train": [1995, 2014]},
            )
        self.assertIn("precedes 2000", str(ctx.exception))

    def test_the_floor_is_refused_at_the_boundary_not_only_in_bulk(self):
        # A gross undercut was already caught; one year short was not, which
        # is the shape a real mistake takes. `floor` is inclusive: 2000 is the
        # first year EM-DAT can be trusted, 1999 is not.
        with self.assertRaises(ContractError) as ctx:
            make_pilot_contract(
                source="emdat", file="zz.xlsx", record_start_year=1999,
                splits={**PILOT_SPLITS, "train": [1999, 2014]},
            )
        self.assertIn("precedes 2000", str(ctx.exception))
        make_pilot_contract(
            source="emdat", file="zz.xlsx", record_start_year=2000
        ).validate()

    def test_the_year_is_an_integer_and_is_not_coerced(self):
        # `criteria()` hashes the section as written, so "2005" and 2005 would
        # be one contract under two digests. Refused rather than normalised.
        # Written straight into the section, as a hand-written or
        # machine-written contract file would be: the fixture's own
        # `record_start_year=` goes through `pilot_sources`, which int-casts.
        for value in ("2005", 2005.7, True, 2005.0):
            with self.subTest(value=value):
                with self.assertRaises(ContractError) as ctx:
                    make_pilot_contract(ground_truth={"record_start_year": value})
                self.assertIn("must be a JSON integer", str(ctx.exception))
        make_pilot_contract(ground_truth={"record_start_year": 2005}).validate()


class TestPilotSectionsRefuseUnknownKeys(unittest.TestCase):
    """The pilot branch is as strict about extra keys as the US branch is.

    `to_spec()` writes both sections back verbatim, `criteria()` hashes them
    and the site publishes them, so an extra key — an operator's absolute path,
    say — would be committed, packed and published. That is exactly what
    `BASENAME_RE` exists to prevent one field along.
    """

    def reject(self, fragment, **over):
        with self.assertRaises(ContractError) as ctx:
            make_pilot_contract(**over)
        self.assertIn(fragment, str(ctx.exception))

    def test_an_operator_path_in_ground_truth_is_refused(self):
        self.reject(
            "unknown key is a criterion nobody agreed to",
            ground_truth={"path": "/home/operator/share/zz_records.csv"},
        )

    def test_an_unknown_regions_key_is_refused(self):
        self.reject("unknown key is a criterion nobody agreed to",
                    regions={"mirror": "http://elsewhere.invalid"})

    def test_the_named_key_is_in_the_message(self):
        with self.assertRaises(ContractError) as ctx:
            make_pilot_contract(ground_truth={"path": "/tmp/x"})
        self.assertIn("'path'", str(ctx.exception))

    def test_the_schema_keys_themselves_are_fine(self):
        make_pilot_contract().validate()
        make_pilot_contract(regions={"sha256": "b" * 64}).validate()


class TestPilotRegionsSha(unittest.TestCase):
    """`regions.sha256` is optional, hashed when present, elided when absent."""

    def test_absent_by_default_so_an_existing_digest_does_not_move(self):
        c = make_pilot_contract()
        self.assertNotIn("sha256", c.regions)
        self.assertEqual(c.digest(), make_pilot_contract().digest())

    def test_present_it_is_a_criterion(self):
        base = make_pilot_contract()
        pinned = make_pilot_contract(regions={"sha256": "b" * 64})
        self.assertNotEqual(base.digest(), pinned.digest())
        self.assertNotEqual(
            pinned.digest(), make_pilot_contract(regions={"sha256": "c" * 64}).digest()
        )

    def test_it_must_be_a_sha256(self):
        with self.assertRaises(ContractError) as ctx:
            make_pilot_contract(regions={"sha256": "nope"})
        self.assertIn("regions.sha256", str(ctx.exception))

    def test_pilot_sources_omits_it_unless_asked(self):
        _gt, regions = contracts.pilot_sources(
            source="national_records", file="x.csv", sha256="a" * 64,
            record_start_year=2005,
        )
        self.assertEqual(sorted(regions), ["admin_level", "release", "source"])
        _gt, regions = contracts.pilot_sources(
            source="national_records", file="x.csv", sha256="a" * 64,
            record_start_year=2005, regions_sha256="b" * 64,
        )
        self.assertEqual(regions["sha256"], "b" * 64)

    def test_pilot_sources_resolves_its_own_defaults(self):
        # The argument parser passes None for "not given" so it can refuse a
        # flag that does not apply; the defaults live here.
        _gt, regions = contracts.pilot_sources(
            source="emdat", file="x.xlsx", sha256="a" * 64, record_start_year=2000,
            admin_level=None, release=None,
        )
        self.assertEqual(regions["admin_level"], contracts.DEFAULTS["admin_level"])
        self.assertEqual(regions["release"], contracts.GEOBOUNDARIES_RELEASE)


class TestContractPatternsEndAtTheString(unittest.TestCase):
    r"""`$` also matches before a final newline; `\Z` does not.

    A country code with a trailing newline would reach a manifest key, a cache
    filename and the geoBoundaries URL; a sha256 with one can never match any
    file, and prints as two hashes that look identical.
    """

    CASES = (
        ("NAME_RE", "flood-zz"),
        ("STATE_RE", "LA"),
        ("COUNTRY_RE", "ZZ"),
        ("SHA256_RE", "a" * 64),
        ("BASENAME_RE", "zz_records.csv"),
        ("RELEASE_RE", "gbOpen 6.0.0"),
        ("YEAR_RANGE_RE", "1996-2015"),
    )

    def test_every_pattern_refuses_a_trailing_newline(self):
        for name, good in self.CASES:
            pattern = getattr(contracts, name)
            with self.subTest(pattern=name):
                self.assertTrue(pattern.match(good), good)
                self.assertFalse(pattern.match(good + "\n"), name)

    def test_the_contract_itself_refuses_them(self):
        with self.assertRaises(ContractError) as ctx:
            make_pilot_contract(name="flood-zz\n")
        self.assertIn("contract name", str(ctx.exception))
        with self.assertRaises(ContractError) as ctx:
            make_pilot_contract(scope={"country": "ZZ\n", "states": []})
        self.assertIn("alpha-2", str(ctx.exception))
        with self.assertRaises(ContractError) as ctx:
            make_pilot_contract(sha256="a" * 64 + "\n")
        self.assertIn("64 lowercase hex", str(ctx.exception))
        with self.assertRaises(ContractError) as ctx:
            make_pilot_contract(file="zz_records.csv\n")
        self.assertIn("basename", str(ctx.exception))
        with self.assertRaises(ContractError) as ctx:
            make_pilot_contract(release="gbOpen 6.0.0\n")
        self.assertIn("regions.release", str(ctx.exception))


class TestPilotReleaseIsCheckedAtRegistration(unittest.TestCase):
    """A contract is hashed before anything tries to resolve its release."""

    def test_a_release_geoboundaries_cannot_read_is_refused(self):
        for bad in ("latest", "6.0.0", "gbOpen", "gbOpen ", "gbHumanitarian 6.0.0"):
            with self.subTest(release=bad):
                with self.assertRaises(ContractError) as ctx:
                    make_pilot_contract(release=bad)
                self.assertIn("gbOpen <version>", str(ctx.exception))

    def test_the_shapes_the_connector_reads_are_accepted(self):
        for good in ("gbOpen 6.0.0", "gbOpen@6.0.0", "gbOpen 5.0.0"):
            with self.subTest(release=good):
                make_pilot_contract(release=good).validate()

    def test_the_pattern_is_the_connectors_pattern(self):
        from readiness.connectors import geoboundaries

        # Two copies on purpose (contracts must not import a connector), so a
        # test ties them together rather than a comment asking nicely.
        for value in ("gbOpen 6.0.0", "gbOpen@6.0.0", "latest", "6.0.0", "gbOpen",
                      "gbOpen 6.0.0\n", "gbopen 6.0.0"):
            with self.subTest(value=value):
                self.assertEqual(
                    bool(contracts.RELEASE_RE.match(value)),
                    bool(geoboundaries._RELEASE_RE.match(value)),
                    value,
                )


class TestPilotHazardMapsToItsOwnSource(unittest.TestCase):
    """A hazard mapped for one global source and not the other is refused now.

    `config.HAZARD_CATEGORIES` is data meant to be extended — "a partner whose
    vocabulary is not here does not need a code change" — so a one-sided entry
    will happen. Caught at registration, not at build time, because a contract
    is hashed before anything reads a record.
    """

    def test_a_one_sided_mapping_is_refused_for_the_missing_source(self):
        table = {**HAZARD_CATEGORIES, "lightning": {"national_records": ("lightning",)}}
        with mock.patch.dict(contracts.HAZARD_CATEGORIES, table, clear=True):
            make_pilot_contract(hazard="lightning").validate()
            with self.assertRaises(ContractError) as ctx:
                make_pilot_contract(
                    hazard="lightning", source="emdat", file="zz.xlsx",
                    record_start_year=2000,
                )
            self.assertIn("has no 'emdat' mapping", str(ctx.exception))

    def test_the_two_hazard_tables_agree(self):
        # `global_hazards()` is the one statement of "registrable outside the
        # US", read by the contract validator's message and by `readiness
        # hazards`. It is only meaningful while the two tables agree.
        from readiness import config

        self.assertEqual(set(HAZARD_CATEGORIES) - set(HAZARDS), set())
        self.assertEqual(
            config.global_hazards(),
            tuple(h for h in HAZARDS if h in HAZARD_CATEGORIES),
        )
        for hazard, mapping in HAZARD_CATEGORIES.items():
            with self.subTest(hazard=hazard):
                self.assertEqual(sorted(mapping), ["emdat", "national_records"])
                self.assertTrue(all(mapping.values()), hazard)

    def test_the_message_lists_the_hazards_that_can_be_registered(self):
        from readiness import config

        with self.assertRaises(ContractError) as ctx:
            make_pilot_contract(hazard="dust_storm")
        text = str(ctx.exception)
        self.assertIn("inland_flood", text)
        self.assertEqual(
            [h for h in config.global_hazards() if h in text], list(config.global_hazards())
        )


class TestPilotEventTypesAreAlwaysEmpty(unittest.TestCase):
    """Two pilots with identical panels must not carry different digests."""

    def test_a_spec_that_names_event_types_outside_the_us_has_them_dropped(self):
        named = make_pilot_contract(event_types=["Flood"])
        self.assertEqual(named.event_types, ())
        self.assertEqual(named.digest(), make_pilot_contract().digest())

    def test_the_us_still_fills_them_from_the_catalogue(self):
        self.assertEqual(
            make_contract().event_types, HAZARDS["inland_flood"].event_types
        )


class TestTheCrosswalkBasenameIsNotAGroundTruth(unittest.TestCase):
    """`.gitignore` re-includes `snapshots/records/??_emdat_regions.csv`.

    That is the only file in the private directory git will commit, so a
    ground-truth file may not be named like one: the structural guard belongs
    on both sides of the boundary, not only in the ignore file.
    """

    def test_a_crosswalk_shaped_basename_is_refused(self):
        for bad in ("zz_emdat_regions.csv", "ZZ_emdat_regions.csv"):
            with self.subTest(file=bad):
                with self.assertRaises(ContractError) as ctx:
                    make_pilot_contract(file=bad)
                self.assertIn("git does *not* ignore", str(ctx.exception))

    def test_a_name_git_would_not_re_include_is_fine(self):
        # Not two letters: the ignore file no longer re-includes it either.
        make_pilot_contract(file="PARTNER_CONFIDENTIAL_emdat_regions.csv").validate()


class TestPilotDescription(unittest.TestCase):
    def test_describe_names_both_sources(self):
        text = make_pilot_contract(admin_level="ADM2", sha256="c" * 64).describe()
        self.assertIn("ground truth    partner national records zz_records.csv", text)
        self.assertIn("cccccccccccccccc", text)
        self.assertIn("from 2005", text)
        self.assertIn("regions         geoBoundaries ADM2 for ZZ (gbOpen 6.0.0)", text)

    def test_describe_names_the_us_sources_too(self):
        text = make_contract().describe()
        self.assertIn("ground truth    NOAA Storm Events, from 1996", text)
        self.assertIn("regions         US Census counties", text)

    def test_an_emdat_contract_says_so(self):
        text = make_pilot_contract(source="emdat", file="zz_emdat.xlsx").describe()
        self.assertIn("EM-DAT export zz_emdat.xlsx", text)


if __name__ == "__main__":
    unittest.main()
