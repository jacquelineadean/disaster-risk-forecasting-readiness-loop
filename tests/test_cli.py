"""The command line, without any network.

Contract selection and registration run against a temporary registry. The
data commands (`panel`, `score`, `canary`, `loop`, `verify`) run against a
synthetic `Dataset` injected in place of `data.build`, with the experiments
tree and the blessed-fingerprint directory pointed at a temporary directory so
the committed ledgers and `harness_expected/` are never touched. The Phase 1
commands (`features`, `loop --queue phase1`, `promote`, `backtest`,
`verify --phase 1`) run against a dataset with synthetic feature sources.
"""

import contextlib
import functools
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from readiness import brief as brief_mod
from readiness import cite, cli, contracts, data as data_mod, verify
from readiness import issue as issue_mod
from readiness.agent import orchestrator
from readiness.connectors import usa_structures
from readiness.connectors.base import Manifest
from readiness.connectors.census import County
from readiness.harness.contract import Check
from readiness.harness.labels import diagnose
from readiness.plans import gap_report as gap_report_mod
from readiness.plans import reviews as reviews_mod
from tests import fixtures_plans
from tests.fixtures import make_panel
from tests.test_features import FakeStatic
from tests.test_issue import CANDIDATE, with_regions
from tests.test_orchestrator import quick, signal_dataset
from tests.test_verify import SPOT_COUNTIES, counts_csv, pin_counts


def synthetic_dataset(contract, tmp: pathlib.Path) -> data_mod.Dataset:
    """A dataset with a region universe, as the CLI's `panel` output needs one."""
    panel = make_panel(contract=contract, n_regions=10)
    state = contract.states[0] if contract.states else "ZZ"
    return data_mod.Dataset(
        contract=contract,
        panel=panel,
        regions=tuple(County(r, state, f"county {r}") for r in panel.regions),
        data_version="synthetic",
        manifest=Manifest(path=tmp / "manifest.json"),
        diagnostics=diagnose([], panel.regions, panel.years, contract),
    )


#: `register` option destination -> the `contracts.DEFAULTS` entry it must equal.
REGISTER_DEFAULTS = {
    "period": "period",
    "damage_usd": "property_usd_min",
    "zone_policy": "zone_policy",
    "train": "train",
    "validate": "validate",
    "test": "test",
    "min_bss": "min_brier_skill_score",
    "tolerance": "reliability_tolerance_pp",
    "min_bin_count": "reliability_min_bin_count",
    "min_auc": "min_auc",
    "bins": "n_reliability_bins",
    "version": "version",
}


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.env = mock.patch.dict(
            os.environ, {contracts.CONTRACTS_DIR_ENV: str(self.dir)}
        )
        self.env.start()
        os.environ.pop(contracts.CONTRACT_ENV, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()


class TestParser(unittest.TestCase):
    def test_every_data_command_takes_a_contract_flag(self):
        parser = cli.build_parser()
        for command in ("contract", "snapshot", "panel", "loop", "canary", "ledger",
                        "verify", "mcp"):
            with self.subTest(command=command):
                args = parser.parse_args([command, "-c", "x"])
                self.assertEqual(args.contract, "x")
        args = parser.parse_args(["score", "climatology-pooled", "-c", "x"])
        self.assertEqual(args.contract, "x")

    def test_register_requires_a_hazard(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["register", "x"])

    def test_every_register_default_is_the_contracts_default(self):
        args = cli.build_parser().parse_args(["register", "x", "--hazard", "tornado"])
        for dest, key in REGISTER_DEFAULTS.items():
            with self.subTest(option=dest):
                self.assertEqual(getattr(args, dest), contracts.DEFAULTS[key])
        self.assertEqual((not args.no_casualties), contracts.DEFAULTS["count_casualties"])

    def test_register_help_quotes_the_contracts_defaults(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["register", "--help"])
        text = out.getvalue()
        d = contracts.DEFAULTS
        for needle in (d["train"], d["validate"], d["test"], str(d["min_auc"]),
                       str(d["reliability_tolerance_pp"]),
                       str(d["reliability_min_bin_count"]), str(d["n_reliability_bins"])):
            self.assertIn(needle, text)


class TestRegistration(CliCase):
    def test_register_writes_a_valid_contract(self):
        code, out = self.run_cli(
            "register", "tornado-zz", "--hazard", "tornado", "--state", "zz",
            "--description", "test",
        )
        self.assertEqual(code, 0, out)
        path = self.dir / "tornado-zz.json"
        self.assertTrue(path.exists())
        spec = json.loads(path.read_text())
        self.assertEqual(spec["scope"]["states"], ["ZZ"])
        self.assertIn("sha256:", out)
        self.assertIn("readiness loop  -c tornado-zz", out)

    def test_register_warns_about_zone_coded_hazards(self):
        _code, out = self.run_cli("register", "heat-zz", "--hazard", "heat", "--state", "zz")
        self.assertIn("zone-coded", out)
        self.assertIn("--zone-policy expand", out)

    def test_register_with_expand_policy(self):
        code, out = self.run_cli(
            "register", "heat-yy", "--hazard", "heat", "--state", "yy",
            "--zone-policy", "expand",
        )
        self.assertEqual(code, 0)
        self.assertNotIn("under-count", out)
        self.assertEqual(contracts.load("heat-yy").zone_policy, "expand")

    def test_register_refuses_to_overwrite(self):
        self.run_cli("register", "tornado-zz", "--hazard", "tornado")
        code, out = self.run_cli("register", "tornado-zz", "--hazard", "tornado")
        self.assertEqual(code, 2)
        self.assertIn("already exists", out)
        code, _ = self.run_cli("register", "tornado-zz", "--hazard", "tornado", "--force")
        self.assertEqual(code, 0)

    def test_register_rejects_an_invalid_contract_cleanly(self):
        code, out = self.run_cli(
            "register", "bad", "--hazard", "tornado", "--train", "1996-2016"
        )
        self.assertEqual(code, 2)
        self.assertIn("disjoint", out)
        self.assertFalse((self.dir / "bad.json").exists())

    def test_dry_run_writes_nothing(self):
        code, out = self.run_cli("register", "x", "--hazard", "hail", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hazard"], "hail")
        self.assertFalse((self.dir / "x.json").exists())

    def test_bare_register_yields_exactly_the_default_contract(self):
        code, out = self.run_cli(
            "register", "x", "--hazard", "tornado", "--state", "ZZ", "--dry-run"
        )
        self.assertEqual(code, 0)
        want = contracts.new("x", hazard="tornado", states=["ZZ"])
        self.assertEqual(json.loads(out), want.to_spec())
        self.assertEqual(contracts.Contract.from_spec(json.loads(out)).digest(),
                         want.digest())

    def test_custom_hazard_via_event_types(self):
        code, _ = self.run_cli(
            "register", "ash-zz", "--hazard", "volcanic_ash", "--event-type", "Volcanic Ash"
        )
        self.assertEqual(code, 0)
        self.assertEqual(contracts.load("ash-zz").event_types, ("Volcanic Ash",))


class TestSelection(CliCase):
    def test_contracts_lists_the_registry(self):
        self.run_cli("register", "tornado-zz", "--hazard", "tornado")
        self.run_cli("register", "hail-yy", "--hazard", "hail", "--state", "yy")
        code, out = self.run_cli("contracts")
        self.assertEqual(code, 0)
        self.assertIn("tornado-zz", out)
        self.assertIn("hail-yy", out)

    def test_contracts_names_prints_one_name_per_line_and_nothing_else(self):
        self.run_cli("register", "tornado-zz", "--hazard", "tornado")
        self.run_cli("register", "hail-yy", "--hazard", "hail", "--state", "yy")
        code, out = self.run_cli("contracts", "--names")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["hail-yy", "tornado-zz"])
        code, out = self.run_cli("contracts", "--names", "--national")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["tornado-zz"])

    def test_contracts_names_on_an_empty_registry_prints_nothing(self):
        code, out = self.run_cli("contracts", "--names")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_fleet_status_on_an_empty_registry_says_so_and_exits_zero(self):
        code, out = self.run_cli("fleet", "--status")
        self.assertEqual(code, 0)
        self.assertIn("fleet status  (0 contract(s))", out)
        self.assertIn("no contracts registered", out)

    def test_fleet_status_is_ledger_only(self):
        self.run_cli("register", "tornado-zz", "--hazard", "tornado")
        with mock.patch.object(data_mod, "build", side_effect=AssertionError("built")), \
                mock.patch.dict(os.environ,
                                {data_mod.EXPERIMENTS_DIR_ENV: str(self.dir / "exp")}):
            code, out = self.run_cli("fleet", "--status")
        self.assertEqual(code, 0)
        self.assertIn("tornado-zz", out)
        self.assertIn("NOT met: test card: none", out)

    def test_fleet_refuses_an_unknown_contract_name(self):
        self.run_cli("register", "tornado-zz", "--hazard", "tornado")
        code, out = self.run_cli("fleet", "--contracts", "tornado-zz,nope", "--status")
        self.assertEqual(code, 2)
        self.assertIn("no registered contract named ['nope']", out)

    def test_fleet_national_and_contracts_are_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args(
                    ["fleet", "--national", "--contracts", "a", "--status"]
                )

    def test_contract_shows_the_selected_one(self):
        self.run_cli("register", "tornado-zz", "--hazard", "tornado")
        code, out = self.run_cli("contract", "-c", "tornado-zz", "--json")
        self.assertEqual(code, 0)
        self.assertIn("tornado", out)
        self.assertIn('"name": "tornado-zz"', out)

    def test_sole_contract_is_the_default(self):
        self.run_cli("register", "tornado-zz", "--hazard", "tornado")
        code, out = self.run_cli("contract")
        self.assertEqual(code, 0)
        self.assertIn("tornado-zz", out)

    def test_ambiguity_is_refused_with_the_choices(self):
        self.run_cli("register", "tornado-zz", "--hazard", "tornado")
        self.run_cli("register", "hail-yy", "--hazard", "hail")
        code, out = self.run_cli("contract")
        self.assertEqual(code, 2)
        self.assertIn("hail-yy", out)
        self.assertIn("tornado-zz", out)

    def test_env_var_selects(self):
        self.run_cli("register", "tornado-zz", "--hazard", "tornado")
        self.run_cli("register", "hail-yy", "--hazard", "hail")
        with mock.patch.dict(os.environ, {contracts.CONTRACT_ENV: "hail-yy"}):
            code, out = self.run_cli("contract")
        self.assertEqual(code, 0)
        self.assertIn("hail-yy", out)

    def test_empty_registry_explains_itself(self):
        code, out = self.run_cli("contract")
        self.assertEqual(code, 2)
        self.assertIn("readiness register", out)

    def test_ledger_of_a_fresh_contract_is_empty_and_valid(self):
        self.run_cli("register", "tornado-zz", "--hazard", "tornado")
        with mock.patch.object(cli.data_mod, "EXPERIMENTS_DIR", self.dir / "experiments"):
            code, out = self.run_cli("ledger", "-c", "tornado-zz")
        self.assertEqual(code, 0)
        self.assertIn("ledger is empty", out)

    def test_hazards_and_models(self):
        code, out = self.run_cli("hazards")
        self.assertEqual(code, 0)
        self.assertIn("tropical_cyclone", out)
        code, out = self.run_cli("models")
        self.assertEqual(code, 0)
        self.assertIn("climatology-pooled", out)


class DataCase(CliCase):
    """A registered contract plus a synthetic dataset behind `data.build`."""

    def setUp(self):
        super().setUp()
        self.run_cli("register", "flood-zz", "--hazard", "inland_flood", "--state", "zz")
        self.contract = contracts.load("flood-zz")
        self.dataset = synthetic_dataset(self.contract, self.dir)
        self.experiments = self.dir / "experiments"
        self.expected = self.dir / "expected"
        self.patches = [
            mock.patch.dict(os.environ, {data_mod.EXPERIMENTS_DIR_ENV: str(self.experiments)}),
            mock.patch.object(data_mod, "build", lambda _c, **_kw: self.dataset),
            mock.patch.object(
                data_mod, "paths",
                functools.partial(data_mod.paths, expected_dir=self.expected),
            ),
        ]
        for p in self.patches:
            p.start()
        self.where = data_mod.paths(self.contract)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        super().tearDown()


class TestVerify(DataCase):
    def test_bless_then_verify_meets_every_criterion(self):
        code, out = self.run_cli("verify", "-c", "flood-zz", "--bless", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.where.expected.exists())
        self.assertEqual(self.where.expected, self.expected / "flood-zz.json")
        self.assertIn("[ok]   blessed baseline fingerprints", out)

        code, out = self.run_cli("verify", "-c", "flood-zz", "--quiet")
        self.assertEqual(code, 0, out)
        lines = [line for line in out.splitlines() if line.startswith("[")]
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(line.startswith("[ok]   ") for line in lines), lines)
        self.assertIn(f"[ok]   contract flood-zz validates (sha256:{self.contract.digest()})", out)
        self.assertIn("[ok]   climatology baselines reproduce bit-for-bit", out)
        self.assertIn("[ok]   leakage canary rejected leaky-oracle (tripped: ", out)
        self.assertIn("[ok]   ledger chain intact: 0 card(s)", out)
        self.assertTrue(out.rstrip().endswith("Phase 0 exit criteria met for flood-zz."))

    def test_nothing_blessed_is_a_failure_with_the_remedy(self):
        code, out = self.run_cli("verify", "-c", "flood-zz", "--quiet")
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] reproducibility: nothing to compare against", out)
        self.assertIn("readiness verify -c flood-zz --bless", out)
        self.assertIn("Phase 0 NOT met for flood-zz", out)
        self.assertNotIn("Phase 0 exit criteria met", out)

    def test_a_disagreeing_blessed_file_fails_with_the_diff(self):
        self.run_cli("verify", "-c", "flood-zz", "--bless", "--quiet")
        blessed = json.loads(self.where.expected.read_text())
        blessed["climatology-seasonal"]["auc"] = 0.999
        self.where.expected.write_text(json.dumps(blessed))
        code, out = self.run_cli("verify", "-c", "flood-zz", "--quiet")
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] reproducibility: 1 field group(s) differ", out)
        self.assertIn("         climatology-seasonal", out)
        self.assertIn("           expected ", out)
        self.assertIn("           observed ", out)
        self.assertIn("[ok]   leakage canary rejected leaky-oracle", out)
        self.assertIn("Phase 0 NOT met for flood-zz — 1 failure(s):", out)

    def test_verify_phase_defaults_to_zero(self):
        args = cli.build_parser().parse_args(["verify", "-c", "x"])
        self.assertEqual(args.phase, 0)
        self.assertFalse(args.replay)
        _code, out = self.run_cli("verify", "-c", "flood-zz", "--quiet")
        self.assertIn("Phase 0 exit criteria  (flood-zz)", out)
        self.assertNotIn("Phase 1", out)

    def test_phase_one_is_ledger_only_and_fails_on_an_empty_ledger(self):
        with mock.patch.object(data_mod, "build", side_effect=AssertionError("built")):
            code, out = self.run_cli("verify", "-c", "flood-zz", "--phase", "1")
        self.assertEqual(code, 1)
        self.assertIn("Phase 1 exit criteria  (flood-zz)", out)
        self.assertIn("[ok]   ledger chain intact: 0 card(s)", out)
        self.assertIn("[FAIL] test card: none under contract", out)
        self.assertIn("Phase 1 NOT met for flood-zz — 1 failure(s):", out)


class TestScore(DataCase):
    def test_validate_split_scores_and_judges(self):
        code, out = self.run_cli("score", "climatology-seasonal", "-c", "flood-zz", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertIn("model            climatology-seasonal@", out)
        self.assertIn("split            validate", out)
        self.assertIn("contract flood-zz 1.0.0", out)
        self.assertIn("canary", out.lower())
        self.assertFalse(self.where.touch_budget.exists())

    def test_score_on_test_refuses_and_points_to_promote(self):
        code, out = self.run_cli("score", "climatology-pooled", "-c", "flood-zz",
                                 "--split", "test", "--quiet")
        self.assertEqual(code, 2)
        self.assertIn("refusing to score against the test split", out)
        self.assertIn(
            "readiness promote climatology-pooled -c flood-zz --spend-test-touch", out
        )
        self.assertNotIn("split            test", out)
        self.assertFalse(self.where.touch_budget.exists())
        self.assertFalse(self.where.ledger.exists())

    def test_spend_test_touch_is_withdrawn_from_score_and_loop(self):
        withdrawn = (
            ("score", "climatology-pooled", "--split", "test", "--spend-test-touch"),
            ("loop", "--split", "test", "--spend-test-touch"),
            ("loop", "--split", "test"),
        )
        for argv in withdrawn:
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        cli.build_parser().parse_args(list(argv))

    def test_params_are_typed_by_the_registry(self):
        code, out = self.run_cli("score", "climatology-seasonal", "-c", "flood-zz",
                                 "--param", "shrinkage=2.5", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertIn('arguments        {"shrinkage": 2.5}', out)

    def test_unknown_param_is_refused_naming_the_known_ones(self):
        code, out = self.run_cli("score", "climatology-seasonal", "-c", "flood-zz",
                                 "--param", "kappa=2", "--quiet")
        self.assertEqual(code, 2)
        self.assertIn("no parameter 'kappa'", out)
        self.assertIn("known: shrinkage", out)
        code, out = self.run_cli("score", "climatology-seasonal", "-c", "flood-zz",
                                 "--param", "shrinkage=lots", "--quiet")
        self.assertEqual(code, 2)
        self.assertIn("'lots' is not a float", out)
        code, out = self.run_cli("score", "no-such-model", "-c", "flood-zz", "--quiet")
        self.assertEqual(code, 2)
        self.assertIn("unknown model 'no-such-model'", out)

    def test_parse_params_handles_every_declared_type(self):
        kwargs = cli.parse_params("gbm", [
            "feature_sets=era5-antecedent,terrain", "history=false", "rounds=12",
            "lr=0.5",
        ])
        self.assertEqual(kwargs, {"feature_sets": ["era5-antecedent", "terrain"],
                                  "history": False, "rounds": 12, "lr": 0.5})
        self.assertEqual(cli.parse_params("gbm", ["feature_sets="]), {"feature_sets": []})
        with self.assertRaises(cli.UsageError):
            cli.parse_params("gbm", ["history=maybe"])
        with self.assertRaises(cli.UsageError):
            cli.parse_params("gbm", ["rounds"])

    def test_a_feature_model_without_sources_is_refused_cleanly(self):
        code, out = self.run_cli("score", "logistic", "-c", "flood-zz", "--quiet")
        self.assertEqual(code, 2)
        self.assertIn("no feature sources were loaded", out)

    def test_unknown_feature_connector_is_refused(self):
        code, out = self.run_cli("score", "logistic", "-c", "flood-zz",
                                 "--features", "era5,gut-feeling", "--quiet")
        self.assertEqual(code, 2)
        self.assertIn("unknown feature connector(s) ['gut-feeling']", out)

    def test_a_rejected_model_exits_one(self):
        code, out = self.run_cli("score", "leaky-oracle", "-c", "flood-zz", "--quiet")
        self.assertEqual(code, 1)
        self.assertIn("REJECTED", out)


class TestCanaryPanelAndLoop(DataCase):
    def test_canary_passes_when_the_harness_rejects(self):
        code, out = self.run_cli("canary", "-c", "flood-zz", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertIn("leakage canary  (target: leaky-oracle, split=validate", out)
        self.assertIn("contract, if it were honoured:", out)
        self.assertTrue(out.rstrip().endswith("PASS: the harness rejected a leaked model."))

    def test_panel_reports_regions_and_coverage(self):
        code, out = self.run_cli("panel", "-c", "flood-zz", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertIn("region-quarter panel  (flood-zz)", out)
        self.assertIn("regions: 10  (", out)
        self.assertIn("split coverage", out)
        self.assertIn("event coverage", out)

    def test_loop_writes_the_ledger_and_reports_it(self):
        code, out = self.run_cli("loop", "-c", "flood-zz")
        self.assertEqual(code, 0, out)
        self.assertIn("gather context", out)
        self.assertIn("take action", out)
        self.assertIn("ledger: ", out)
        self.assertTrue(self.where.ledger.exists())

    def test_fleet_runs_the_named_contract_and_prints_the_status_table(self):
        code, out = self.run_cli("fleet", "--contracts", "flood-zz", "--queue", "baseline",
                                 "--quiet")
        self.assertEqual(code, 0, out)
        self.assertIn("fleet  (1 contract(s), queue=baseline)", out)
        self.assertIn("ran 4 experiment(s) against flood-zz", out)
        self.assertIn("fleet status  (1 contract(s))", out)
        self.assertIn("flood-zz", out)
        self.assertTrue(self.where.ledger.exists())
        self.assertFalse(self.where.touch_budget.exists())

    def test_fleet_reports_a_contract_it_could_not_build_and_exits_one(self):
        with mock.patch.object(data_mod, "build", side_effect=RuntimeError("no data")):
            code, out = self.run_cli("fleet", "--contracts", "flood-zz", "--quiet")
        self.assertEqual(code, 1)
        self.assertIn("flood-zz: not run (RuntimeError: no data)", out)
        self.assertIn("fleet status", out)
        self.assertFalse(self.where.ledger.exists())

    def test_loop_quiet_silences_progress_but_prints_the_result(self):
        code, out = self.run_cli("loop", "-c", "flood-zz", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertNotIn("gather context", out)
        self.assertNotIn("take action", out)
        self.assertIn("leaky-oracle", out)
        self.assertIn("ledger: ", out)
        self.assertTrue(self.where.ledger.exists())


class FeatureCase(DataCase):
    """The synthetic dataset carries feature sources with planted signal."""

    def setUp(self):
        super().setUp()
        self.dataset = signal_dataset(self.contract, self.dir)
        # Fewer steps than the registry defaults; the wiring is what is under test.
        phase1 = orchestrator.BASELINE_QUEUE + tuple(quick(orchestrator.PHASE1_QUEUE))
        fast = {"phase1": phase1}
        self.queue_patch = mock.patch.dict(orchestrator.QUEUES, fast)
        self.queue_patch.start()

    def tearDown(self):
        self.queue_patch.stop()
        super().tearDown()

    def promote_pass(self, *extra):
        """`promote` for the candidate the quick phase1 queue passes first."""
        return self.run_cli(
            "promote", "logistic+iso", "-c", "flood-zz", "--quiet",
            "--param", "feature_sets=era5-antecedent,terrain", "--param", "iters=60",
            *extra,
        )


class TestFeaturesCommand(FeatureCase):
    def test_prints_admission_verdicts_columns_and_the_audit(self):
        code, out = self.run_cli("features", "-c", "flood-zz",
                                 "--features", "era5,terrain", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertIn("feature sources  (flood-zz: era5, terrain)", out)
        self.assertIn("[admitted] era5        series", out)
        self.assertIn("[admitted] gazetteer   static, timeless geometry", out)
        self.assertIn("feature version sha256:synthetic-features", out)
        self.assertIn("  era5-antecedent", out)
        self.assertIn("  terrain", out)
        self.assertNotIn("  nri\n", out)
        self.assertIn(
            "precip_3m          era5.precip_mm  trailing_sum over 3 month(s), lag 1", out
        )
        self.assertIn("audit over", out)
        self.assertIn("feature audit -> clean", out)
        self.assertFalse(self.where.ledger.exists())

    def test_a_refused_layer_is_a_finding_not_an_error(self):
        self.dataset.sources["nri"] = FakeStatic("nri", derived_through=2023)
        code, out = self.run_cli("features", "-c", "flood-zz",
                                 "--features", "era5,terrain,nri", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertIn("[REFUSED] nri", out)
        self.assertIn("encodes data through 2023", out)
        self.assertIn("  nri\n", out)
        self.assertIn("feature audit -> REFUSED", out)
        self.assertIn("[REFUSED] admission", out)

    def test_build_receives_the_requested_connectors(self):
        seen = {}

        def capture(_c, **kw):
            seen.update(kw)
            return self.dataset

        with mock.patch.object(data_mod, "build", capture):
            code, _out = self.run_cli("features", "-c", "flood-zz",
                                      "--features", "terrain", "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(seen["features"], ["terrain"])
        with mock.patch.object(data_mod, "build", capture):
            self.run_cli("features", "-c", "flood-zz", "--quiet")
        # Nothing is pinned in this checkout's snapshot tree for ZZ.
        self.assertEqual(seen["features"], [])


class TestPhase1Loop(FeatureCase):
    def test_queue_phase1_with_promote_writes_the_test_card(self):
        code, out = self.run_cli("loop", "-c", "flood-zz", "--queue", "phase1",
                                 "--promote", "--features", "era5,terrain")
        self.assertEqual(code, 0, out)
        self.assertIn("queue=phase1", out)
        self.assertIn("take action  [logistic[history-only]]", out)
        self.assertIn("promote      [logistic+iso[era5-antecedent+terrain]]", out)
        self.assertIn("test touch 1/1 spent", out)
        self.assertIn("promoted to test  logistic+iso@1.0.0 -> PASS", out)
        self.assertNotIn("skip ", out)
        self.assertEqual(json.loads(self.where.touch_budget.read_text()),
                         {"logistic+iso@1.0.0": 1})
        _code, ledger = self.run_cli("ledger", "-c", "flood-zz")
        self.assertIn("test", ledger)
        self.assertIn("chain intact", ledger)

    def test_candidates_are_skipped_when_the_sources_are_missing(self):
        self.dataset.sources.pop("elevation")
        code, out = self.run_cli("loop", "-c", "flood-zz", "--queue", "phase1", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertIn("skipped (sources)", out)
        self.assertIn("gbm+iso[era5-antecedent+terrain]", out)
        self.assertFalse(self.where.touch_budget.exists())

    def test_a_refused_promotion_prints_the_loop_and_exits_two(self):
        # The second run's queue is fine; it is the promotion that cannot
        # happen, because the first run already spent the ledger's one touch.
        code, out = self.run_cli("loop", "-c", "flood-zz", "--queue", "phase1",
                                 "--promote", "--quiet")
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("loop", "-c", "flood-zz", "--queue", "phase1",
                                 "--promote", "--quiet")
        self.assertEqual(code, 2, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("ran ", out)  # the loop's own result, before the reason
        self.assertIn("ledger: ", out)
        self.assertIn("already holds a test card", out)
        self.assertIn("A new contract name, not a second touch", out)
        self.assertEqual(json.loads(self.where.touch_budget.read_text()),
                         {"logistic+iso@1.0.0": 1})

    def test_the_fleet_reports_a_refused_promotion_and_still_exits_zero(self):
        # `loop --promote` exits 2 on a refusal because one contract is all it
        # was asked about. The fleet was asked about a set, and a spent touch
        # in one of them is not a fleet that failed to run: the loop's summary
        # and the refusal are printed, and the exit status stays 0. `verify`
        # is the gate on the phase, not this.
        code, out = self.run_cli("fleet", "--contracts", "flood-zz", "--queue", "phase1",
                                 "--promote", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertIn("promoted to test  logistic+iso@1.0.0 -> PASS", out)
        code, out = self.run_cli("fleet", "--contracts", "flood-zz", "--queue", "phase1",
                                 "--promote", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertNotIn("not run", out)
        self.assertIn("ran ", out)  # the loop that did run, first
        self.assertIn("promotion refused: refusing to promote", out)
        self.assertIn("already holds a test card", out)
        self.assertEqual(json.loads(self.where.touch_budget.read_text()),
                         {"logistic+iso@1.0.0": 1})

    def test_baseline_queue_is_the_default_and_promotes_nothing(self):
        code, out = self.run_cli("loop", "-c", "flood-zz", "--promote", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertNotIn("promoted to test", out)
        self.assertFalse(self.where.touch_budget.exists())


class TestPromoteCommand(FeatureCase):
    def test_refuses_without_spend_test_touch(self):
        code, out = self.promote_pass()
        self.assertEqual(code, 2)
        self.assertIn("refusing to promote logistic+iso without --spend-test-touch", out)
        self.assertFalse(self.where.touch_budget.exists())
        self.assertFalse(self.where.ledger.exists())

    def test_refuses_without_a_validate_pass(self):
        code, out = self.promote_pass("--spend-test-touch")
        self.assertEqual(code, 2)
        self.assertIn("no validate card", out)
        self.assertFalse(self.where.touch_budget.exists())

    def test_promotes_once_then_refuses_a_second_test_card(self):
        self.run_cli("loop", "-c", "flood-zz", "--queue", "phase1", "--quiet")
        code, out = self.promote_pass("--spend-test-touch")
        self.assertEqual(code, 0, out)
        self.assertIn("promote  logistic+iso  to test  (flood-zz)", out)
        self.assertIn("split            test", out)
        self.assertIn("readiness backtest -c flood-zz", out)
        self.assertEqual(json.loads(self.where.touch_budget.read_text()),
                         {"logistic+iso@1.0.0": 1})
        code, out = self.promote_pass("--spend-test-touch")
        self.assertEqual(code, 2)
        self.assertIn("already holds a test card", out)
        code, out = self.run_cli("promote", "gbm", "-c", "flood-zz", "--quiet",
                                 "--spend-test-touch", "--param", "rounds=40",
                                 "--param", "feature_sets=era5-antecedent,terrain")
        self.assertEqual(code, 2)

    def test_refuses_named_arguments_no_validate_card_carries(self):
        self.run_cli("loop", "-c", "flood-zz", "--queue", "phase1", "--quiet")
        code, out = self.run_cli("promote", "logistic+iso", "-c", "flood-zz", "--quiet",
                                 "--spend-test-touch", "--param", "iters=61",
                                 "--param", "feature_sets=era5-antecedent,terrain")
        self.assertEqual(code, 2)
        self.assertIn("no validate card for exactly this model, version and arguments",
                      out)
        self.assertFalse(self.where.touch_budget.exists())

    def test_without_param_the_validate_cards_arguments_are_adopted(self):
        # `make promote MODEL=logistic+iso` and the documented command name no
        # arguments at all; the registry's defaults are not what was validated.
        self.run_cli("loop", "-c", "flood-zz", "--queue", "phase1", "--quiet")
        code, out = self.run_cli("promote", "logistic+iso", "-c", "flood-zz",
                                 "--spend-test-touch")
        self.assertEqual(code, 0, out)
        self.assertIn("adopted from validate card", out)
        self.assertIn('"iters": 60', out)
        self.assertIn("arguments        {", out)
        self.assertEqual(json.loads(self.where.touch_budget.read_text()),
                         {"logistic+iso@1.0.0": 1})
        card = json.loads(self.where.ledger.read_text().splitlines()[-1])
        self.assertEqual(card["split"], "test")
        self.assertEqual(card["data_snapshot"]["model_kwargs"]["iters"], 60)


class TestBacktestAndPhase1Verify(FeatureCase):
    def test_backtest_writes_the_report_and_its_twin(self):
        code, out = self.run_cli("backtest", "-c", "flood-zz")
        self.assertEqual(code, 0, out)
        page = self.where.directory / "backtest.html"
        self.assertTrue(page.exists())
        self.assertTrue(page.with_suffix(".json").exists())
        self.assertIn("backtest.html", out)
        self.assertIn("backtest.json", out)
        target = self.dir / "out.html"
        code, _out = self.run_cli("backtest", "-c", "flood-zz", "-o", str(target))
        self.assertEqual(code, 0)
        self.assertTrue(target.exists())

    def test_the_phase1_flow_meets_the_exit_criteria(self):
        self.run_cli("loop", "-c", "flood-zz", "--queue", "phase1", "--promote",
                     "--quiet")
        code, out = self.run_cli("verify", "-c", "flood-zz", "--phase", "1")
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] published: no backtest report", out)
        self.run_cli("backtest", "-c", "flood-zz")
        code, out = self.run_cli("verify", "-c", "flood-zz", "--phase", "1")
        self.assertEqual(code, 0, out)
        lines = [line for line in out.splitlines() if line.startswith("[")]
        self.assertEqual(len(lines), 8)
        self.assertTrue(all(line.startswith("[ok]   ") for line in lines), lines)
        self.assertTrue(out.rstrip().endswith("Phase 1 exit criteria met for flood-zz."))
        touches = self.where.touch_budget.read_text()
        code, out = self.run_cli("verify", "-c", "flood-zz", "--phase", "1", "--replay",
                                 "--quiet")
        self.assertEqual(code, 0, out)
        self.assertIn("[ok]   replay brier_score: agrees", out)
        self.assertIn("[ok]   replay feature_digest: card ", out)
        for i in range(self.contract.n_reliability_bins):
            self.assertIn(f"[ok]   replay reliability_bins[{i}]: agrees", out)
        self.assertEqual(self.where.touch_budget.read_text(), touches)

    def test_replay_never_prints_a_score_from_the_test_split(self):
        # The refit rescores the one-shot holdout; the command may say whether
        # it agrees with the card and nothing more. Where they agree the card's
        # own rendering *is* the refit's, so neither may be printed.
        self.run_cli("loop", "-c", "flood-zz", "--queue", "phase1", "--promote",
                     "--quiet")
        self.run_cli("backtest", "-c", "flood-zz")
        result = verify.phase1(
            self.contract, ledger_path=self.where.ledger,
            touch_path=self.where.touch_budget,
            backtest_path=self.where.directory / "backtest.html",
        )
        rows = verify.replay(self.contract, self.dataset, result.card)
        code, out = self.run_cli("verify", "-c", "flood-zz", "--phase", "1", "--replay",
                                 "--quiet")
        self.assertEqual(code, 0, out)
        replay_lines = "\n".join(x for x in out.splitlines() if "replay " in x)
        scored = {f: (e, o) for f, e, o, _ok in rows
                  if f not in verify.REPLAY_SHOWN_FIELDS and not f.startswith("reli")}
        self.assertIn("brier_score", scored)
        for field, (expected, observed) in scored.items():
            with self.subTest(field=field):
                self.assertNotIn(verify.sig(observed), replay_lines)
                self.assertNotIn(repr(observed), replay_lines)
                self.assertNotIn(verify.sig(expected), replay_lines)
                self.assertIn(f"replay {field}: agrees", out)

    def test_replay_without_a_test_card_is_a_failure(self):
        code, out = self.run_cli("verify", "-c", "flood-zz", "--phase", "1", "--replay",
                                 "--quiet")
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] replay: no test card to replay", out)


class TestLedgerCommand(DataCase):
    def setUp(self):
        super().setUp()
        self.run_cli("loop", "-c", "flood-zz", "--quiet")

    def test_summary_by_default(self):
        code, out = self.run_cli("ledger", "-c", "flood-zz")
        self.assertEqual(code, 0)
        self.assertIn("exp-0001", out)
        self.assertIn("chain intact", out)
        self.assertNotIn('"card_hash"', out)

    def test_id_implies_show(self):
        code, out = self.run_cli("ledger", "-c", "flood-zz", "--id", "exp-0002")
        self.assertEqual(code, 0)
        card = json.loads(out[out.index("{"):out.rindex("}") + 1])  # exactly one card
        self.assertEqual(card["experiment_id"], "exp-0002")
        self.assertIn("card_hash", card)
        self.assertNotIn("exp-0001", out)

    def test_unknown_id_is_a_failure_with_a_message(self):
        code, out = self.run_cli("ledger", "-c", "flood-zz", "--id", "exp-9999")
        self.assertEqual(code, 1)
        self.assertIn("no experiment 'exp-9999'", out)

    def test_show_prints_every_card(self):
        code, out = self.run_cli("ledger", "-c", "flood-zz", "--show")
        self.assertEqual(code, 0)
        self.assertEqual(out.count('"card_hash"'), 4)


class TestDashboardCommand(DataCase):
    def setUp(self):
        super().setUp()
        self.run_cli("loop", "-c", "flood-zz", "--quiet")

    def test_all_refuses_a_contract_or_an_output_path(self):
        for extra in (("-c", "flood-zz"), ("-o", str(self.dir / "page.html"))):
            with self.subTest(extra=extra):
                code, out = self.run_cli("dashboard", "--all", *extra)
                self.assertEqual(code, 2)
                self.assertEqual(len(out.strip().splitlines()), 1)
                self.assertIn("--all cannot be combined", out)
        self.assertFalse((self.dir / "page.html").exists())
        self.assertFalse((self.experiments / "index.html").exists())

    def test_all_alone_writes_the_index(self):
        code, out = self.run_cli("dashboard", "--all")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.experiments / "index.html").exists())

    def test_output_path_is_honoured_for_one_contract(self):
        target = self.dir / "page.html"
        code, _out = self.run_cli("dashboard", "-c", "flood-zz", "-o", str(target))
        self.assertEqual(code, 0)
        self.assertTrue(target.exists())


# ---------------------------------------------------------------------------
# Phase 2: exposure, issue, brief, verify --phase 2
# ---------------------------------------------------------------------------


class ExposureCase(CliCase):
    """Pinned USA Structures extracts in a temporary snapshot tree."""

    def setUp(self):
        super().setUp()
        self.snapshots = self.dir / "snapshots"
        self.patches = [
            mock.patch.object(data_mod, "SNAPSHOT_DIR", self.snapshots),
            mock.patch.object(data_mod, "MANIFEST_PATH", self.snapshots / "manifest.json"),
        ]
        for p in self.patches:
            p.start()
        self.manifest = Manifest(path=self.snapshots / "manifest.json")
        for state, counties in SPOT_COUNTIES.items():
            pin_counts(self.snapshots, self.manifest, state, counties)
        self.manifest.save()
        self.counts = self.dir / "assessor_counts.csv"

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        super().tearDown()

    def every_county(self) -> dict:
        return {f: n for counties in SPOT_COUNTIES.values() for f, n in counties.items()}


class TestExposureCommands(ExposureCase):
    def test_show_prints_every_county_of_a_state(self):
        code, out = self.run_cli("exposure", "show", "--state", "99")
        self.assertEqual(code, 0, out)
        for fips in SPOT_COUNTIES["99"]:
            self.assertIn(fips, out)
        self.assertNotIn("98001", out)
        self.assertIn("1,200", out)          # the county total, grouped
        self.assertIn("vintage 2023", out)   # the layer year the record declares

    def test_show_one_county_and_a_county_that_is_not_there(self):
        code, out = self.run_cli("exposure", "show", "--county", "99003")
        self.assertEqual(code, 0, out)
        self.assertIn("99003", out)
        self.assertNotIn("99001 ", out)
        code, out = self.run_cli("exposure", "show", "--county", "99999")
        self.assertEqual(code, 1)
        self.assertIn("no row for 99999", out)

    def test_show_every_pinned_state_by_default(self):
        code, out = self.run_cli("exposure", "show")
        self.assertEqual(code, 0, out)
        self.assertIn("12 county/counties", out)
        self.assertIn("97, 98, 99", out)

    def test_show_says_so_when_nothing_is_pinned(self):
        (self.snapshots / "manifest.json").write_text('{"records": {}}')
        code, out = self.run_cli("exposure", "show")
        self.assertEqual(code, 1)
        self.assertIn("no USA Structures counts are pinned", out)

    def test_spot_check_prints_every_ratio_and_passes_on_twelve(self):
        counts_csv(self.counts, self.every_county())
        code, out = self.run_cli("exposure", "spot-check", "--counts", str(self.counts))
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("within"), 13)  # twelve rows, and the band's name
        self.assertIn("spot-check -> PASS: 12/12 counties", out)

    def test_spot_check_exits_1_below_ten_counties_or_three_states(self):
        counts_csv(self.counts, dict(list(self.every_county().items())[:8]))
        code, out = self.run_cli("exposure", "spot-check", "--counts", str(self.counts))
        self.assertEqual(code, 1)
        self.assertIn("spot-check -> NOT YET", out)
        self.assertIn(">= 10 counties from >= 3 states", out)

    def test_spot_check_prints_out_of_band_rows_without_counting_them(self):
        counts_csv(self.counts, self.every_county(), ratio=3.0)
        code, out = self.run_cli("exposure", "spot-check", "--counts", str(self.counts))
        self.assertEqual(code, 1)
        self.assertIn("OUTSIDE", out)
        self.assertIn("0/12 counties within the band", out)

    def test_spot_check_refuses_an_unpinned_state_by_name(self):
        counts_csv(self.counts, {"96001": 1000})
        code, out = self.run_cli("exposure", "spot-check", "--counts", str(self.counts))
        self.assertEqual(code, 2)
        self.assertIn("fema/usa_structures/96 is not pinned", out)
        self.assertIn("readiness exposure snapshot --states 96", out)

    def test_spot_check_refuses_a_malformed_counts_file_by_row(self):
        self.counts.write_text(
            "fips,assessor_count,count_definition,source_url,retrieved_on,notes\n"
            "99001,1200,acres,https://example.invalid/x,2026-09-01,\n",
            encoding="utf-8",
        )
        code, out = self.run_cli("exposure", "spot-check", "--counts", str(self.counts))
        self.assertEqual(code, 2)
        self.assertIn("count_definition must be one of", out)

    def test_spot_check_refuses_a_missing_counts_file(self):
        code, out = self.run_cli("exposure", "spot-check", "--counts", str(self.dir / "x"))
        self.assertEqual(code, 2)
        self.assertIn("no assessor counts file at", out)

    def test_snapshot_pulls_the_named_states_and_pins_them(self):
        pulled = {}

        def fake_snapshot(states, snapshot_dir, manifest, **kw):
            pulled["states"] = list(states)
            pulled["layer_url"] = kw.get("layer_url")
            for state in states:
                pin_counts(snapshot_dir, manifest, state, SPOT_COUNTIES[state])
            return []

        with mock.patch.object(usa_structures, "snapshot", fake_snapshot):
            code, out = self.run_cli("exposure", "snapshot", "--states", "97,98")
        self.assertEqual(code, 0, out)
        self.assertEqual(pulled["states"], ["97", "98"])
        self.assertEqual(pulled["layer_url"], usa_structures.LAYER_URL)
        self.assertIn("8 counties", out)
        self.assertIn("fema/usa_structures/<st>", out)

    def test_snapshot_takes_a_layer_url_override(self):
        with mock.patch.object(usa_structures, "snapshot") as fake:
            fake.side_effect = lambda states, d, m, **kw: pin_counts(
                d, m, "99", SPOT_COUNTIES["99"]
            )
            code, _out = self.run_cli(
                "exposure", "snapshot", "--states", "99",
                "--layer-url", "https://example.invalid/layer/0",
            )
        self.assertEqual(code, 0)
        self.assertEqual(fake.call_args.kwargs["layer_url"],
                         "https://example.invalid/layer/0")

    def test_snapshot_needs_states_or_all_states(self):
        code, out = self.run_cli("exposure", "snapshot")
        self.assertEqual(code, 2)
        self.assertIn("--states A,B or ask for --all-states", out)

    def test_all_states_needs_the_pinned_county_file(self):
        code, out = self.run_cli("exposure", "snapshot", "--all-states")
        self.assertEqual(code, 2)
        self.assertIn("--all-states needs the pinned Census county file", out)


class Phase2Case(FeatureCase):
    """A promoted test card, a temporary issued/ and briefs/, pinned exposure."""

    def setUp(self):
        super().setUp()
        self.dataset = with_regions(self.dataset)
        self.snapshots = self.dir / "snapshots"
        self.issued_dir = self.dir / "issued"
        self.briefs_dir = self.dir / "briefs"
        self.phase2_patches = [
            mock.patch.dict(os.environ, {
                issue_mod.ISSUED_DIR_ENV: str(self.issued_dir),
                brief_mod.BRIEFS_DIR_ENV: str(self.briefs_dir),
            }),
            mock.patch.object(data_mod, "SNAPSHOT_DIR", self.snapshots),
            mock.patch.object(data_mod, "MANIFEST_PATH", self.snapshots / "manifest.json"),
        ]
        for p in self.phase2_patches:
            p.start()
        self.manifest = Manifest(path=self.snapshots / "manifest.json")
        pin_counts(self.snapshots, self.manifest, "99",
                   {fips: 1000 for fips in self.dataset.panel.regions})
        self.manifest.save()

    def tearDown(self):
        for p in reversed(self.phase2_patches):
            p.stop()
        super().tearDown()

    def promote(self):
        """The one passing candidate, straight through the orchestrator."""
        result = orchestrator.run_local(
            self.contract, experiments_dir=self.experiments, dataset=self.dataset,
            queue=[CANDIDATE], include_canary=False, promote=True,
            progress=lambda _m: None,
        )
        self.assertIsNotNone(result.promoted)
        return result.promoted

    def issue(self, *extra, period="2026-Q1"):
        return self.run_cli(
            "issue", CANDIDATE.model, "-c", "flood-zz", "--period", period, "--quiet",
            *(f for pair in sorted(CANDIDATE.kwargs.items())
              for f in ("--param", f"{pair[0]}="
                        + (",".join(map(str, pair[1])) if isinstance(pair[1], list)
                           else str(pair[1])))),
            *extra,
        )


class TestIssueCommand(Phase2Case):
    def test_issue_writes_the_file_and_names_the_card(self):
        card = self.promote()
        code, out = self.issue()
        self.assertEqual(code, 0, out)
        path = self.issued_dir / "flood-zz" / "2026-Q1.json"
        self.assertTrue(path.exists())
        issued = issue_mod.Issued.read(path)
        self.assertEqual(issued.validated_by, card.experiment_id)
        self.assertEqual(sorted(issued.probabilities),
                         sorted(r.fips for r in self.dataset.regions))
        self.assertIn("issue  logistic+iso  for 2026-Q1  (flood-zz)", out)
        self.assertIn("readiness brief --county 99001 --period 2026-Q1", out)

    def test_issue_refuses_without_a_test_card(self):
        code, out = self.issue()
        self.assertEqual(code, 2)
        self.assertIn("holds no test card", out)
        self.assertFalse(self.issued_dir.exists())

    def test_issue_refuses_a_period_the_data_does_not_reach(self):
        self.promote()
        code, out = self.issue(period="2026-Q2")
        self.assertEqual(code, 2)
        self.assertIn("period cannot be issued yet", out)
        self.assertFalse(self.issued_dir.exists())

    def test_issue_refuses_a_label_of_the_wrong_shape(self):
        code, out = self.issue(period="2026-M04")
        self.assertEqual(code, 2)
        self.assertIn("is not a quarterly label", out)

    def test_issue_refuses_to_overwrite_and_reissue_replaces(self):
        self.promote()
        self.assertEqual(self.issue()[0], 0)
        path = self.issued_dir / "flood-zz" / "2026-Q1.json"
        first = issue_mod.Issued.read(path)
        code, out = self.issue()
        self.assertEqual(code, 2, out)
        self.assertIn("already exists", out)
        self.assertIn("--reissue", out)
        self.assertEqual(issue_mod.Issued.read(path).to_dict(), first.to_dict())
        code, out = self.run_cli(
            "issue", CANDIDATE.model, "-c", "flood-zz", "--period", "2026-Q1",
            "--reissue",
            *(f for pair in sorted(CANDIDATE.kwargs.items())
              for f in ("--param", f"{pair[0]}="
                        + (",".join(map(str, pair[1])) if isinstance(pair[1], list)
                           else str(pair[1])))),
        )
        self.assertEqual(code, 0, out)
        self.assertIn("reissued", out)
        self.assertIn(f"replacing the file issued at {first.issued_at}", out)

    def test_issue_refuses_a_year_the_contract_spans(self):
        self.promote()
        code, out = self.issue(period="2024-Q1")
        self.assertEqual(code, 2, out)
        self.assertIn("2024 is a test year", out)
        self.assertIn("The first issuable period is 2026-Q1", out)
        self.assertFalse(self.issued_dir.exists())

    def test_issue_has_no_flag_through_which_labels_could_arrive(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["issue", "logistic", "-c", "flood-zz",
                               "--period", "2026-Q1", "--labels", "labels.csv"])
        issue_parser = parser._subparsers._group_actions[0].choices["issue"]
        options = {o for action in issue_parser._actions for o in action.option_strings}
        self.assertEqual({o for o in options if "label" in o or "outcome" in o}, set())


class TestBriefCommand(Phase2Case):
    def test_issue_then_brief_round_trip(self):
        self.promote()
        self.assertEqual(self.issue()[0], 0)
        code, out = self.run_cli("brief", "--county", "99001", "--period", "2026-Q1")
        self.assertEqual(code, 0, out)
        html = self.briefs_dir / "99001" / "2026-Q1.html"
        json_path = html.with_suffix(".json")
        self.assertTrue(html.exists())
        self.assertIn("1 brief(s) written, 0 refused", out)
        doc = cite.from_json(json_path.read_text())
        self.assertEqual(doc.kind, brief_mod.KIND)
        self.assertEqual(brief_mod.sub_county_keys(cite.to_dict(doc)), [])
        text = " ".join(cite.strip_markers(s.text) for s in doc.sentences)
        self.assertIn("chance of at least one damaging inland flood event", text)
        self.assertIn("holds 1,000 structures", text)   # the pinned exposure row
        self.assertIn(cite.NOT_A_WARNING_SENTENCE, text)
        self.assertNotIn("would touch", text)
        # Re-validating the committed file against the tree finds nothing wrong.
        resolver = brief_mod.resolver(
            {"flood-zz": self.contract}, issued_dir=self.issued_dir,
            snapshot_dir=self.snapshots, experiments_dir=self.experiments,
        )
        self.assertEqual(brief_mod.check(doc, resolver), [])
        self.assertIn("USA Structures (public domain), county counts only",
                      html.read_text())

    def test_brief_for_a_whole_state_writes_one_per_county(self):
        self.promote()
        self.issue()
        code, out = self.run_cli("brief", "--state", "99", "--period", "2026-Q1",
                                 "--out", str(self.dir / "elsewhere"))
        self.assertEqual(code, 0, out)
        written = sorted((self.dir / "elsewhere").glob("*/2026-Q1.json"))
        self.assertEqual(len(written), len(self.dataset.regions))
        self.assertIn(f"{len(written)} brief(s) written, 0 refused", out)
        self.assertFalse(self.briefs_dir.exists())

    def test_brief_without_an_issued_file_says_what_to_run(self):
        code, out = self.run_cli("brief", "--county", "99001", "--period", "2026-Q1")
        self.assertEqual(code, 1)
        self.assertIn("nothing is issued for period 2026-Q1", out)
        self.assertIn("readiness issue MODEL", out)
        self.assertFalse(self.briefs_dir.exists())

    def test_brief_for_a_county_no_issued_file_covers(self):
        self.promote()
        self.issue()
        code, out = self.run_cli("brief", "--county", "99999", "--period", "2026-Q1")
        self.assertEqual(code, 1)
        self.assertIn("not written", out)
        self.assertIn("no issued file for period 2026-Q1 carries a "
                      "probability for county 99999", out)
        self.assertFalse((self.briefs_dir / "99999").exists())


class TestVerifyPhase2(Phase2Case):
    def test_verify_phase2_takes_no_contract_and_reports_every_check(self):
        code, out = self.run_cli("verify", "--phase", "2")
        self.assertEqual(code, 1)
        self.assertIn("Phase 2 exit criteria  (the registry)", out)
        for name in ("national contracts", "exposure spot-check", "issued", "brief"):
            self.assertIn(name, out)
        self.assertIn("Phase 2 NOT met — 4 failure(s):", out)

    def test_verify_phase2_refuses_a_contract_rather_than_ignoring_it(self):
        code, out = self.run_cli("verify", "-c", "flood-zz", "--phase", "2")
        self.assertEqual(code, 2)
        self.assertIn("takes no contract", out)

    def test_verify_phase2_exits_0_when_every_criterion_is_met(self):
        met = verify.Phase2Result(tuple(
            Check(name, True, f"{name}: met")
            for name in ("national contracts", "exposure spot-check", "issued", "brief")
        ))
        with mock.patch.object(verify, "phase2", lambda **_kw: met):
            code, out = self.run_cli("verify", "--phase", "2")
        self.assertEqual(code, 0, out)
        self.assertTrue(out.rstrip().endswith("Phase 2 exit criteria met."))
        self.assertEqual(len([ln for ln in out.splitlines() if ln.startswith("[ok]")]), 4)

    def test_verify_phase2_needs_no_registered_contract_to_run(self):
        # The registry here holds one state-scoped contract, so the national
        # criterion is 0/0 and the command still prints rather than crashing.
        code, out = self.run_cli("verify", "--phase", "2")
        self.assertEqual(code, 1)
        self.assertIn("0/0 registered with scope 'every region'", out)


# ---------------------------------------------------------------------------
# Phase 3: scenarios, gap-report, review, verify --phase 3
# ---------------------------------------------------------------------------


class Phase3Case(CliCase):
    """A temporary facility record, issued tree, reports tree and reviews tree."""

    def setUp(self):
        super().setUp()
        self.issued_dir = self.dir / "issued"
        self.reports_dir = self.dir / "reports"
        self.reviews_dir = self.dir / "reviews"
        fixtures_plans.make_issued().write(self.issued_dir)
        self.facility = fixtures_plans.write_facility(self.dir / "facility.json")
        self.env3 = mock.patch.dict(os.environ, {
            issue_mod.ISSUED_DIR_ENV: str(self.issued_dir),
            gap_report_mod.REPORTS_DIR_ENV: str(self.reports_dir),
            reviews_mod.REVIEWS_DIR_ENV: str(self.reviews_dir),
            data_mod.EXPERIMENTS_DIR_ENV: str(self.dir / "experiments"),
        })
        self.env3.start()

    def tearDown(self):
        self.env3.stop()
        super().tearDown()

    def gap_report(self, *extra, facility=None, period="2026-Q4"):
        return self.run_cli(
            "gap-report", "--facility", str(facility or self.facility),
            "--period", period, *extra,
        )


class TestScenariosCommand(CliCase):
    def test_list_prints_ids_titles_and_question_counts(self):
        code, out = self.run_cli("scenarios", "list")
        self.assertEqual(code, 0, out)
        self.assertIn("96h-isolation-acute-care", out)
        self.assertIn("6 question(s)", out)
        self.assertIn("96-hour isolation of an acute-care facility", out)
        self.assertIn("plans/scenarios/96h-isolation-acute-care.md", out)
        for question_id in ("q1", "q2", "q3", "q4", "q5", "q6"):
            self.assertIn(question_id, out)

    def test_check_with_no_case_study_passes_and_says_why(self):
        code, out = self.run_cli("scenarios", "check")
        self.assertEqual(code, 0, out)
        self.assertIn("none committed", out)
        self.assertIn("published investigation", out)

    def test_check_runs_a_directory_of_case_studies(self):
        studies = self.dir / "case-studies"
        fixtures_plans.write_case_study(studies / "synthetic.json")
        code, out = self.run_cli("scenarios", "check", "--case-studies", str(studies))
        self.assertEqual(code, 0, out)
        self.assertIn("synthetic-river-flood", out)
        self.assertIn("1/1 case study/studies reproduce", out)

    def test_check_exits_1_on_a_mismatch(self):
        studies = self.dir / "case-studies"
        study = fixtures_plans.synthetic_case_study()
        study["expected_findings"]["q1"] = "answered"
        fixtures_plans.write_case_study(studies / "synthetic.json", **study)
        code, out = self.run_cli("scenarios", "check", "--case-studies", str(studies))
        self.assertEqual(code, 1)
        self.assertIn("[FAIL]", out)
        self.assertIn("q1: expected 'answered', rules said 'failed'", out)

    def test_a_scenario_that_cannot_be_read_is_exit_2_not_a_traceback(self):
        from readiness.plans import scenarios as scenarios_mod

        broken = self.dir / "scenarios"
        broken.mkdir(parents=True, exist_ok=True)
        (broken / "broken.json").write_text("null", encoding="utf-8")
        with mock.patch.object(scenarios_mod, "SCENARIOS_DIR", broken):
            code, out = self.run_cli("scenarios", "list")
        self.assertEqual(code, 2, out)
        self.assertIn("expected a JSON object", out)

    def test_a_case_study_that_is_not_an_object_is_exit_1_not_a_traceback(self):
        studies = self.dir / "case-studies"
        studies.mkdir(parents=True, exist_ok=True)
        (studies / "null.json").write_text("null", encoding="utf-8")
        code, out = self.run_cli("scenarios", "check", "--case-studies", str(studies))
        self.assertEqual(code, 1, out)
        self.assertIn("expected a JSON object", out)

    def test_check_exits_1_on_a_fact_without_a_source(self):
        studies = self.dir / "case-studies"
        fixtures_plans.write_case_study(
            studies / "synthetic.json", event={"text": "something happened"}
        )
        code, out = self.run_cli("scenarios", "check", "--case-studies", str(studies))
        self.assertEqual(code, 1)
        self.assertIn("a fact without a source does not belong", out)


class TestGapReportCommand(Phase3Case):
    def blinded_path(self, slug="test-facility") -> pathlib.Path:
        label = fixtures_plans.make_facility(slug=slug).blind_label
        return self.reports_dir / "blinded" / label / "2026-Q4.blind.html"

    def test_it_writes_three_files_and_prints_every_status(self):
        code, out = self.gap_report()
        self.assertEqual(code, 0, out)
        directory = self.reports_dir / "test-facility"
        for name in ("2026-Q4.html", "2026-Q4.json"):
            self.assertTrue((directory / name).exists(), name)
        self.assertTrue(self.blinded_path().exists())
        self.assertFalse((directory / "2026-Q4.blind.html").exists())
        for question_id in ("q1", "q2", "q3", "q4", "q5", "q6"):
            self.assertIn(question_id, out)
        self.assertIn("blinded sha256", out)
        self.assertIn("A practising emergency manager reviews every finding", out)
        self.assertIn("readiness review record --report", out)

    def test_the_written_json_validates_and_names_nothing_below_a_county(self):
        self.assertEqual(self.gap_report()[0], 0)
        path = self.reports_dir / "test-facility" / "2026-Q4.json"
        doc = cite.from_json(path.read_text(encoding="utf-8"))
        self.assertEqual(doc.kind, gap_report_mod.KIND)
        self.assertEqual(gap_report_mod.forbidden_keys(cite.to_dict(doc)), [])
        text = " ".join(cite.strip_markers(s.text) for s in doc.sentences)
        self.assertIn(cite.NOT_A_WARNING_SENTENCE, text)

    def test_the_blinded_render_names_neither_the_facility_nor_a_partner(self):
        self.assertEqual(self.gap_report()[0], 0)
        path = self.blinded_path()
        page = path.read_text()
        self.assertNotIn("test-facility", page)
        self.assertNotIn("Far Ridge Hospital", page)
        self.assertNotIn(fixtures_plans.COUNTY, page)
        self.assertIn("FACILITY-", page)
        self.assertIn("PARTNER-1", page)
        self.assertIn("COUNTY-A", page)
        # Nothing under blinded/ carries the slug, in a path or anywhere else.
        self.assertNotIn(
            "test-facility", str(path.relative_to(self.reports_dir))
        )

    def test_a_period_that_is_not_a_period_is_refused_with_exit_2(self):
        for period in ("../../case-studies/leaked", "not/a/period", "2026-q4"):
            with self.subTest(period=period):
                code, out = self.gap_report("--period", period)
                self.assertEqual(code, 2, out)
                self.assertIn("is not a period label", out)
                self.assertFalse(self.reports_dir.exists())

    def test_rerunning_the_command_does_not_move_the_blinded_sha(self):
        # Finding 17: the sha covered `generated_at`, so re-running with
        # identical inputs orphaned every review of the report.
        self.assertEqual(self.gap_report()[0], 0)
        first = reviews_mod.sha256_of(self.blinded_path())
        self.assertEqual(self.gap_report()[0], 0)
        self.assertEqual(reviews_mod.sha256_of(self.blinded_path()), first)

    def test_gap_report_refuses_unknown_fields(self):
        broken = self.dir / "broken.json"
        raw = fixtures_plans.facility_dict()
        raw["sprinklers"] = True
        broken.write_text(json.dumps(raw), encoding="utf-8")
        code, out = self.gap_report(facility=broken)
        self.assertEqual(code, 2)
        self.assertIn("unknown field 'sprinklers'", out)
        self.assertFalse(self.reports_dir.exists())

    def test_gap_report_refuses_an_address_with_the_reason(self):
        broken = self.dir / "addressed.json"
        raw = fixtures_plans.facility_dict()
        raw["address"] = "1 Example Street"
        broken.write_text(json.dumps(raw), encoding="utf-8")
        code, out = self.gap_report(facility=broken)
        self.assertEqual(code, 2)
        self.assertIn("'address' is refused", out)
        self.assertIn("Elevation Certificate", out)

    def test_gap_report_refuses_a_record_missing_evidence(self):
        broken = self.dir / "unevidenced.json"
        raw = fixtures_plans.facility_dict()
        del raw["evidence"]["power.fuel_hours"]
        broken.write_text(json.dumps(raw), encoding="utf-8")
        code, out = self.gap_report(facility=broken)
        self.assertEqual(code, 2)
        self.assertIn("no evidence entry names it", out)

    def test_gap_report_refuses_an_unknown_scenario(self):
        code, out = self.gap_report("--scenario", "no-such-scenario")
        self.assertEqual(code, 2)
        self.assertIn("no-such-scenario", out)

    def test_a_missing_intensity_reports_cannot_run_naming_the_document(self):
        path = fixtures_plans.write_facility(
            self.dir / "no-elevation.json", slug="no-elevation",
            **{"design_intensity.flood_elevation_ft": None},
        )
        code, out = self.gap_report(facility=path)
        self.assertEqual(code, 0, out)
        self.assertIn("q1  cannot_run", out)
        doc = cite.from_json(
            (self.reports_dir / "no-elevation" / "2026-Q4.json").read_text()
        )
        text = " ".join(cite.strip_markers(s.text) for s in doc.sentences)
        self.assertIn("cannot be run for the electrical equipment", text)
        guidance = {c.source.ref for c in doc.claims if c.source.kind == "guidance"}
        self.assertIn("fema-elevation-certificate", guidance)

    def test_out_writes_elsewhere(self):
        elsewhere = self.dir / "elsewhere"
        code, out = self.gap_report("--out", str(elsewhere))
        self.assertEqual(code, 0, out)
        self.assertTrue((elsewhere / "test-facility" / "2026-Q4.json").exists())
        self.assertFalse(self.reports_dir.exists())

    def test_a_document_with_a_violation_exits_1_and_prints_every_one(self):
        # The local drafter cannot produce a violating document — that is the
        # point of it — so the refusal is injected to check what the command
        # does with one: exit 1, print the rules broken, write nothing.
        violations = [
            cite.Violation(cite.UNCITED, 3, "no claim cited: 'Evacuate now.'"),
            cite.Violation(cite.NUMBER_WITHOUT_CLAIM, 4, "'44' is not a cited value"),
        ]
        refusal = gap_report_mod.GapReportRefused(violations, "refusing to write")
        with mock.patch.object(gap_report_mod, "write", side_effect=refusal):
            code, out = self.gap_report()
        self.assertEqual(code, 1)
        self.assertIn("not written", out)
        self.assertIn(cite.UNCITED, out)
        self.assertIn(cite.NUMBER_WITHOUT_CLAIM, out)
        self.assertFalse(self.reports_dir.exists())

    def test_the_drafter_choices_are_local_and_claude(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["gap-report", "--facility", "f", "--period", "2026-Q4",
                               "--drafter", "gpt"])
        args = parser.parse_args(["gap-report", "--facility", "f", "--period",
                                  "2026-Q4", "--drafter", "claude"])
        self.assertEqual(args.drafter, "claude")


class TestReviewCommand(Phase3Case):
    def blind_path(self) -> pathlib.Path:
        self.assertEqual(self.gap_report()[0], 0)
        label = fixtures_plans.make_facility(slug="test-facility").blind_label
        return self.reports_dir / "blinded" / label / "2026-Q4.blind.html"

    def test_it_records_a_rating_against_the_report_sha(self):
        blind = self.blind_path()
        code, out = self.run_cli(
            "review", "record", "--report", str(blind), "--rating", "very useful",
            "--role", "practising emergency manager", "--org-type", "county",
            "--years", "12", "--comments", "found the seam finding useful",
        )
        self.assertEqual(code, 0, out)
        sha = reviews_mod.sha256_of(blind)
        written = sorted(self.reviews_dir.glob("*.json"))
        self.assertEqual(len(written), 1)
        path = written[0]
        self.assertTrue(path.name.startswith(sha[:16] + "-"))
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["report_sha256"], sha)
        self.assertEqual(record["rating"], "very useful")
        self.assertTrue(record["blinded"])
        self.assertRegex(record["facility_label"], r"^FACILITY-[0-9a-f]{12}$")
        self.assertNotIn("test-facility", json.dumps(record))
        self.assertIn("attestation", out)

    def test_a_directory_passed_as_the_report_is_a_refusal_not_a_traceback(self):
        blind = self.blind_path()
        code, out = self.run_cli(
            "review", "record", "--report", str(blind.parent), "--rating", "useful",
            "--role", "practising emergency manager", "--org-type", "hospital",
            "--years", "5",
        )
        self.assertEqual(code, 2)
        self.assertIn("cannot be read", out)

    def test_it_refuses_a_page_that_is_not_a_blinded_render(self):
        self.assertEqual(self.gap_report()[0], 0)
        named = self.reports_dir / "test-facility" / "2026-Q4.html"
        code, out = self.run_cli(
            "review", "record", "--report", str(named), "--rating", "useful",
            "--role", "practising emergency manager", "--org-type", "hospital",
            "--years", "5",
        )
        self.assertEqual(code, 2)
        self.assertIn("not a blinded render", out)
        self.assertFalse(self.reviews_dir.exists())

    def test_the_rating_and_org_vocabularies_are_closed_in_the_parser(self):
        parser = cli.build_parser()
        base = ["review", "record", "--report", "r", "--role", "x",
                "--org-type", "hospital", "--years", "1"]
        parser.parse_args(base + ["--rating", "not useful"])
        for bad in (["--rating", "brilliant"],
                    ["--rating", "useful", "--org-type", "consultancy"]):
            with self.subTest(bad=bad), self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(base + bad)

    def test_reviews_writes_elsewhere(self):
        blind = self.blind_path()
        elsewhere = self.dir / "elsewhere"
        code, out = self.run_cli(
            "review", "record", "--report", str(blind), "--rating", "useful",
            "--role", "practising emergency manager", "--org-type", "ngo",
            "--years", "7", "--reviews", str(elsewhere),
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(len(list(elsewhere.glob("*.json"))), 1)
        self.assertFalse(self.reviews_dir.exists())


class TestVerifyPhase3(Phase3Case):
    def test_verify_phase3_takes_no_contract_and_reports_every_check(self):
        code, out = self.run_cli("verify", "--phase", "3")
        self.assertEqual(code, 1)
        self.assertIn("Phase 3 exit criteria  (the gap reports and their reviews)", out)
        for name in ("reviews", "reports", "case studies", "no coordinates"):
            self.assertIn(name, out)
        self.assertIn("attestation", out)

    def test_verify_phase3_refuses_a_contract_rather_than_ignoring_it(self):
        code, out = self.run_cli("verify", "-c", "flood-zz", "--phase", "3")
        self.assertEqual(code, 2)
        self.assertIn("takes no contract", out)

    def test_verify_phase3_reads_the_directories_it_is_given(self):
        for slug in ("alpha-ridge", "bravo-ridge", "charlie-ridge"):
            path = fixtures_plans.write_facility(self.dir / f"{slug}.json", slug=slug)
            self.assertEqual(self.gap_report(facility=path)[0], 0)
            label = fixtures_plans.make_facility(slug=slug).blind_label
            blind = self.reports_dir / "blinded" / label / "2026-Q4.blind.html"
            self.assertEqual(self.run_cli(
                "review", "record", "--report", str(blind), "--rating", "useful",
                "--role", "practising emergency manager", "--org-type", "county",
                "--years", "9",
            )[0], 0)
        code, out = self.run_cli(
            "verify", "--phase", "3", "--reports", str(self.reports_dir),
            "--reviews", str(self.reviews_dir),
        )
        self.assertEqual(code, 0, out)
        self.assertTrue(out.rstrip().endswith("Phase 3 exit criteria met."))
        self.assertIn("3/3 distinct facility", out)
        # The output is pasted into pull requests: it may not pair a blinded
        # label with the slug of the building it belongs to.
        for slug in ("alpha-ridge", "bravo-ridge", "charlie-ridge"):
            self.assertNotIn(slug, out.split("reports", 1)[1])

    def test_the_phase_flag_accepts_zero_to_three(self):
        parser = cli.build_parser()
        for phase in (0, 1, 2, 3):
            self.assertEqual(parser.parse_args(
                ["verify", "--phase", str(phase)]).phase, phase)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["verify", "--phase", "4"])


class TestNoCommittedPlansJsonHasAddressOrLatLon(unittest.TestCase):
    """The tripwire, over the committed tree, in the suite rather than in review."""

    def test_no_committed_plans_json_has_address_or_latlon(self):
        root = pathlib.Path(cli.data_mod.REPO_ROOT) / "plans"
        scanned = 0
        for path in sorted(root.rglob("*.json")):
            scanned += 1
            payload = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(path=str(path)):
                self.assertEqual(gap_report_mod.forbidden_keys(payload), [])
        self.assertGreaterEqual(scanned, 3)

    def test_the_scan_catches_a_place_in_a_value_as_well_as_a_key(self):
        # Otherwise the tripwire above is vacuous over a tree whose keys
        # nothing constrains: a case study is committed, and its free text is
        # where an address actually ends up.
        for payload, where in (
            ({"event": {"text": "A flood at 412 Riverside Drive."}}, "a street address"),
            ({"sources": [{"note": "27834-1234"}]}, "a ZIP+4"),
            ({"event": "35.6127, -77.3664"}, "a coordinate pair"),
            ({"note": "ZIP 27834"}, "the token ZIP"),
            ({"Lat": "35.9"}, "a case-variant key"),
        ):
            with self.subTest(where=where):
                self.assertTrue(gap_report_mod.forbidden_keys(payload), where)
        # A bare five-digit number is a county FIPS.
        self.assertEqual(
            gap_report_mod.forbidden_keys({"event": {"text": "county 27834"}}), []
        )

    def test_the_only_committed_facility_is_the_fictional_example(self):
        # git, not a glob: a glob over the working tree both misses what would
        # be committed (a subdirectory, a .JSON) and trips over the planner's
        # own, correctly ignored, records.
        from tests.test_facility import tracked

        self.assertEqual(
            tracked("plans/facilities"),
            ["plans/facilities/README.md",
             "plans/facilities/example-rural-hospital.json"],
        )

    def test_no_report_or_review_is_committed(self):
        from tests.test_facility import ignored, tracked

        self.assertEqual(tracked("plans/reports"), ["plans/reports/README.md"])
        self.assertEqual(tracked("plans/reviews"), ["plans/reviews/README.md"])
        for path in ("plans/reports/slug/2026-Q4.html",
                     "plans/reports/blinded/FACILITY-abc/2026-Q4.blind.html",
                     "plans/reports/notes.md",
                     "plans/reviews/2026/deadbeef.json",
                     "plans/reviews/review.JSON"):
            with self.subTest(path=path):
                self.assertTrue(ignored(path), f"{path} would be committed")


if __name__ == "__main__":
    unittest.main()
