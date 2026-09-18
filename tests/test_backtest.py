"""The backtest report: built from committed files only, and it says so verifiably.

No network, no dataset: `data.build` is patched to raise, so a report that
reached for the data plane would fail here. The ledgers are written card by
card with the helpers from `tests.test_verify`, and the committed
`snapshots/manifest.json` is read (never written) as the report would read it
from a clone.
"""

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from readiness import backtest, data as data_mod
from readiness.connectors.base import Manifest, SourceRecord
from readiness.harness.ledger import Ledger
from tests.fixtures import make_contract
from tests.test_verify import CLEAN_AUDIT, append_card

REFUSED_AUDIT = {
    "clean": False,
    "findings": [
        {"check": "admission", "passed": False,
         "detail": "static source 'nri' encodes data through 2023, but contract "
                   "'flood-zz' starts validating in 2016"},
    ],
}


def boom(*_a, **_kw):
    raise AssertionError("the backtest must not build a dataset")


class BacktestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.patches = [
            mock.patch.dict(os.environ, {data_mod.EXPERIMENTS_DIR_ENV: str(self.dir)}),
            mock.patch.object(data_mod, "build", boom),
        ]
        for p in self.patches:
            p.start()
        self.contract = make_contract(name="flood-zz")
        self.where = data_mod.paths(self.contract)
        self.ledger = Ledger(self.where.ledger)
        self.manifest = Manifest(path=self.dir / "manifest.json")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def render(self, expected=None) -> str:
        return backtest.render(self.contract, self.ledger, self.manifest, expected)


class TestRender(BacktestCase):
    def test_renders_from_committed_files_only_even_when_empty(self):
        html = self.render()
        self.assertIn("<title>flood-zz · backtest</title>", html)
        self.assertIn(self.contract.digest(), html)
        self.assertIn('<meta name="ledger-head" content="' + "0" * 64 + '">', html)
        self.assertIn('<meta name="test-card" content="">', html)
        self.assertIn("No validate cards yet", html)
        self.assertIn("No test card under contract", html)
        for what, _why in backtest.NOT_TRIED:
            self.assertIn(what, html)
        self.assertIn("Weather data by Open-Meteo.com (CC BY 4.0)", html)
        self.assertIn("Weather Service and IPAWS", html)
        self.assertNotIn("[if ", html)
        self.assertNotIn("<script", html)

    def test_embeds_test_card_hash_touch_count_and_ledger_head(self):
        append_card(self.ledger, self.contract, "validate", model="logistic", bss=-0.01)
        append_card(self.ledger, self.contract, "validate")
        card = append_card(self.ledger, self.contract, "test")
        html = self.render()
        self.assertIn(f'<meta name="test-card" content="{card.card_hash}">', html)
        self.assertIn(f'<meta name="ledger-head" content="{self.ledger.head()}">', html)
        self.assertEqual(self.ledger.head(), card.card_hash)
        self.assertIn("1 in the ledger, ever", html)
        self.assertIn("<svg", html)
        for term in ("reliability", "resolution", "uncertainty", "card hash"):
            self.assertIn(f"<dt>{term}</dt>", html)
        self.assertIn(card.card_hash, html)

    def test_counts_every_test_touch_in_the_ledger_not_just_the_current_one(self):
        earlier = make_contract(name="flood-zz", thresholds={"min_auc": 0.71})
        append_card(self.ledger, earlier, "test", auc=0.65)
        append_card(self.ledger, self.contract, "validate")
        card = append_card(self.ledger, self.contract, "test")
        html = self.render()
        self.assertIn("2 in the ledger, ever", html)
        self.assertIn(f'content="{card.card_hash}"', html)

    def test_lists_failures_and_the_inadmissible_nri_row(self):
        append_card(self.ledger, self.contract, "validate", model="persistence-last-year",
                    dev=0.2, kwargs={"hit": 0.35})
        append_card(self.ledger, self.contract, "validate", model="logistic",
                    kwargs={"feature_sets": ["nri"]}, audit=REFUSED_AUDIT)
        append_card(self.ledger, self.contract, "validate")
        html = self.render()
        self.assertIn("persistence-last-year@1.0.0", html)
        self.assertIn('<span class="tag fail">FAIL</span>', html)
        self.assertIn('<span class="tag pass">PASS</span>', html)
        self.assertIn("INADMISSIBLE", html)
        self.assertIn("<code>nri</code>", html)
        self.assertIn("encodes data through 2023", html)
        self.assertIn("Baseline risk: FEMA National Risk Index", html)
        self.assertIn("&quot;hit&quot;: 0.35", html)

    def test_pinned_nri_layer_is_refused_from_the_manifest_alone(self):
        self.manifest.add("fema/nri_counties_1.20", SourceRecord(
            source="FEMA National Risk Index, county table", url="https://x",
            sha256="a" * 64, bytes=1, fetched_at="2026-01-01T00:00:00+00:00",
            license="public domain",
            notes="derived_through=2023; FEMA National Risk Index v1.20",
        ))
        rows = backtest.inadmissible([], self.manifest, self.contract)
        self.assertEqual([r["source"] for r in rows], ["nri"])
        self.assertIn("2023", rows[0]["reason"])
        self.assertIn("2016", rows[0]["reason"])
        self.assertEqual(rows[0]["recorded"], "fema/nri_counties_1.20")
        # Under a contract that validates after the layer's vintage it is admitted.
        later = make_contract(splits={"train": [2000, 2023], "validate": [2024, 2024],
                                      "test": [2025, 2025]})
        self.assertEqual(backtest.inadmissible([], self.manifest, later), [])

    def test_feature_columns_carry_their_catalogue_specs(self):
        append_card(self.ledger, self.contract, "validate",
                    columns=("precip_3m", "water_share"))
        append_card(self.ledger, self.contract, "test",
                    columns=("precip_3m", "water_share"))
        f = backtest.facts(self.contract, self.ledger, self.manifest, None)
        by_column = {c["column"]: c for c in f["feature_columns"]}
        self.assertEqual(by_column["precip_3m"]["transform"], "trailing_sum")
        self.assertEqual(by_column["precip_3m"]["lag_months"], 1)
        self.assertEqual(by_column["water_share"]["source"], "gazetteer")
        self.assertEqual(f["feature_audit"], CLEAN_AUDIT)

    def test_attribution_resolves_conditional_lines(self):
        plain = backtest.attribution(self.contract, nri_shown=False, climada_used=False)
        self.assertFalse(any("FEMA" in line for line in plain))
        self.assertFalse(any("crosswalk" in line for line in plain))
        expanded = make_contract(zone_policy="expand")
        with_zones = backtest.attribution(expanded, nri_shown=True, climada_used=True)
        for prefix in ("Zone-county crosswalk", "Baseline risk: FEMA",
                       "Risk computation: CLIMADA"):
            self.assertTrue(any(line.startswith(prefix) for line in with_zones), prefix)
        self.assertFalse(any("Building footprints" in line for line in with_zones))
        missing = backtest.attribution(self.contract, nri_shown=False, climada_used=False,
                                       path=self.dir / "nope.md")
        self.assertEqual(missing, list(backtest.FALLBACK_ATTRIBUTION))

    def test_phase0_anchor_is_shown_when_blessed(self):
        expected = {"_data_version": "abc", "_contract": self.contract.digest(),
                    "climatology-pooled": {"brier_skill_score": 0.0, "auc": 0.5,
                                           "reliability_bins_sha256": "deadbeefdeadbeef"}}
        html = self.render(expected)
        self.assertIn("Phase 0 anchor", html)
        self.assertIn("deadbeefdeadbeef", html)
        self.assertNotIn("Phase 0 anchor", self.render(None))


class TestWrite(BacktestCase):
    def test_writes_the_page_and_a_json_twin_with_the_same_facts(self):
        append_card(self.ledger, self.contract, "validate")
        card = append_card(self.ledger, self.contract, "test")
        path = backtest.write(self.contract)
        self.assertEqual(path, self.where.directory / "backtest.html")
        twin = json.loads(path.with_suffix(".json").read_text())
        self.assertEqual(twin["test_card"], card.card_hash)
        self.assertEqual(twin["ledger_head"], self.ledger.head())
        self.assertEqual(twin["test_touches"], 1)
        self.assertEqual([r["status"] for r in twin["validate_history"]], ["PASS"])
        self.assertEqual(twin["contract_digest"], self.contract.digest())
        self.assertIn(
            "Weather data by Open-Meteo.com (CC BY 4.0); ERA5 by ECMWF/Copernicus.",
            twin["attribution"],
        )
        html = path.read_text()
        self.assertIn(card.card_hash, html)

    def test_output_path_is_honoured(self):
        target = self.dir / "elsewhere" / "report.html"
        path = backtest.write(self.contract, target)
        self.assertEqual(path, target)
        self.assertTrue(target.exists())
        self.assertTrue(target.with_suffix(".json").exists())


if __name__ == "__main__":
    unittest.main()
