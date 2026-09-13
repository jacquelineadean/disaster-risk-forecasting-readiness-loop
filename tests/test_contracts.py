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
from readiness.config import HAZARDS, RECORD_START_YEAR
from readiness.contracts import Contract, ContractError
from tests.fixtures import make_contract


class TestConstruction(unittest.TestCase):
    def test_defaults_fill_in(self):
        c = Contract.from_spec(
            {"name": "x", "hazard": "tornado", "splits": {"train": [1996, 2015],
             "validate": [2016, 2020], "test": [2021, 2025]}}
        )
        self.assertEqual(c.event_types, HAZARDS["tornado"].event_types)
        self.assertEqual(c.states, ())
        self.assertEqual(c.period, "quarter")
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
        for needle in ("test-hazard", "inland_flood", "US, ZZ", "region x quarter", "AUC"):
            self.assertIn(needle, text)


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

    def test_country_must_be_supported(self):
        self.assert_rejected("not supported", scope={"country": "FR", "states": []})

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

    def test_reference_model_must_be_computable(self):
        self.assert_rejected("reference model", reference_model="oracle")

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


if __name__ == "__main__":
    unittest.main()
