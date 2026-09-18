"""The whole loop, run somewhere that is not the United States.

Plan §5's exit is "the Phase 1 contract passes in two non-US pilots using only
globally available data". The claim underneath it is that *the architecture does
not change, the connectors do* — so the test worth writing is not that a new
code path works, but that the old one does, unmodified, on a contract whose
ground truth and region universe are the global ones.

Nothing here reaches the network, and nothing reads a real pilot's ground
truth: the panel is synthetic and the fictional country is `ZZ` (and `ZY`),
user-assigned ISO codes that can never collide with a real place. What is real
is the machinery: the Phase 1 queue, the feature firewall, the canary, the
ledger, the one atomic test touch, `verify --phase 1` and `verify --phase 4`.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from readiness import backtest, data as data_mod, verify
from readiness.agent import orchestrator, subagents
from readiness.connectors.base import Manifest, SourceRecord, utc_now
from readiness.harness.labels import diagnose
from readiness.harness.ledger import Ledger
from tests.fixtures import (
    make_contract,
    make_pilot_contract,
    make_signal_panel,
    shape_id,
)
from tests.test_engine_models import FakeElevation, SignalSeries
from tests.test_features import FakeStatic
from tests.test_orchestrator import quick


#: The Phase 1 queue a pilot can actually run today.
#:
#: The catalogue's `terrain` set reads the Census Gazetteer (water share,
#: latitude), which is US-only, so the two candidates that ask for it are
#: skipped outside the US — honestly, with a progress line and no card, because
#: "could not be run" is not an experiment. What remains is the history-only
#: model, the ERA5 antecedent model and its isotonic recalibration, which is
#: enough to pass the contract. A global terrain source (geoBoundaries
#: centroids carry latitude; the ERA5 extract carries elevation) is the obvious
#: next connector and is deliberately not invented here.
def pilot_queue() -> list:
    import dataclasses

    from readiness.agent.orchestrator import PHASE1_QUEUE

    era5_only = dataclasses.replace(
        PHASE1_QUEUE[3], kwargs={"feature_sets": ["era5-antecedent"]}
    )
    return quick([PHASE1_QUEUE[0], PHASE1_QUEUE[1], era5_only])


class GlobalSeries(SignalSeries):
    """The same planted-signal series, filed under a pilot's ERA5 extract.

    Open-Meteo is already global, so the only thing that changes between a US
    contract and a pilot is the label the extract is pinned under: `.../99` for
    a state, `.../ZZ` for a country.
    """

    def __init__(self, country: str = "ZZ", seed: int = 7) -> None:
        super().__init__(seed=seed)
        self.manifest_keys = (f"open-meteo/era5/{country}",)


class GlobalElevation(FakeElevation):
    def __init__(self, country: str = "ZZ") -> None:
        super().__init__()
        self.manifest_keys = (f"open-meteo/era5/{country}",)


def pilot_dataset(contract, tmp: pathlib.Path, n_regions: int = 8) -> data_mod.Dataset:
    """A dataset shaped exactly as `data.build` leaves one for a pilot.

    Region ids are geoBoundaries shapeIDs, the inputs are the ones
    `data.input_keys` names for this contract, and the feature source is the
    country's ERA5 extract. The panel carries planted antecedent-precipitation
    signal so that a candidate can genuinely pass rather than pass by luck.
    """
    series = GlobalSeries(contract.country)
    panel = make_signal_panel(
        contract, series, n_regions=n_regions,
        region_id_of=lambda i: shape_id(i, contract.country, contract.admin_level),
    )
    sources = {"era5": series, "elevation": GlobalElevation(contract.country)}
    keys = tuple(sorted({k for s in sources.values() for k in s.manifest_keys}))
    return data_mod.Dataset(
        contract=contract,
        panel=panel,
        regions=(),
        data_version="synthetic",
        manifest=Manifest(path=tmp / "manifest.json"),
        diagnostics=diagnose([], panel.regions, panel.years, contract),
        sources=dict(sources),
        feature_version="synthetic-features",
        feature_inputs=keys,
    )


class TestPilotLoop(unittest.TestCase):
    """One pilot, end to end: the Phase 1 queue, a promotion, `verify --phase 1`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        # `backtest.write` resolves the contract's paths through the
        # environment, so it must point at the same tree the loop writes into.
        self.env = mock.patch.dict(
            os.environ, {data_mod.EXPERIMENTS_DIR_ENV: str(self.dir)}
        )
        self.env.start()
        self.contract = make_pilot_contract(name="flood-zz")
        self.dataset = pilot_dataset(self.contract, self.dir)
        self.where = data_mod.paths(self.contract, experiments_dir=self.dir)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_loop(self, queue=None, **kw):
        return orchestrator.run_local(
            self.contract, experiments_dir=self.dir, dataset=self.dataset,
            queue=queue if queue is not None else pilot_queue(), include_canary=True,
            progress=lambda _m: None, **kw,
        )

    def test_the_phase1_queue_runs_unchanged_on_a_pilot(self):
        result = self.run_loop()
        self.assertEqual(
            [c.model for c in result.cards],
            ["logistic", "logistic", "logistic+iso", "leaky-oracle"],
        )
        self.assertEqual(result.rejected, ["leaky-oracle"])
        self.assertEqual(result.passed, ["logistic+iso[era5-antecedent]"])
        self.assertTrue(Ledger(self.where.ledger).verify().valid)

    def test_a_candidate_that_needs_a_us_only_source_is_skipped_not_faked(self):
        # `terrain` reads the Census Gazetteer. Outside the US there is nothing
        # for it to read, so the candidate is skipped with a progress line and
        # no card — rather than scored against a frame of missing values, which
        # would look like an experiment and be one.
        lines: list[str] = []
        result = orchestrator.run_local(
            self.contract, experiments_dir=self.dir, dataset=self.dataset,
            queue=quick(orchestrator.PHASE1_QUEUE), include_canary=False,
            progress=lines.append,
        )
        self.assertEqual(
            result.skipped,
            ["logistic[era5-antecedent+terrain]", "logistic+iso[era5-antecedent+terrain]",
             "gbm[era5-antecedent+terrain]", "gbm+iso[era5-antecedent+terrain]"],
        )
        self.assertEqual([c.model for c in result.cards], ["logistic", "logistic"])
        self.assertTrue(any("gazetteer" in line for line in lines), lines)

    def test_every_card_records_the_pilots_own_inputs(self):
        result = self.run_loop()
        for card in result.cards:
            with self.subTest(model=card.model):
                snapshot = card.data_snapshot
                self.assertEqual(snapshot["scope"], "ZZ:all")
                self.assertEqual(
                    snapshot["inputs"],
                    ["geoboundaries/ZZ/ADM1", "records/ZZ/zz_records.csv"],
                )
                self.assertEqual(snapshot["feature_inputs"], ["open-meteo/era5/ZZ"])

    def test_the_feature_firewall_is_the_same_firewall(self):
        result = self.run_loop()
        audits = [
            c.scorecard["feature_audit"] for c in result.cards
            if c.scorecard.get("feature_audit")
        ]
        self.assertTrue(audits)
        for audit in audits:
            self.assertTrue(audit["clean"], audit)

    def test_promote_and_verify_phase1_on_a_synthetic_non_us_contract(self):
        result = self.run_loop(promote=True)
        self.assertIsNotNone(result.promoted)
        self.assertEqual(result.promoted.split, "test")
        backtest.write(self.contract, self.where.directory / "backtest.html")
        outcome = verify.phase1(
            self.contract,
            ledger_path=self.where.ledger,
            touch_path=self.where.touch_budget,
            backtest_path=self.where.directory / "backtest.html",
        )
        self.assertTrue(outcome.passed, outcome.failures())

    def test_the_system_prompt_names_the_record_the_pilot_is_judged_against(self):
        text = orchestrator.system_prompt(self.contract)
        self.assertIn("partner national records zz_records.csv", text)
        self.assertNotIn("Storm Events", text)
        self.assertIn("geoBoundaries ADM1 for ZZ", text)
        # ...and the subagents stay place-agnostic, as they are in the US.
        for spec in subagents.subagents_for(self.contract).values():
            self.assertNotIn("county", spec["prompt"].lower())


class TestTwoPilotsMeetPhase4(unittest.TestCase):
    """The exit itself: two pilots, both passing, on global inputs only."""

    PILOTS = (
        dict(name="flood-zz", hazard="inland_flood", country="ZZ",
             source="national_records", file="zz_records.csv", sha256="a" * 64,
             admin_level="ADM1"),
        dict(name="cyclone-zy", hazard="tropical_cyclone", country="ZY",
             source="emdat", file="zy_emdat.xlsx", sha256="b" * 64,
             record_start_year=2000, admin_level="ADM2"),
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.experiments = self.dir / "experiments"
        self.snapshots = self.dir / "snapshots"
        self.env = mock.patch.dict(
            os.environ, {data_mod.EXPERIMENTS_DIR_ENV: str(self.experiments)}
        )
        self.env.start()
        self.registry = {}
        self.manifest = Manifest(path=self.snapshots / "manifest.json")
        for spec in self.PILOTS:
            contract = make_pilot_contract(**spec)
            self.registry[contract.name] = contract
            self.run_to_a_test_card(contract)
            self.pin_record(contract)
        self.manifest.save()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_to_a_test_card(self, contract, sources=None):
        dataset = pilot_dataset(contract, self.dir)
        if sources is not None:
            dataset = data_mod.Dataset(
                contract=dataset.contract, panel=dataset.panel, regions=(),
                data_version=dataset.data_version, manifest=dataset.manifest,
                diagnostics=dataset.diagnostics, sources=sources,
                feature_version="synthetic-features",
                feature_inputs=tuple(
                    sorted({k for s in sources.values() for k in s.manifest_keys})
                ),
            )
        result = orchestrator.run_local(
            contract, experiments_dir=self.experiments, dataset=dataset,
            queue=pilot_queue(), include_canary=False,
            promote=True, progress=lambda _m: None,
        )
        self.assertIsNotNone(result.promoted, f"{contract.name}: nothing promoted")
        where = data_mod.paths(contract, experiments_dir=self.experiments)
        backtest.write(contract, where.directory / "backtest.html")
        return result

    def pin_record(self, contract) -> None:
        self.manifest.add(
            data_mod.records_key(contract),
            SourceRecord(
                source="the partner", url="",
                sha256=contract.ground_truth["sha256"], bytes=1,
                fetched_at=utc_now(), license="partner data; not redistributed",
            ),
        )

    def phase4(self):
        return verify.phase4(
            registry=self.registry,
            experiments_dir=self.experiments,
            snapshot_dir=self.snapshots,
        )

    def test_two_synthetic_pilots_meet_every_criterion(self):
        result = self.phase4()
        self.assertTrue(
            result.passed, [c.detail for c in result.checks if not c.passed]
        )

    def test_a_us_only_connector_in_one_pilots_inputs_fails(self):
        # The same loop, the same pass — but one pilot's features came from the
        # Census Gazetteer, which does not exist outside the US. The result no
        # longer demonstrates what the exit claims, and the check says so.
        contract = self.registry["flood-zz"]
        where = data_mod.paths(contract, experiments_dir=self.experiments)
        for path in (where.ledger, where.touch_budget,
                     where.ledger.with_suffix(".jsonl.anchor.json")):
            if path.exists():
                path.unlink()
        series = GlobalSeries(contract.country)
        self.run_to_a_test_card(
            contract,
            sources={"era5": series, "gazetteer": FakeStatic(),
                     "elevation": GlobalElevation(contract.country)},
        )
        result = self.phase4()
        self.assertFalse(result.passed)
        detail = next(c for c in result.checks if c.name == "global inputs").detail
        self.assertIn("census/gazetteer_counties2020", detail)
        self.assertIn("flood-zz", detail)

    def test_the_us_digests_are_still_the_committed_ones(self):
        check = next(c for c in self.phase4().checks if c.name == "us digests")
        self.assertTrue(check.passed, check.detail)

    def test_the_exit_is_reached_without_any_ground_truth_bytes_on_disk(self):
        # Both pilots pass, and `snapshots/records/` — the one place a partner
        # archive or an EM-DAT export may live — was never created. The exit
        # criterion is checked from the committed ledger and the pinned hash,
        # which is what makes it checkable at all: the bytes are not in this
        # repository and never will be.
        for contract in self.registry.values():
            self.assertFalse(
                data_mod.records_path(contract, self.snapshots).exists(),
                contract.name,
            )
        self.assertFalse((self.snapshots / data_mod.RECORDS_DIRNAME).exists())
        self.assertTrue(self.phase4().passed)

    def test_the_cards_are_readable_json_with_the_pilots_provenance(self):
        for name, contract in self.registry.items():
            where = data_mod.paths(contract, experiments_dir=self.experiments)
            cards = [json.loads(line) for line in where.ledger.read_text().splitlines()]
            with self.subTest(contract=name):
                self.assertTrue(cards)
                self.assertEqual(cards[-1]["split"], "test")
                self.assertEqual(
                    cards[-1]["data_snapshot"]["scope"], f"{contract.country}:all"
                )


class TestPilotsDoNotCountAsNationalContracts(unittest.TestCase):
    """A pilot's `states` is empty because it is not in the US at all.

    Phase 2's criterion is four *national* contracts over the Storm Events
    record. A pilot would otherwise satisfy it by accident, because the test
    for "national" was an empty state list.
    """

    def setUp(self):
        self.registry = {
            "flood-us": make_contract(name="flood-us", scope={"states": []}),
            "tornado-la": make_contract(name="tornado-la", scope={"states": ["LA"]}),
            "flood-zz": make_pilot_contract(name="flood-zz"),
        }

    def test_fleet_national_excludes_the_pilot(self):
        from readiness import fleet

        self.assertEqual(sorted(fleet.national(self.registry)), ["flood-us"])

    def test_phase2_counts_only_the_us_national_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = verify.phase2(
                registry=self.registry,
                experiments_dir=pathlib.Path(tmp) / "experiments",
                counts_path=pathlib.Path(tmp) / "counts.csv",
                issued_dir=pathlib.Path(tmp) / "issued",
                briefs_dir=pathlib.Path(tmp) / "briefs",
                snapshot_dir=pathlib.Path(tmp) / "snapshots",
            )
        national = next(c for c in result.checks if c.name == "national contracts")
        self.assertIn("0/1", national.detail)
        self.assertNotIn("flood-zz", national.detail)


if __name__ == "__main__":
    unittest.main()
