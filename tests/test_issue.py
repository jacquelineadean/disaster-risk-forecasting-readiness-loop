"""Issuance: the period labels, every guard, and what an issued file records.

No network and no committed artefact is touched: the dataset is the planted-
signal one from `tests.test_orchestrator`, the ledger is written card by card
into a temporary experiments tree by the loop itself, and every issue writes
into a temporary `issued/`.

The test that matters most is the last one: a dataset whose every holdout
label is flipped produces a byte-identical issued file. Issuance refits on the
training split and forecasts a period that has not happened, so an outcome
cannot reach it — and if one ever could, that test fails.
"""

import dataclasses
import inspect
import json
import pathlib
import tempfile
import unittest

from readiness import data as data_mod
from readiness import issue as issue_mod
from readiness.agent import orchestrator
from readiness.connectors.census import County
from readiness.engine import build_model
from readiness.harness.labels import Panel
from readiness.harness.ledger import Ledger
from readiness.harness.splits import TrainingView, split_panel
from readiness.issue import IssueRefused, Issued
from tests.fixtures import make_contract
from tests.test_engine_models import SignalSeries
from tests.test_features import FakeStatic
from tests.test_orchestrator import quick, signal_dataset
from tests.test_verify import append_card

#: The Phase 1 candidate that passes on the planted-signal panel.
CANDIDATE = quick(orchestrator.PHASE1_QUEUE)[3]


def with_regions(dataset: data_mod.Dataset) -> data_mod.Dataset:
    """The same dataset with a region universe, which issuance forecasts over."""
    return dataclasses.replace(
        dataset,
        regions=tuple(
            County(fips, "ZZ", f"County {fips}") for fips in dataset.panel.regions
        ),
    )


def flip(dataset: data_mod.Dataset, years) -> data_mod.Dataset:
    """Every label in `years` turned upside down; units and everything else kept."""
    wanted = set(years)
    panel = dataset.panel
    labels = tuple(
        (1 - label) if unit[1] in wanted else label
        for unit, label in zip(panel.units, panel.labels)
    )
    return dataclasses.replace(dataset, panel=dataclasses.replace(panel, labels=labels))


class TestPeriodLabels(unittest.TestCase):
    """`YYYY-Qn`, `YYYY-Mnn`, `YYYY` — and nothing else, per contract."""

    def setUp(self):
        self.quarterly = make_contract(name="q-zz", period="quarter")
        self.monthly = make_contract(name="m-zz", period="month")
        self.annual = make_contract(name="y-zz", period="year")

    def test_quarter_round_trips(self):
        for period in range(1, 5):
            label = issue_mod.period_label(2026, period, self.quarterly)
            self.assertEqual(label, f"2026-Q{period}")
            self.assertEqual(issue_mod.parse_period(label, self.quarterly), (2026, period))

    def test_month_round_trips_and_is_zero_padded(self):
        self.assertEqual(issue_mod.period_label(2026, 1, self.monthly), "2026-M01")
        self.assertEqual(issue_mod.period_label(2026, 11, self.monthly), "2026-M11")
        self.assertEqual(issue_mod.parse_period("2026-M11", self.monthly), (2026, 11))
        # Lenient in, canonical out: an unpadded label reads, and re-renders padded.
        self.assertEqual(issue_mod.parse_period("2026-M1", self.monthly), (2026, 1))

    def test_year_is_a_bare_year(self):
        self.assertEqual(issue_mod.period_label(2026, 1, self.annual), "2026")
        self.assertEqual(issue_mod.parse_period("2026", self.annual), (2026, 1))

    def test_a_period_after_the_last_split_year_is_allowed(self):
        # The point of the module: the forecast period is one nobody has scored.
        self.assertGreater(2099, self.quarterly.test_years[-1])
        self.assertEqual(issue_mod.parse_period("2099-Q1", self.quarterly), (2099, 1))

    def test_the_shape_must_match_the_contract(self):
        for label, contract in (
            ("2026-M4", self.quarterly),
            ("2026-Q4", self.monthly),
            ("2026-Q1", self.annual),
            ("2026", self.quarterly),
            ("2026-Q5", self.quarterly),
            ("2026-M13", self.monthly),
            ("26-Q1", self.quarterly),
            ("next quarter", self.quarterly),
            ("2026-Q", self.quarterly),
        ):
            with self.subTest(label=label, contract=contract.period):
                with self.assertRaises(IssueRefused):
                    issue_mod.parse_period(label, contract)

    def test_period_label_refuses_an_impossible_period(self):
        with self.assertRaises(IssueRefused):
            issue_mod.period_label(2026, 5, self.quarterly)


class IssueCase(unittest.TestCase):
    """A contract, a signal dataset, and a ledger the loop wrote into a temp tree."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.shared = pathlib.Path(cls._tmp.name)
        cls.contract = make_contract(name="tornado-zz", hazard="tornado")
        cls.dataset = with_regions(signal_dataset(cls.contract, cls.shared))
        cls.experiments = cls.shared / "experiments"
        result = orchestrator.run_local(
            cls.contract,
            experiments_dir=cls.experiments,
            dataset=cls.dataset,
            queue=[CANDIDATE],
            include_canary=False,
            promote=True,
            progress=lambda _m: None,
        )
        cls.card = result.promoted
        assert cls.card is not None and cls.card.status == "PASS", "fixture must promote"

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.issued_dir = self.dir / "issued"

    def tearDown(self):
        self.tmp.cleanup()

    def issue(self, dataset=None, period=(2026, 1), experiments_dir=None, **kw):
        return issue_mod.issue(
            self.contract,
            dataset if dataset is not None else self.dataset,
            CANDIDATE.model,
            CANDIDATE.kwargs,
            period,
            experiments_dir=experiments_dir or self.experiments,
            issued_dir=self.issued_dir,
            **kw,
        )


class TestIssueRefusals(IssueCase):
    def test_refuses_without_test_pass_card(self):
        with self.assertRaises(IssueRefused) as ctx:
            self.issue(experiments_dir=self.dir / "empty")
        self.assertIn("holds no test card", str(ctx.exception))
        self.assertIn("readiness promote", str(ctx.exception))
        self.assertFalse(self.issued_dir.exists())

    def test_refuses_when_only_validate_cards_exist(self):
        where = data_mod.paths(self.contract, experiments_dir=self.dir / "validate-only")
        append_card(Ledger(where.ledger), self.contract, "validate")
        with self.assertRaises(IssueRefused) as ctx:
            self.issue(experiments_dir=self.dir / "validate-only")
        self.assertIn("holds no test card", str(ctx.exception))

    def test_refuses_a_different_model_or_arguments(self):
        with self.assertRaises(IssueRefused) as ctx:
            issue_mod.issue(
                self.contract, self.dataset, CANDIDATE.model,
                {**CANDIDATE.kwargs, "iters": 61}, (2026, 1),
                experiments_dir=self.experiments, issued_dir=self.issued_dir,
            )
        message = str(ctx.exception)
        self.assertIn("no test card", message)
        self.assertIn(self.card.experiment_id, message)  # it names what is there
        self.assertFalse(self.issued_dir.exists())

    def test_refuses_a_rejected_card(self):
        where = data_mod.paths(self.contract, experiments_dir=self.dir / "rejected")
        append_card(
            Ledger(where.ledger), self.contract, "test",
            model=CANDIDATE.model, kwargs=orchestrator.json_kwargs(CANDIDATE.kwargs),
            canary={"rejected": True,
                    "findings": [{"check": "train provenance", "tripped": True,
                                  "detail": "declared digest does not match"}]},
        )
        with self.assertRaises(IssueRefused) as ctx:
            self.issue(experiments_dir=self.dir / "rejected")
        self.assertIn("rejected by the leakage canary", str(ctx.exception))
        self.assertIn("train provenance", str(ctx.exception))

    def test_refuses_a_card_that_failed_the_contract(self):
        where = data_mod.paths(self.contract, experiments_dir=self.dir / "failed")
        append_card(
            Ledger(where.ledger), self.contract, "test", bss=-0.2, auc=0.4,
            model=CANDIDATE.model, kwargs=orchestrator.json_kwargs(CANDIDATE.kwargs),
        )
        with self.assertRaises(IssueRefused) as ctx:
            self.issue(experiments_dir=self.dir / "failed")
        self.assertIn("did not pass contract", str(ctx.exception))
        self.assertIn("brier skill score", str(ctx.exception))

    def test_refuses_train_digest_mismatch(self):
        # A different training panel: the refit is not the fit that was scored.
        other = with_regions(signal_dataset(self.contract, self.dir, n_regions=6))
        with self.assertRaises(IssueRefused) as ctx:
            self.issue(other)
        message = str(ctx.exception)
        self.assertIn("trained on", message)
        self.assertIn(self.card.experiment_id, message)
        self.assertFalse(self.issued_dir.exists())

    def test_refuses_feature_digest_mismatch(self):
        # Same panel, revised extract: the labels are untouched, so only the
        # feature digest moves — which is exactly the silent reissue to refuse.
        revised = dataclasses.replace(
            self.dataset,
            sources={**self.dataset.sources, "era5": SignalSeries(seed=11)},
        )
        with self.assertRaises(IssueRefused) as ctx:
            self.issue(revised)
        self.assertIn("feature digest", str(ctx.exception))
        self.assertFalse(self.issued_dir.exists())

    def test_refuses_a_period_the_data_does_not_reach(self):
        # The signal series ends in December 2025. 2026-Q2 starts in April, the
        # cutoff is March (one month of lag), and only months strictly before
        # the cutoff are read — so the data has to reach February 2026.
        with self.assertRaises(IssueRefused) as ctx:
            self.issue(period=(2026, 2))
        message = str(ctx.exception)
        self.assertIn("period cannot be issued yet: data through 2026-02 needed", message)
        self.assertIn("era5 ends at 2025-12", message)
        self.assertIn("precip", message)
        self.assertFalse(self.issued_dir.exists())

    def test_refuses_a_dataset_with_no_region_universe(self):
        with self.assertRaises(IssueRefused) as ctx:
            self.issue(dataclasses.replace(self.dataset, regions=()))
        self.assertIn("no region universe", str(ctx.exception))

    def test_refuses_a_dataset_built_for_another_contract(self):
        other = make_contract(name="tornado-zz", thresholds={"min_auc": 0.71})
        with self.assertRaises(IssueRefused) as ctx:
            issue_mod.issue(
                other, self.dataset, CANDIDATE.model, CANDIDATE.kwargs, (2026, 1),
                experiments_dir=self.experiments, issued_dir=self.issued_dir,
            )
        self.assertIn("the dataset was built for", str(ctx.exception))


class TestIssuableYears(IssueCase):
    """Which periods may be issued at all: after the contract, and not far after."""

    def test_a_year_the_contract_spans_is_refused(self):
        # A test-year "forecast" would be a per-county reading of the holdout
        # with no touch spent; a train- or validate-year one an in-sample fit
        # published under the test card's skill claim.
        for year, split in ((2000, "train"), (2018, "validate"), (2024, "test")):
            with self.subTest(year=year), self.assertRaises(IssueRefused) as ctx:
                self.issue(period=(year, 1))
            message = str(ctx.exception)
            self.assertIn(f"{year} is a {split} year", message)
            self.assertIn("The first issuable period is 2026-Q1", message)
        self.assertFalse(self.issued_dir.exists())

    def test_the_first_issuable_period_is_the_one_after_the_last_contract_year(self):
        self.assertEqual(issue_mod.first_issuable(self.contract), (2026, 1))
        self.assertEqual(self.contract.test_years[-1], 2025)
        self.assertEqual(self.issue(period=(2026, 1)).period_label, "2026-Q1")


class FeaturelessCase(IssueCase):
    """The history-only logistic: a model that reads no feature source at all.

    The queue does not promote it on this panel (the featured candidate passes
    first), so its test card is written by hand — with the digest the refit
    will actually produce, which is the one thing `issue` never takes on trust.
    """

    KWARGS = {"feature_sets": [], "iters": 60}

    def setUp(self):
        super().setUp()
        self.experiments_hist = self.dir / "history-only"
        model = build_model("logistic", **self.KWARGS)
        train = self.contract.splits.train
        model.fit(TrainingView(split_panel(self.dataset.panel, train), train, None))
        self.assertEqual(getattr(model, "feature_specs", ()), ())
        where = data_mod.paths(self.contract, experiments_dir=self.experiments_hist)
        self.card_hist = append_card(
            Ledger(where.ledger), self.contract, "test", model=model.name,
            version=model.version, kwargs=dict(self.KWARGS), columns=(),
            train_digest=model.training_digest,
        )

    def issue_hist(self, period=(2026, 1), **kw):
        return issue_mod.issue(
            self.contract, self.dataset, "logistic", dict(self.KWARGS), period,
            experiments_dir=self.experiments_hist, issued_dir=self.issued_dir, **kw,
        )


class TestFeaturelessHorizon(FeaturelessCase):
    def test_the_first_period_after_the_contract_is_issuable(self):
        issued = self.issue_hist()
        self.assertEqual(issued.period_label, "2026-Q1")
        self.assertEqual(issued.validated_by, self.card_hist.experiment_id)
        self.assertEqual(issued.feature_digest, "")

    def test_a_year_the_contract_spans_is_refused_here_too(self):
        # The bound is not a property of having features: it is the calendar.
        for year, split in ((2000, "train"), (2018, "validate"), (2024, "test")):
            with self.subTest(year=year), self.assertRaises(IssueRefused) as ctx:
                self.issue_hist(period=(year, 1))
            self.assertIn(f"{year} is a {split} year", str(ctx.exception))
        self.assertFalse(self.issued_dir.exists())

    def test_a_period_past_the_one_period_horizon_is_refused(self):
        # A featured model's horizon is how far its pinned series reach. This
        # model reads no series, so its horizon is the first period after the
        # contract and no further — 2999-Q1 is a number about nothing.
        for period in ((2026, 2), (2027, 1), (2999, 1)):
            with self.subTest(period=period), self.assertRaises(IssueRefused) as ctx:
                self.issue_hist(period=period)
            message = str(ctx.exception)
            self.assertIn("reads no feature series", message)
            self.assertIn("2026-Q1 is the first period after the contract's last "
                          "year (2025)", message)
        self.assertFalse(self.issued_dir.exists())


class TestReissue(IssueCase):
    def test_an_existing_issued_file_is_not_overwritten(self):
        first = self.issue()
        path = self.issued_dir / "tornado-zz" / "2026-Q1.json"
        with self.assertRaises(IssueRefused) as ctx:
            self.issue()
        message = str(ctx.exception)
        self.assertIn("already exists", message)
        self.assertIn(first.issued_at, message)
        self.assertIn("--reissue", message)
        self.assertEqual(Issued.read(path).to_dict(), first.to_dict())

    def test_reissue_replaces_the_file_and_names_what_it_replaced(self):
        first = self.issue()
        path = self.issued_dir / "tornado-zz" / "2026-Q1.json"
        lines: list[str] = []
        second = self.issue(reissue=True, progress=lines.append)
        replaced = [ln for ln in lines if "reissued" in ln]
        self.assertEqual(len(replaced), 1)
        self.assertIn("2026-Q1.json", replaced[0])
        self.assertIn(f"replacing the file issued at {first.issued_at}", replaced[0])
        self.assertEqual(Issued.read(path).to_dict(), second.to_dict())
        self.assertFalse(any("wrote" in ln for ln in lines))


class TestLedgerGuards(IssueCase):
    def test_a_passing_test_card_under_another_digest_is_no_test_card(self):
        # Re-registering the criteria does not make an old card's numbers a
        # judgement about the new ones.
        other = make_contract(name="tornado-zz", hazard="tornado",
                              thresholds={"min_auc": 0.71})
        self.assertNotEqual(other.digest(), self.contract.digest())
        where = data_mod.paths(self.contract, experiments_dir=self.dir / "other-digest")
        card = append_card(
            Ledger(where.ledger), other, "test",
            kwargs=orchestrator.json_kwargs(CANDIDATE.kwargs),
        )
        self.assertEqual(card.status, "PASS")
        with self.assertRaises(IssueRefused) as ctx:
            self.issue(experiments_dir=self.dir / "other-digest")
        message = str(ctx.exception)
        self.assertIn("no test card", message)
        self.assertIn(self.contract.digest(), message)
        self.assertFalse(self.issued_dir.exists())

    def test_an_unclean_feature_audit_is_refused_and_writes_nothing(self):
        # The same values, so the frame and its digest are unchanged and this
        # is the audit's refusal alone: a static layer maintained through 2023
        # has seen the holdout years and is not admissible as a feature.
        inadmissible = dataclasses.replace(
            self.dataset,
            sources={**self.dataset.sources,
                     "gazetteer": FakeStatic("gazetteer", derived_through=2023)},
        )
        with self.assertRaises(IssueRefused) as ctx:
            self.issue(inadmissible)
        message = str(ctx.exception)
        self.assertIn("the feature audit is not clean", message)
        self.assertIn("gazetteer", message)
        self.assertFalse(self.issued_dir.exists())


class TestIssuedFile(IssueCase):
    def test_issued_carries_provenance_and_every_region(self):
        issued = self.issue()
        path = self.issued_dir / "tornado-zz" / "2026-Q1.json"
        self.assertTrue(path.exists())
        self.assertEqual(
            sorted(issued.probabilities), sorted(r.fips for r in self.dataset.regions)
        )
        self.assertTrue(all(0.0 <= p <= 1.0 for p in issued.probabilities.values()))
        self.assertEqual(issued.validated_by, self.card.experiment_id)
        self.assertEqual(issued.model, self.card.model)
        self.assertEqual(issued.version, self.card.version)
        self.assertEqual(issued.model_kwargs, self.card.data_snapshot["model_kwargs"])
        self.assertEqual(issued.contract_digest, self.contract.digest())
        self.assertEqual(issued.train_digest, self.card.scorecard["train_digest"])
        self.assertEqual(issued.feature_digest, self.card.scorecard["feature_digest"])
        self.assertEqual(issued.data_version, self.dataset.data_version)
        self.assertEqual(issued.feature_version, self.dataset.feature_version)
        self.assertEqual(issued.period, (2026, 1))
        self.assertEqual(issued.period_label, "2026-Q1")
        self.assertTrue(issued.harness_digest)
        self.assertTrue(issued.issued_at.endswith("+00:00"))
        self.assertIn(data_mod.CENSUS_KEY, issued.inputs)
        self.assertIn("open-meteo/era5/99", issued.inputs)
        # The file is the object: reading it back gives the same record.
        self.assertEqual(Issued.read(path).to_dict(), issued.to_dict())
        self.assertEqual(json.loads(path.read_text())["period"], [2026, 1])

    def test_the_file_has_no_field_for_an_outcome(self):
        issued = self.issue()
        forbidden = {"labels", "label", "outcome", "outcomes", "truth", "observed"}
        self.assertEqual(set(issued.to_dict()) & forbidden, set())
        self.assertEqual(set(f.name for f in dataclasses.fields(Issued)) & forbidden, set())

    def test_issue_has_no_parameter_through_which_a_label_could_arrive(self):
        names = set(inspect.signature(issue_mod.issue).parameters)
        self.assertEqual(
            names,
            {"contract", "dataset", "model_name", "kwargs", "period",
             "experiments_dir", "issued_dir", "reissue", "progress"},
        )

    def test_reading_and_finding_issued_files(self):
        self.issue()
        found = issue_mod.read_issued("tornado-zz", self.issued_dir)
        self.assertEqual([i.period_label for i in found], ["2026-Q1"])
        region = sorted(found[0].probabilities)[0]
        self.assertEqual(
            [i.contract for i in issue_mod.covering(region, "2026-Q1", self.issued_dir)],
            ["tornado-zz"],
        )
        self.assertEqual(issue_mod.covering("00000", "2026-Q1", self.issued_dir), [])

    def test_the_issued_directory_can_be_pointed_elsewhere_by_environment(self):
        from unittest import mock

        with mock.patch.dict(
            "os.environ", {issue_mod.ISSUED_DIR_ENV: str(self.dir / "elsewhere")}
        ):
            self.assertEqual(
                issue_mod.issued_path("c", "2026-Q1"),
                self.dir / "elsewhere" / "c" / "2026-Q1.json",
            )

    def test_flipping_every_holdout_label_changes_nothing(self):
        first = self.issue().to_dict()
        holdout = self.contract.validate_years + self.contract.test_years
        second = self.issue(flip(self.dataset, holdout), reissue=True).to_dict()
        for record in (first, second):
            record.pop("issued_at")
        self.assertEqual(first, second)

    def test_flipping_a_training_label_is_refused_not_reissued(self):
        # The other half of the same property: the training labels are part of
        # the fit, so moving one moves the digest and nothing is published.
        with self.assertRaises(IssueRefused) as ctx:
            self.issue(flip(self.dataset, self.contract.train_years[:1]))
        self.assertIn("trained on", str(ctx.exception))


class TestPanelIsUnchanged(unittest.TestCase):
    def test_flip_helper_only_touches_the_years_it_is_given(self):
        # The fixture the label-independence test leans on, checked itself.
        contract = make_contract(name="tornado-zz")
        panel = Panel(
            (("99001", 2016, 1), ("99001", 1996, 1)), (0, 0),
            hazard=contract.hazard, scope=contract.scope_key, period=contract.period,
        )
        dataset = dataclasses.replace(
            with_regions(signal_dataset(contract, pathlib.Path("."))), panel=panel
        )
        flipped = flip(dataset, (2016,))
        self.assertEqual(flipped.panel.labels, (1, 0))
        self.assertEqual(flipped.panel.units, panel.units)


if __name__ == "__main__":
    unittest.main()
