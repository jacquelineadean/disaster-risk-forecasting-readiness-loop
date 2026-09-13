"""The command line: contract selection and registration, without any network."""

import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from readiness import cli, contracts


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


if __name__ == "__main__":
    unittest.main()
