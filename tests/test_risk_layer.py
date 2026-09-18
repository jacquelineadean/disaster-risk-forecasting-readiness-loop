"""The risk layer: issued files only, and an absence that is stated, not filled."""

import os
import pathlib
import tempfile
import unittest
from unittest import mock

from readiness import data as data_mod
from readiness.harness.ledger import Ledger
from readiness.plans.risk import RiskLayer
from tests import fixtures_plans as fp
from tests.fixtures import make_contract
from tests.test_verify import append_card


class RiskCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.issued_dir = self.dir / "issued"
        self.experiments = self.dir / "experiments"

    def tearDown(self):
        self.tmp.cleanup()


class TestLoading(RiskCase):
    def test_reads_only_issued_and_states_absence(self):
        fp.make_issued().write(self.issued_dir)
        layer = RiskLayer.load(
            fp.PERIOD, [fp.COUNTY, fp.OTHER_COUNTY, "99999"],
            issued_dir=self.issued_dir, experiments_dir=self.experiments,
        )
        self.assertEqual(layer.contracts(), (fp.CONTRACT,))
        self.assertEqual(layer.covering(fp.COUNTY), (fp.CONTRACT,))
        self.assertEqual(layer.covering("99999"), ())

        claim = layer.probability(fp.COUNTY, fp.CONTRACT)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.value, 0.18)
        self.assertEqual(claim.source.kind, "issued")
        self.assertEqual(
            claim.source.ref, f"issued/{fp.CONTRACT}/{fp.PERIOD}.json#{fp.PERIOD}"
        )

        # A county nothing covers gets no number at all, and the absence is a
        # claim of its own rather than a quieter number.
        self.assertIsNone(layer.probability("99999", fp.CONTRACT))
        absent = layer.absence("99999", derived_from="f-county_fips")
        self.assertIsNone(absent.value)
        self.assertEqual(absent.source.kind, "computed")
        self.assertEqual(absent.source.ref, "f-county_fips")
        self.assertIn("no validated issuance covers county 99999", absent.text)
        self.assertIn(fp.PERIOD, absent.text)

    def test_another_period_is_not_read(self):
        fp.make_issued(label="2026-Q3").write(self.issued_dir)
        layer = RiskLayer.load(
            fp.PERIOD, [fp.COUNTY], issued_dir=self.issued_dir,
            experiments_dir=self.experiments,
        )
        self.assertEqual(layer.issued, {})
        self.assertIsNone(layer.probability(fp.COUNTY, fp.CONTRACT))

    def test_an_empty_tree_is_an_empty_layer_not_an_error(self):
        layer = RiskLayer.load(
            fp.PERIOD, [fp.COUNTY], issued_dir=self.dir / "nothing",
            experiments_dir=self.experiments,
        )
        self.assertEqual(layer.probabilities, {})
        self.assertIn("0 issued file(s) (none)", RiskLayer.empty(fp.PERIOD).summary())

    def test_the_backing_test_card_is_loaded_for_citation(self):
        contract = make_contract(name=fp.CONTRACT)
        where = data_mod.paths(contract, experiments_dir=self.experiments)
        ledger = Ledger(where.ledger)
        append_card(ledger, contract, "validate")
        card = append_card(ledger, contract, "test")
        self.assertEqual(card.experiment_id, "exp-0002")  # what the fixture names
        fp.make_issued().write(self.issued_dir)
        layer = RiskLayer.load(
            fp.PERIOD, [fp.COUNTY], issued_dir=self.issued_dir,
            experiments_dir=self.experiments,
        )
        self.assertIn(f"{fp.CONTRACT}/exp-0002", layer.cards)
        self.assertIn("exp-0002", layer.cards)
        claim = layer.card_claim(fp.CONTRACT)
        self.assertEqual(claim.source.kind, "ledger")
        self.assertEqual(claim.source.ref, f"{fp.CONTRACT}/exp-0002")

    def test_a_missing_ledger_leaves_the_probabilities_intact(self):
        fp.make_issued().write(self.issued_dir)
        layer = RiskLayer.load(
            fp.PERIOD, [fp.COUNTY], issued_dir=self.issued_dir,
            experiments_dir=self.dir / "no-experiments",
        )
        self.assertEqual(layer.cards, {})
        self.assertIsNone(layer.card_claim(fp.CONTRACT))
        self.assertIsNotNone(layer.probability(fp.COUNTY, fp.CONTRACT))

    def test_the_experiments_root_environment_variable_is_followed(self):
        fp.make_issued().write(self.issued_dir)
        with mock.patch.dict(
            os.environ, {data_mod.EXPERIMENTS_DIR_ENV: str(self.experiments)}
        ):
            layer = RiskLayer.load(
                fp.PERIOD, [fp.COUNTY], issued_dir=self.issued_dir
            )
        self.assertEqual(layer.cards, {})


class TestCitationGrammar(unittest.TestCase):
    def test_issued_refs_are_the_paths_a_document_cites(self):
        layer = fp.make_risk()
        self.assertEqual(
            layer.issued_refs(), {f"issued/{fp.CONTRACT}/{fp.PERIOD}.json": {fp.PERIOD}}
        )

    def test_a_probability_claim_renders_as_a_percentage(self):
        from readiness import cite

        claim = fp.make_risk().probability(fp.COUNTY, fp.CONTRACT)
        self.assertEqual(cite.render_value(claim), "18%")

    def test_the_summary_names_what_was_read(self):
        summary = fp.make_risk().summary()
        self.assertIn(fp.PERIOD, summary)
        self.assertIn(fp.CONTRACT, summary)


if __name__ == "__main__":
    unittest.main()
