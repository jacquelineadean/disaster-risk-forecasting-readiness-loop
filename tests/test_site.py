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
import inspect
import io
import json
import os
import pathlib
import re
import tempfile
import unittest
import zipfile
from unittest import mock

from readiness import brief as brief_mod
from readiness import fleet as fleet_mod
from readiness import cite, contracts, data as data_mod
from readiness.connectors.base import Manifest, SourceRecord
from readiness.connectors import usa_structures
from readiness.agent import orchestrator
from readiness.cli import build_parser
from readiness.engine.features import FEATURE_SETS
from readiness.engine.registry import REGISTRY
from tests.fixtures import make_contract, make_pilot_contract
from tests.test_connectors import PAGES, FakeLayer
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
            # A contract registered before its first run has no ledger yet.
            text = where.ledger.read_text() if where.ledger.exists() else ""
            lines = [x for x in text.splitlines() if x.strip()]
            self.assertEqual(len(ledgers[name]["cards"]), len(lines))
            for card, raw in zip(ledgers[name]["cards"], lines):
                self.assertEqual(card["raw"], raw)
                self.assertEqual(card["card_hash"], json.loads(raw)["card_hash"])
            self.assertTrue(ledgers[name]["status"]["valid"])
            if lines:
                self.assertEqual(ledgers[name]["anchor"]["n_cards"], len(lines))
            else:
                self.assertIsNone(ledgers[name]["anchor"])

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

    # --- Phase 2: the fleet, the briefs and the exposure pins ------------------

    def test_fleet_json_has_one_ledger_only_row_per_registered_contract(self):
        rows = {r["name"]: r for r in self._json("fleet.json")}
        registry = contracts.registered()
        self.assertEqual(set(rows), set(registry))
        self.assertEqual(len(rows), 9)
        ledgers = self._json("ledgers.json")
        for name, c in registry.items():
            row = rows[name]
            self.assertEqual(row["national"], not c.states)
            self.assertEqual(row["hazard"], c.hazard)
            self.assertEqual(row["period"], c.period)
            self.assertEqual(row["n_cards"], len(ledgers[name]["cards"]))
            self.assertIsInstance(row["validate_passes"], list)
            self.assertIn("test_card", row)
            self.assertIsInstance(row["phase1_ok"], bool)
            self.assertTrue(row["phase1_detail"])
        self.assertEqual(sum(r["national"] for r in rows.values()), 6)
        # contracts.json marks the same six, so the pages need not re-derive it.
        marked = {c["name"] for c in self._json("contracts.json") if c["national"]}
        self.assertEqual(marked, {n for n, r in rows.items() if r["national"]})

    def test_briefs_json_lists_only_briefs_that_validate(self):
        # None ship in the repository: every brief needs a passing test card.
        shipped = self._json("briefs.json")
        self.assertIsInstance(shipped, list)
        for entry in shipped:
            self.assertEqual(
                set(entry),
                {"fips", "period", "title", "contracts", "html", "generated_at", "inputs"},
            )
            self.assertTrue((self.out / entry["html"]).exists())
        # With a briefs/ tree: the validating brief is listed and its page is
        # rendered from the document that validated — never the committed
        # sibling .html, which is bytes nobody re-checked. The one with an
        # uncited sentence is not listed, whatever its file says, and neither
        # is one whose JSON names something below the county.
        nws = cite.Claim("g1", "official alerting", None, cite.Source("guidance", "nws-ipaws"))
        total = cite.Claim("n", "structures", 1234, cite.Source("manifest", "fema/x"), "{:,}")
        prob = cite.Claim("p", "chance", 0.12, cite.Source("issued", "issued/hail-us/2026-Q4.json#2026-Q4.40001"), "{:.0%}")
        good = cite.Document(
            "Adair County, OK — 2026-Q4", "county-brief",
            [cite.Sentence.from_text("12% chance of at least one damaging hail event [c:p]."),
             cite.Sentence.from_text("The county holds 1,234 structures [c:n]."),
             cite.Sentence.from_text(f"{cite.NOT_A_WARNING_SENTENCE} [c:g1].")],
            [prob, total, nws], "2026-09-16T00:00:00+00:00",
            {"county": "40001", "period": "2026-Q4", "contract": "hail-us"},
        )
        bad = cite.Document("Bad", "county-brief", [cite.Sentence("No citation here.")],
                            [nws], "2026-09-16T00:00:00+00:00")
        finer = cite.Document(
            good.title, good.kind, good.sentences, good.claims, good.generated_at,
            {**good.inputs, "county": "40005", "parcel": "0123-45"},
        )
        resolver = cite.DictResolver({
            "issued": {"issued/hail-us/2026-Q4.json": {"2026-Q4": {"40001": 0.12}}},
            "manifest": {"fema/x"},
        })
        with tempfile.TemporaryDirectory() as tmp:
            briefs = pathlib.Path(tmp) / "briefs"
            (briefs / "40001").mkdir(parents=True)
            (briefs / "40001" / "2026-Q4.json").write_text(cite.to_json(good))
            (briefs / "40001" / "2026-Q4.html").write_text("<html>the committed page</html>")
            (briefs / "40003").mkdir()
            (briefs / "40003" / "2026-Q4.json").write_text(cite.to_json(bad))
            (briefs / "40005").mkdir()
            (briefs / "40005" / "2026-Q4.json").write_text(cite.to_json(finer))
            out = pathlib.Path(tmp) / "generated"
            self.build.build_briefs(out, contracts.registered(), briefs_dir=briefs,
                                    resolver=resolver)
            listed = json.loads((out / "briefs.json").read_text())
            self.assertEqual([b["fips"] for b in listed], ["40001"])
            self.assertEqual(listed[0]["period"], "2026-Q4")
            self.assertEqual(listed[0]["contracts"], ["hail-us"])
            self.assertEqual(listed[0]["html"], "briefs/40001/2026-Q4.html")
            page = (out / listed[0]["html"]).read_text()
            self.assertNotIn("the committed page", page)
            self.assertIn("12% chance of at least one damaging hail event", page)
            # Escaped on the page, so match the part without the apostrophe.
            self.assertIn(brief_mod.READING_CAVEAT.split(";")[0], page)
            self.assertIn("NOAA National Centers for Environmental Information", page)
            self.assertFalse((out / "briefs" / "40003").exists())
            self.assertFalse((out / "briefs" / "40005").exists())

    def test_the_brief_resolver_knows_the_ledgers_issued_files_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            issued = pathlib.Path(tmp) / "issued" / "tornado-ok"
            issued.mkdir(parents=True)
            (issued / "2026-Q4.json").write_text(json.dumps(
                {"period": "2026-Q4", "probabilities": {"40001": 0.12}}
            ))
            r = self.build.brief_resolver(contracts.registered(), issued_dir=issued.parent)
        # A committed card, by bare id and qualified by its contract.
        self.assertIsNone(r.resolve(cite.Source("ledger", "exp-0001")))
        self.assertIsNone(r.resolve(cite.Source("ledger", "tornado-ok/exp-0001#scorecard")))
        self.assertIsNotNone(r.resolve(cite.Source("ledger", "exp-9999")))
        self.assertIsNone(r.resolve(cite.Source("issued", "issued/tornado-ok/2026-Q4.json#2026-Q4")))
        self.assertIsNotNone(r.resolve(cite.Source("issued", "issued/tornado-ok/2026-Q4.json#2027-Q1")))
        # A county qualifier resolves to that county's probability, so a brief
        # that quotes it is checked against it.
        self.assertEqual(
            r.resolve(cite.Source("issued", "issued/tornado-ok/2026-Q4.json#2026-Q4.40001")),
            0.12,
        )
        self.assertIsNotNone(
            r.resolve(cite.Source("issued", "issued/tornado-ok/2026-Q4.json#2026-Q4.40003"))
        )
        self.assertIsNotNone(r.resolve(cite.Source("manifest", "not/a/key")))
        self.assertIsNone(r.resolve(cite.Source("guidance", "nws-ipaws")))

    def test_exposure_json_is_empty_until_a_state_is_pinned(self):
        rows = self._json("exposure.json")
        self.assertIsInstance(rows, list)
        if not (data_mod.SNAPSHOT_DIR / "usa_structures").exists():
            self.assertEqual(rows, [])
        for row in rows:
            self.assertEqual(
                set(row), {"state_fips", "state", "n_counties", "total_structures",
                           "vintage", "manifest_key", "digest"},
            )
        # A pinned synthetic state shows up as one row, coarser than the county.
        with tempfile.TemporaryDirectory() as tmp:
            snapshots = pathlib.Path(tmp) / "snapshots"
            snapshots.mkdir()
            manifest = Manifest(path=snapshots / "manifest.json")
            usa_structures.snapshot(["99"], snapshots, manifest, session=FakeLayer(PAGES),
                                    page=3)
            manifest.save()
            out = pathlib.Path(tmp) / "generated"
            self.build.build_exposure(out, snapshot_dir=snapshots)
            rows = json.loads((out / "exposure.json").read_text())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state_fips"], "99")
        self.assertEqual(rows[0]["n_counties"], 2)
        self.assertEqual(rows[0]["vintage"], 2023)
        self.assertEqual(rows[0]["manifest_key"], "fema/usa_structures/99")
        self.assertNotIn("rows", rows[0])

    def test_every_page_links_the_briefs_page_in_the_shared_nav(self):
        for page in sorted(SITE.glob("*.html")):
            text = page.read_text(encoding="utf-8")
            self.assertIn('<a data-page="briefs" href="briefs.html">', text, page.name)
        briefs = (SITE / "briefs.html").read_text(encoding="utf-8")
        self.assertIn('data-page="briefs"', briefs)
        js = (SITE / "assets" / "briefs.js").read_text(encoding="utf-8")
        for name in ("briefs.json", "fleet.json", "exposure.json"):
            self.assertIn(f'"{name}"', js)
        self.assertIn("readiness brief", js)  # the empty state says how one is produced

    def test_every_button_sits_inside_its_row(self):
        # A button after the row's closing </div> is laid out on its own,
        # wider and out of line with the two beside it.
        for page in sorted(SITE.glob("*.html")):
            text = page.read_text(encoding="utf-8")
            stray = re.findall(r'</div>\s*<a class="btn[^"]*"', text)
            self.assertEqual(stray, [], f"{page.name}: a button sits outside .btn-row")
        walkthrough = (SITE / "walkthrough.html").read_text(encoding="utf-8")
        rows = re.findall(r'<div class="btn-row">(.*?)</div>', walkthrough, re.S)
        self.assertTrue(any("briefs.html" in row for row in rows))

    def test_the_site_says_the_fleet_runs_the_phase_2_queue(self):
        # `fleet` and `run_fleet` default to queue="phase2"; the page that
        # tells a reader what Phase 2 does must not name the Phase 1 queue.
        text = (SITE / "index.html").read_text(encoding="utf-8")
        self.assertIn("runs them through the Phase 2 queue", text)
        self.assertNotIn("runs them through the Phase 1 queue", text)
        self.assertEqual(
            inspect.signature(fleet_mod.run_fleet).parameters["queue"].default,
            "phase2",
        )

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

    def test_loop_promote_is_refused_exactly_like_promote(self):
        # `loop --promote` spends the same one touch as `promote`; a committed
        # budget must never be spent from a tab, whichever route asks for it.
        for argv in (("loop", "-c", "flood-zz", "--promote"),
                     ("loop", "-c", "flood-zz", "--promote", "--queue", "phase1"),
                     ("loop", "-c", "flood-zz", "--prom")):  # argparse abbreviation
            with self.subTest(argv=argv):
                code, text = self.cli(*argv)
                self.assertEqual(code, 2, text)
                self.assertIn("`readiness loop --promote` is not available in the "
                              "browser sandbox", text)
                self.assertIn("spent budget with no card in the repository", text)
        experiments = pathlib.Path(os.environ["READINESS_EXPERIMENTS_DIR"])
        self.assertFalse((experiments / "flood-zz" / "test_touches.json").exists())
        # The same loop without the flag still runs, and spends nothing.
        ds = self.sb._DATASETS[self.contract.digest()]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"READINESS_EXPERIMENTS_DIR": tmp}), \
                mock.patch("readiness.data.build", return_value=ds):
            code, text = self.cli("loop", "-c", "flood-zz", "--quiet")
            self.assertEqual(code, 0, text)
            self.assertFalse((pathlib.Path(tmp) / "flood-zz"
                              / "test_touches.json").exists())

    def test_fleet_promote_is_refused_exactly_like_loop_promote(self):
        # `fleet --promote` spends the same one touch as `promote`, once per
        # contract. Every spelling argparse would accept is refused.
        for argv in (("fleet", "--promote"),
                     ("fleet", "--contracts", "flood-zz", "--promote"),
                     ("fleet", "--national", "--queue", "phase2", "--promote"),
                     ("fleet", "--prom")):  # argparse abbreviation
            with self.subTest(argv=argv):
                code, text = self.cli(*argv)
                self.assertEqual(code, 2, text)
                self.assertIn("`readiness fleet --promote` is not available in the "
                              "browser sandbox", text)
                self.assertIn("spent budget with no card in the repository", text)
        experiments = pathlib.Path(os.environ["READINESS_EXPERIMENTS_DIR"])
        self.assertFalse((experiments / "flood-zz" / "test_touches.json").exists())
        # The fleet's read-only view still runs.
        self.assertEqual(self.cli("fleet", "--status")[0], 0)

    def test_phase2_writers_are_refused_and_fleet_status_runs(self):
        # `issue` and `brief` write records the repository commits; `exposure
        # snapshot` needs the network. Each is refused with the same shape as
        # `snapshot`. `fleet --status` reads the packed ledgers and runs.
        for argv in (("exposure", "snapshot", "--all-states"),
                     ("issue", "logistic", "-c", "flood-zz", "--period", "2026-Q4"),
                     ("brief", "--county", "40001", "--period", "2026-Q4")):
            with self.subTest(command=" ".join(argv[:2])):
                code, text = self.cli(*argv)
                self.assertEqual(code, 2)
                self.assertIn("is not available in the browser sandbox", text)
        code, text = self.cli("fleet", "--status")
        self.assertEqual(code, 0)
        self.assertIn("flood-zz", text)

    # INTEGRATOR: `verify --phase 2` (no -c) lands with the CLI cluster; this
    # must run, not skip, after integration.
    @unittest.skipUnless(parser_accepts("verify", "--phase", "2"),
                         "the CLI in this tree has no `verify --phase 2`")
    def test_verify_phase2_runs_in_the_browser(self):
        code, text = self.cli("verify", "--phase", "2")
        self.assertIn(code, (0, 1))
        self.assertNotIn("is not available in the browser sandbox", text)

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


class TestSandboxNeverPacksPartnerRecords(unittest.TestCase):
    """The archive is published. A partner's ground truth is not.

    A pilot's records file is pinned by hash and never redistributed
    (DATA-LICENSES.md), and the sandbox archive is the one artefact of this
    repository that is handed to strangers. So the packer is pointed at a
    synthetic tree that has a pilot in its registry and a records file in its
    snapshots, and the archive it produces is searched for both the bytes and
    the path.
    """

    SECRET = b"CONFIDENTIAL-PARTNER-ROW-DO-NOT-PUBLISH"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.out = self.dir / "generated"
        self.out.mkdir(parents=True)
        self.snapshots = self.dir / "snapshots"
        self.build = _load("build_site_private", ROOT / "tools" / "build_site.py")
        self.build.log = lambda _msg: None

        # The minimum tree `build_sandbox` reads from, so it can run against a
        # temporary root rather than the repository's own.
        (self.dir / "experiments").mkdir()
        (self.dir / "experiments" / "README.md").write_text("synthetic\n")
        (self.dir / "readiness").mkdir()
        (self.dir / "readiness" / "__init__.py").write_text("__version__ = '0.1'\n")
        (self.snapshots / "census").mkdir(parents=True)
        (self.snapshots / "census" / "national_county2020.txt").write_text(
            "STATE|STATEFP|COUNTYFP|COUNTYNS|COUNTYNAME|CLASSFP|FUNCSTAT\n"
            "ZZ|99|001|00000001|One County|H1|A\n"
        )
        self.contract = make_pilot_contract(name="flood-zz", sha256="a" * 64)
        self.records = self.snapshots / "records" / "zz_records.csv"
        self.records.parent.mkdir(parents=True)
        self.records.write_bytes(
            b"event_id,start_date,region_id,hazard,deaths,injured,damage_usd,source\n"
            + self.SECRET
            + b",2006-03-14,ZZ-ADM1-001,flood,2,11,450000,partner\n"
        )
        manifest = Manifest(path=self.snapshots / "manifest.json")
        manifest.add(
            data_mod.records_key(self.contract),
            SourceRecord(
                source="the partner", url="", sha256="a" * 64, bytes=1,
                fetched_at="2026-01-01T00:00:00+00:00",
                license="partner data; not redistributed",
            ),
        )
        manifest.save()
        self.registry = {"flood-zz": self.contract}
        self.patches = [
            mock.patch.object(data_mod, "SNAPSHOT_DIR", self.snapshots),
            mock.patch.object(self.build, "ROOT", self.dir),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def archive(self) -> zipfile.ZipFile:
        self.build.build_sandbox(self.out, self.registry)
        return zipfile.ZipFile(self.out / "sandbox.zip")

    def test_the_archive_holds_no_records_path_and_no_records_bytes(self):
        with self.archive() as zf:
            names = zf.namelist()
            self.assertIn("snapshots/census/national_county2020.txt", names)
            for name in names:
                self.assertFalse(
                    name.startswith("snapshots/records"), f"packed {name}"
                )
                self.assertNotIn(self.SECRET, zf.read(name), f"in {name}")

    def test_a_packed_manifest_drops_the_private_records(self):
        # The hashes are public — that is the point of pinning by hash — but
        # the archive has no use for a partner document's file name and should
        # not carry one.
        with self.archive() as zf:
            packed = json.loads(zf.read("snapshots/manifest.json"))
        self.assertEqual(packed["records"], {})
        self.assertNotIn("zz_records.csv", json.dumps(packed))

    def test_adding_such_a_file_raises_rather_than_skipping_quietly(self):
        # The guard is on the packer's `add` itself, so a pattern added later
        # cannot sweep one in: it raises where it would have written.
        self.assertTrue(self.build.is_private("snapshots/records/zz_records.csv"))
        self.assertTrue(self.build.is_private("snapshots/records"))
        self.assertFalse(self.build.is_private("snapshots/census/x.txt"))
        for key in ("records/ZZ/zz_records.csv", "emdat/ZY/zy.xlsx"):
            self.assertTrue(self.build.is_private_key(key))
        for key in ("noaa/storm_events/2010", "geoboundaries/ZZ/ADM1"):
            self.assertFalse(self.build.is_private_key(key))

    def test_the_two_label_source_prefixes_are_the_harnesss_own(self):
        # One statement of what "ground truth" means, not two that can drift.
        from readiness.connectors import CONNECTORS

        private = {
            info.key_prefix for name, info in CONNECTORS.items()
            if info.is_label_source and not info.network
        }
        self.assertEqual(private, set(self.build.PRIVATE_KEY_PREFIXES))
