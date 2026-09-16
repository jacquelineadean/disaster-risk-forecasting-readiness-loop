"""The command line, without any network.

Contract selection and registration run against a temporary registry. The
data commands (`panel`, `score`, `canary`, `loop`, `verify`) run against a
synthetic `Dataset` injected in place of `data.build`, with the experiments
tree and the blessed-fingerprint directory pointed at a temporary directory so
the committed ledgers and `harness_expected/` are never touched.
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

from readiness import cli, contracts, data as data_mod
from readiness.connectors.base import Manifest
from readiness.connectors.census import County
from readiness.harness import scoring
from readiness.harness.labels import diagnose
from tests.fixtures import make_panel


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


class TestScore(DataCase):
    def test_validate_split_scores_and_judges(self):
        code, out = self.run_cli("score", "climatology-seasonal", "-c", "flood-zz", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertIn("model            climatology-seasonal@", out)
        self.assertIn("split            validate", out)
        self.assertIn("contract flood-zz 1.0.0", out)
        self.assertIn("canary", out.lower())
        self.assertFalse(self.where.touch_budget.exists())

    def test_test_split_is_refused_without_spending_a_touch(self):
        code, out = self.run_cli("score", "climatology-pooled", "-c", "flood-zz",
                                 "--split", "test", "--quiet")
        self.assertEqual(code, 2)
        self.assertIn("refusing to score against the test split", out)
        self.assertNotIn("split            test", out)
        self.assertFalse(self.where.touch_budget.exists())

    def test_the_touch_is_spent_before_the_scores_exist(self):
        def boom(*_a, **_kw):
            raise RuntimeError("interrupted before any score")

        with mock.patch.object(scoring, "screen", boom):
            with self.assertRaises(RuntimeError):
                self.run_cli("score", "climatology-pooled", "-c", "flood-zz",
                             "--split", "test", "--spend-test-touch", "--quiet")
        self.assertEqual(json.loads(self.where.touch_budget.read_text()),
                         {"climatology-pooled@1.0.0": 1})
        # The budget is spent: the same model version cannot try again.
        code, out = self.run_cli("score", "climatology-pooled", "-c", "flood-zz",
                                 "--split", "test", "--spend-test-touch", "--quiet")
        self.assertEqual(code, 2)
        self.assertIn("already been scored against the test split", out)

    def test_spending_the_touch_scores_the_test_split_once(self):
        code, out = self.run_cli("score", "climatology-pooled", "-c", "flood-zz",
                                 "--split", "test", "--spend-test-touch", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertIn("test touch 1/1 spent for climatology-pooled@1.0.0", out)
        self.assertIn("split            test", out)
        self.assertLess(out.index("test touch 1/1"), out.index("split            test"))

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

    def test_loop_quiet_silences_progress_but_prints_the_result(self):
        code, out = self.run_cli("loop", "-c", "flood-zz", "--quiet")
        self.assertEqual(code, 0, out)
        self.assertNotIn("gather context", out)
        self.assertNotIn("take action", out)
        self.assertIn("leaky-oracle", out)
        self.assertIn("ledger: ", out)
        self.assertTrue(self.where.ledger.exists())


if __name__ == "__main__":
    unittest.main()
