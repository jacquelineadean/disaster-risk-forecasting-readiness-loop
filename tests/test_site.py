"""The overview website: generated from the repository's own artefacts, and its
browser sandbox's adapters checked natively.

No network. The build packs whatever pinned data is present in `snapshots/`
and must work — with a smaller archive — when there is none; the sandbox
module is exercised against a synthetic dataset.
"""

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import tempfile
import unittest
import zipfile
from unittest import mock

from readiness import contracts, data as data_mod
from readiness.agent import orchestrator
from readiness.cli import build_parser
from readiness.engine.features import FEATURE_SETS
from readiness.engine.registry import REGISTRY
from tests.fixtures import make_contract
from tests.test_orchestrator import synthetic_dataset

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE = ROOT / "site"


def parser_accepts(*argv: str) -> bool:
    """Whether the CLI parser in this tree takes `argv` (Phase 1 flags land
    with the CLI cluster; the sandbox tests that need them skip until then)."""
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            build_parser().parse_args(list(argv))
    except SystemExit:
        return False
    return True


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestBuildSite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = pathlib.Path(cls.tmp.name) / "generated"
        cls.build = _load("build_site", ROOT / "tools" / "build_site.py")
        cls.build.log = lambda _msg: None  # keep the test run quiet
        cls.build.build(cls.out, sandbox=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _json(self, name):
        return json.loads((self.out / name).read_text())

    def test_contracts_json_matches_the_registry(self):
        registry = contracts.registered()
        view = {c["name"]: c for c in self._json("contracts.json")}
        self.assertEqual(set(view), set(registry))
        for name, c in registry.items():
            self.assertEqual(view[name]["digest"], c.digest())
            self.assertEqual(view[name]["canonical_json"], c.canonical_json())
            self.assertEqual(view[name]["describe"], c.describe())
            self.assertEqual(view[name]["spec"], c.to_spec())

    def test_ledgers_json_carries_every_card_its_raw_line_and_the_chain_status(self):
        ledgers = self._json("ledgers.json")
        for name, c in contracts.registered().items():
            where = data_mod.paths(c)
            lines = [x for x in where.ledger.read_text().splitlines() if x.strip()]
            self.assertEqual(len(ledgers[name]["cards"]), len(lines))
            for card, raw in zip(ledgers[name]["cards"], lines):
                self.assertEqual(card["raw"], raw)
                self.assertEqual(card["card_hash"], json.loads(raw)["card_hash"])
            self.assertTrue(ledgers[name]["status"]["valid"])
            self.assertEqual(ledgers[name]["anchor"]["n_cards"], len(lines))

    def test_the_browser_verification_premise_holds(self):
        # ledgers.js hashes each raw line with its card_hash member removed.
        # That equals ExperimentCard.compute_hash() only because Ledger.append
        # writes canonical JSON; assert it for every committed card.
        for led in self._json("ledgers.json").values():
            for card in led["cards"]:
                payload = re.sub(r',"card_hash":"[0-9a-f]{64}"', "", card["raw"], count=1)
                digest = hashlib.sha256(payload.encode()).hexdigest()
                self.assertEqual(digest, card["card_hash"])

    def test_sandbox_archive_carries_the_package_registry_ledgers_and_fingerprints(self):
        names = zipfile.ZipFile(self.out / "sandbox.zip").namelist()
        for needed in (
            "readiness/cli.py",
            "readiness/harness/scoring.py",
            "contracts/tornado-ok.json",
            "experiments/tornado-ok/ledger.jsonl",
            "experiments/tornado-ok/ledger.jsonl.anchor.json",
            "harness_expected/tornado-ok.json",
            "skills/verification-protocol.md",
            "snapshots/sandbox_coverage.json",
        ):
            self.assertIn(needed, names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")])
        self.assertFalse([n for n in names if n.endswith("_extract.jsonl")])

    def test_sandbox_json_describes_coverage_per_contract(self):
        info = self._json("sandbox.json")
        self.assertEqual(set(info["contracts"]), set(contracts.registered()))
        for entry in info["contracts"].values():
            self.assertIn(entry["coverage"], ("full", "hazard-only", "missing"))
            if entry["coverage"] == "missing":
                self.assertFalse(entry["reproducible"])
        panels = self._json("panels.json")
        for name, entry in info["contracts"].items():
            if entry["coverage"] == "missing":
                self.assertIsNone(panels[name])
            else:
                self.assertIsNotNone(panels[name])
                self.assertIn("n_units", panels[name])

    def test_catalogue_and_models_views(self):
        hazards = self._json("hazards.json")
        self.assertIn("tornado", hazards)
        self.assertEqual(hazards["tornado"]["coding"], "county")
        models = self._json("models.json")
        self.assertEqual([q["model"] for q in models["queue"]],
                         [c.model for c in orchestrator.BASELINE_QUEUE])
        self.assertEqual(models["canary_candidate"]["model"], "leaky-oracle")

    def test_models_json_carries_each_models_parameter_schema_in_order(self):
        # A list, not an object: the build sorts object keys, and the order is
        # the constructor's. The playground renders one input per entry.
        by_name = {m["name"]: m for m in self._json("models.json")["registry"]}
        self.assertEqual(set(by_name), set(REGISTRY))
        for name, spec in REGISTRY.items():
            exported = by_name[name]["params"]
            self.assertEqual([p["name"] for p in exported], list(spec.params))
            for p in exported:
                self.assertEqual({k: v for k, v in p.items() if k != "name"},
                                 spec.params[p["name"]])
        self.assertEqual([p["name"] for p in by_name["persistence-last-year"]["params"]],
                         ["hit", "miss"])

    def test_features_json_is_the_engine_catalogue(self):
        catalogue = self._json("features.json")
        self.assertEqual([f["name"] for f in catalogue], list(FEATURE_SETS))
        for entry in catalogue:
            specs = FEATURE_SETS[entry["name"]]
            self.assertEqual(entry["columns"], [s.to_dict() for s in specs])
            for column in entry["columns"]:
                self.assertEqual(
                    set(column),
                    {"column", "source", "variable", "transform", "window_months",
                     "lag_months"},
                )

    def test_models_json_exports_needs_features_and_the_list_type(self):
        models = self._json("models.json")
        by_name = {m["name"]: m for m in models["registry"]}
        for name, spec in REGISTRY.items():
            self.assertEqual(by_name[name]["needs_features"], spec.needs_features)
        feature_models = [n for n, m in by_name.items() if m["needs_features"]]
        self.assertEqual(sorted(feature_models),
                         ["gbm", "gbm+iso", "logistic", "logistic+iso"])
        for name in feature_models:
            params = {p["name"]: p for p in by_name[name]["params"]}
            self.assertEqual(params["feature_sets"]["type"], "list")
            self.assertIsInstance(params["feature_sets"]["default"], list)
            self.assertTrue(set(params["feature_sets"]["default"]) <= set(FEATURE_SETS))
            self.assertEqual(params["history"]["type"], "bool")
        # The Phase 1 queue is exported when the orchestrator defines it; the
        # key is always present so the page never has to guess.
        self.assertIsInstance(models["phase1_queue"], list)
        for candidate in models["queue"] + models["phase1_queue"]:
            self.assertIn("kwargs", candidate)

    def test_contracts_json_records_which_contracts_have_a_backtest(self):
        for entry in self._json("contracts.json"):
            self.assertIn("backtest", entry)
            if entry["backtest"] is not None:
                self.assertTrue((self.out / entry["backtest"]).exists())
        # With a committed report the entry names the copied file.
        c = contracts.registered()["tornado-ok"]
        with tempfile.TemporaryDirectory() as tmp:
            experiments = pathlib.Path(tmp) / "experiments"
            (experiments / c.name).mkdir(parents=True)
            (experiments / c.name / "backtest.html").write_text(
                "<html><meta name=\"ledger-head\" content=\"abc\"></html>"
            )
            out = pathlib.Path(tmp) / "generated"
            with mock.patch.dict(os.environ, {"READINESS_EXPERIMENTS_DIR": str(experiments)}):
                rel = self.build.copy_backtest(c, out)
            self.assertEqual(rel, "backtest/tornado-ok.html")
            self.assertIn("ledger-head", (out / rel).read_text())

    def test_the_playground_types_its_parameter_inputs_from_the_schema(self):
        # A `list` parameter is a comma-separated text field, a `bool` a
        # checkbox; the JavaScript reads the type from models.json and the set
        # names from features.json rather than restating either.
        js = (SITE / "assets" / "playground.js").read_text(encoding="utf-8")
        self.assertIn('"features.json"', js)
        self.assertIn('data-type="${p.type}"', js)
        for needle in ('p.type === "list"', 'p.type === "bool"', 'type="checkbox"',
                       'split(",")'):
            self.assertIn(needle, js)
        for name in FEATURE_SETS:
            self.assertNotIn(f'"{name}"', js, "playground.js restates a feature set")

    def test_contract_defaults_json_is_the_packages_defaults(self):
        self.assertEqual(self._json("contract_defaults.json"), dict(contracts.DEFAULTS))

    def test_the_playground_reads_defaults_and_parameters_instead_of_restating(self):
        js = (SITE / "assets" / "playground.js").read_text(encoding="utf-8")
        self.assertIn('"contract_defaults.json"', js)
        self.assertIn('"models.json"', js)
        for value in (contracts.DEFAULTS["train"], contracts.DEFAULTS["validate"],
                      contracts.DEFAULTS["test"]):
            self.assertNotIn(value, js, f"playground.js restates the default {value!r}")
        for spec in REGISTRY.values():
            for p in spec.params.values():
                self.assertNotIn(p["help"], js, "playground.js restates a param label")

    def test_tapes_json_packs_each_built_panel_as_bits(self):
        tapes = self._json("tapes.json")
        for name, panel in self._json("panels.json").items():
            if panel is None:
                self.assertNotIn(name, tapes)
                continue
            tape = tapes[name]
            self.assertEqual(tape["n_regions"], panel["n_regions"])
            self.assertEqual(tape["n_regions"] * tape["n_periods"], panel["n_units"])
            self.assertEqual(len(tape["regions"]), tape["n_regions"])
            bits = base64.b64decode(tape["bits"])
            self.assertEqual(len(bits), (tape["n_regions"] * tape["n_periods"] + 7) // 8)
            lit = sum(bin(b).count("1") for b in bits)
            self.assertEqual(lit, panel["n_positive"])
            self.assertEqual(sum(tape["counts"]), panel["n_positive"])
            self.assertEqual(sum(tape["freq"]), panel["n_positive"])
            self.assertEqual(len(tape["counts"]), tape["n_periods"])

    def test_pages_share_one_top_bar_and_one_typeface(self):
        top = re.compile(r'<header class="top".*?</header>', re.S)
        font = re.compile(r'<link href="https://fonts\.googleapis\.com/css2\?[^"]*" rel="stylesheet">')
        bars, fonts = set(), set()
        for page in sorted(SITE.glob("*.html")):
            text = page.read_text(encoding="utf-8")
            bar = top.search(text)
            self.assertIsNotNone(bar, f"{page.name} has no top bar")
            bars.add(bar.group(0))
            link = font.search(text)
            self.assertIsNotNone(link, f"{page.name} loads no typeface")
            fonts.add(link.group(0))
            self.assertIn(f'data-page="{page.stem}"', text)
        self.assertEqual(len(bars), 1, "the pages' top bars differ")
        self.assertEqual(len(fonts), 1, "the pages load different typefaces")

    def test_the_look_borrows_no_assets_from_its_inspiration(self):
        # The design is inspired by a research microsite; nothing is copied from
        # it: no framework class names, no brand typefaces, no hosted styles.
        borrowed = re.compile(r"glue-|Google\+Sans|Product\+Sans|gstatic\.com/glue")
        for path in sorted(list(SITE.glob("*.html")) + list((SITE / "assets").glob("*"))):
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="ignore")
                self.assertIsNone(borrowed.search(text), path.name)

    def test_pages_reference_only_files_that_exist(self):
        attr = re.compile(r'\b(?:href|src)="([^"#][^"]*)"')
        for page in sorted(SITE.glob("*.html")):
            for ref in attr.findall(page.read_text(encoding="utf-8")):
                if re.match(r"^(https?:|data:|mailto:|//)", ref) or "${" in ref:
                    continue  # external, inline data, or a template literal in a script
                path = ref.split("?")[0].split("#")[0]
                if path.startswith("generated/"):
                    target = self.out / path[len("generated/"):]
                    if path.startswith(("generated/media/", "generated/report/")):
                        continue  # only present when the source media exists
                else:
                    target = SITE / path
                self.assertTrue(target.exists(), f"{page.name} references missing {ref}")


class TestSandboxModule(unittest.TestCase):
    """site/assets/sandbox.py, the Python side of the browser sandbox, run natively."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.tmp.name)
        cls.contracts_dir = root / "contracts"
        cls.contract = make_contract(name="flood-zz")
        cls.contract.save(cls.contracts_dir)
        cls.env = mock.patch.dict(os.environ, {
            "SANDBOX_REPO": str(ROOT),
            "SANDBOX_ROOT": str(root / "sandbox"),
            "READINESS_EXPERIMENTS_DIR": str(root / "sandbox" / "experiments"),
            contracts.CONTRACTS_DIR_ENV: str(cls.contracts_dir),
        })
        cls.env.start()
        cls.cwd = os.getcwd()
        cls.sb = _load("sandbox", SITE / "assets" / "sandbox.py")
        cls.sb._DATASETS[cls.contract.digest()] = synthetic_dataset(cls.contract, root)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        cls.env.stop()
        cls.tmp.cleanup()

    def call(self, fn, **args):
        return json.loads(getattr(self.sb, fn)(json.dumps(args)))

    def test_info_lists_the_registry_and_reports_data_gaps(self):
        info = self.call("sandbox_info")
        self.assertEqual(list(info["contracts"]), ["flood-zz"])
        entry = info["contracts"]["flood-zz"]
        self.assertEqual(entry["digest"], self.contract.digest())
        self.assertFalse(entry["has_data"])  # ZZ is not a state; nothing packed for it
        self.assertTrue(entry["data_gap"])

    def test_cli_runs_and_refuses_the_commands_the_browser_cannot_run(self):
        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.sb.run_cli(json.dumps(list(argv)))
            return code, out.getvalue() + err.getvalue()

        code, text = run("contracts")
        self.assertEqual(code, 0)
        self.assertIn("flood-zz", text)
        self.assertEqual(run("contract", "-c", "missing")[0], 2)
        code, text = run("snapshot", "-c", "flood-zz")
        self.assertEqual(code, 2)
        self.assertIn("not available in the browser sandbox", text)
        self.assertEqual(run("mcp")[0], 2)
        self.assertEqual(run("--nonsense")[0], 2)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.sb.run_cli(json.dumps(list(argv)))
        return code, out.getvalue() + err.getvalue()

    def test_features_reports_no_packed_sources_per_connector(self):
        # The archive packs Storm Events extracts only. `features` must say so
        # for every connector rather than fail on a missing pinned file.
        code, text = self.cli("features", "-c", "flood-zz")
        self.assertEqual(code, 0)
        for name in data_mod.FEATURE_CONNECTORS:
            self.assertRegex(text, rf"{name}\s+no feature sources packed")
        for set_name in FEATURE_SETS:
            self.assertIn(set_name, text)
        code, text = self.cli("features", "-c", "flood-zz", "--features", "era5,nri")
        self.assertEqual(code, 0)
        self.assertIn("era5", text)
        self.assertNotRegex(text, r"climada\s+no feature sources packed")
        self.assertEqual(self.cli("features", "--features", "bogus")[0], 2)
        info = self.call("sandbox_info")
        self.assertEqual(info["feature_sources_packed"], [])
        self.assertEqual(info["feature_connectors"], list(data_mod.FEATURE_CONNECTORS))

    def test_promote_and_backtest_are_refused_like_snapshot(self):
        # A test touch spent from a browser is a spent budget with no card in
        # the repository; the refusal has the same shape as snapshot's.
        for argv in (("promote", "logistic", "-c", "flood-zz", "--spend-test-touch"),
                     ("backtest", "-c", "flood-zz"),
                     ("snapshot", "-c", "flood-zz")):
            with self.subTest(command=argv[0]):
                code, text = self.cli(*argv)
                self.assertEqual(code, 2)
                self.assertIn(f"`readiness {argv[0]}` is not available in the browser "
                              "sandbox", text)
        self.assertFalse(
            (pathlib.Path(os.environ["READINESS_EXPERIMENTS_DIR"]) / "flood-zz"
             / "test_touches.json").exists()
        )

    def test_playground_test_refusal_names_promote(self):
        r = self.call("score_playground", contract="flood-zz",
                      model="climatology-pooled", split="test")
        self.assertIn("readiness promote", r["error"])
        self.assertNotIn("score --split test", r["error"])

    # INTEGRATOR: this test needs `loop --queue phase1` from the CLI cluster; it
    # skips until that parser lands and must run (not skip) after integration.
    @unittest.skipUnless(parser_accepts("loop", "-c", "x", "--queue", "phase1"),
                         "the CLI in this tree has no `loop --queue`")
    def test_loop_phase1_runs_history_only_and_skips_feature_candidates(self):
        ds = self.sb._DATASETS[self.contract.digest()]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"READINESS_EXPERIMENTS_DIR": tmp}), \
                mock.patch("readiness.data.build", return_value=ds):
            code, text = self.cli("loop", "-c", "flood-zz", "--queue", "phase1",
                                  "--quiet")
            self.assertEqual(code, 0)
            self.assertIn("logistic", text)
            self.assertIn("skip", text.lower())
            ledger = pathlib.Path(tmp) / "flood-zz" / "ledger.jsonl"
            models = [json.loads(line)["model"] for line in ledger.read_text().splitlines()
                      if line.strip()]
            self.assertIn("logistic", models)
            self.assertNotIn("gbm", models)

    def test_playground_rescoring_goes_through_the_harness_and_the_canary(self):
        r = self.call("score_playground", contract="flood-zz",
                      model="climatology-seasonal", params={"shrinkage": 10},
                      scale=1.0, shift=0.0)
        self.assertEqual(r["model"], "climatology-seasonal")
        self.assertIn("checks", r["verdict"])
        self.assertFalse(r["canary"]["rejected"])
        self.assertEqual(r["scorecard"]["brier_skill_score"],
                         r["base_scorecard"]["brier_skill_score"])
        sharpened = self.call("score_playground", contract="flood-zz",
                              model="climatology-seasonal", params={"shrinkage": 10},
                              scale=2.0, shift=0.0)
        self.assertEqual(sharpened["model"], "climatology-seasonal+recal")
        self.assertGreater(sharpened["scorecard"]["sharpness"],
                           r["scorecard"]["sharpness"])
        oracle = self.call("score_playground", contract="flood-zz", model="leaky-oracle",
                           params={}, scale=1.0, shift=0.0)
        self.assertTrue(oracle["canary"]["rejected"])
        self.assertTrue(oracle["verdict"]["passed"])

    def test_playground_refuses_every_split_but_validate(self):
        # The test split is a budgeted one-shot holdout and the browser has no
        # budget; the training split is not a holdout. Neither may be scored.
        for split in ("test", "train", "holdout"):
            with self.subTest(split=split):
                r = self.call("score_playground", contract="flood-zz",
                              model="climatology-pooled", split=split)
                self.assertEqual(set(r), {"error"})
                self.assertIn("SplitViolation", r["error"])
                self.assertIn("validate split only", r["error"])
        self.assertNotIn("error", self.call("score_playground", contract="flood-zz",
                                            model="climatology-pooled", split="validate"))

    def test_caches_go_stale_when_the_criteria_change_under_the_same_name(self):
        path = self.contracts_dir / "flood-zz.json"
        original = path.read_text()
        try:
            # `register --force` with a changed criterion: same name, new digest.
            make_contract(name="flood-zz", period="month").save(self.contracts_dir,
                                                                 force=True)
            r = self.call("score_playground", contract="flood-zz",
                          model="climatology-pooled")
            self.assertIn("error", r)
            self.assertIn("cannot build the panel for flood-zz", r["error"])
            self.assertNotIn(contracts.load("flood-zz").digest(), self.sb._DATASETS)
            fp = self.call("fingerprints", contract="flood-zz")
            self.assertIn("cannot build the panel", fp["error"])
        finally:
            path.write_text(original)
        self.assertNotIn("error", self.call("score_playground", contract="flood-zz",
                                            model="climatology-pooled"))

    def test_tamper_breaks_the_chain_and_restore_mends_it(self):
        state = self.call("ledger_state", contract="flood-zz")
        self.assertFalse(state["exists"])
        orchestrator.run_local(self.contract,
                               dataset=self.sb._DATASETS[self.contract.digest()],
                               progress=lambda _m: None)
        self.assertTrue(self.call("ledger_state", contract="flood-zz")["valid"])
        for action in ("edit", "swap", "delete-middle", "truncate", "no-anchor"):
            broken = self.call("tamper", contract="flood-zz", action=action)
            self.assertFalse(broken["valid"], action)
            self.assertIn("BROKEN", broken["status"])
            mended = self.call("restore_ledger", contract="flood-zz")
            self.assertTrue(mended["valid"], action)
        forged = self.call("tamper", contract="flood-zz", action="forge")
        self.assertTrue(forged["valid"])
        self.assertTrue(forged["forged"])
        self.assertTrue(self.call("restore_ledger", contract="flood-zz")["valid"])
        self.assertEqual(self.call("ledger_state", contract="flood-zz")["n_cards"], 4)
        html = self.call("dashboard_html", contract="flood-zz")["html"]
        self.assertIn("<svg", html)
        self.assertIn("chain intact", html)

    def test_fingerprints_report_nothing_blessed_for_a_new_contract(self):
        r = self.call("fingerprints", contract="flood-zz")
        self.assertFalse(r["has_expected"])
        self.assertEqual(r["observed"]["_contract"], self.contract.digest())
        self.assertEqual(r["observed"]["climatology-pooled"]["brier_skill_score"], 0.0)

    def test_errors_come_back_as_json_not_exceptions(self):
        self.assertIn("error", self.call("score_playground", contract="nope"))
        self.assertIn("error", self.call("tamper", contract="flood-zz", action="burn"))
