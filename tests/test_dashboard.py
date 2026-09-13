"""The ledger dashboard: self-contained, faithful to the ledger, one page per contract."""

import os
import pathlib
import re
import tempfile
import unittest
from unittest import mock

from readiness import contracts, dashboard
from readiness import data as data_mod
from readiness.agent import orchestrator
from readiness.harness.ledger import Ledger
from tests.fixtures import make_contract
from tests.test_orchestrator import synthetic_dataset


class DashboardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.env = mock.patch.dict(
            os.environ,
            {
                data_mod.EXPERIMENTS_DIR_ENV: str(self.dir / "experiments"),
                contracts.CONTRACTS_DIR_ENV: str(self.dir / "contracts"),
            },
        )
        self.env.start()
        self.c = make_contract(name="flood-zz")
        self.c.save(self.dir / "contracts")
        orchestrator.run_local(
            self.c, dataset=synthetic_dataset(self.c, self.dir), progress=lambda _m: None
        )

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class TestRender(DashboardCase):
    def test_page_is_self_contained(self):
        page = dashboard.render(self.c, Ledger(data_mod.paths(self.c).ledger))
        self.assertNotIn("<script", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn('src="', page)

    def test_page_carries_the_ledger(self):
        page = dashboard.render(self.c, Ledger(data_mod.paths(self.c).ledger))
        for needle in ("flood-zz", self.c.digest(), "exp-0001", "exp-0004",
                       "climatology-pooled", "leaky-oracle", "REJECTED", "chain intact"):
            self.assertIn(needle, page)
        self.assertEqual(page.count("<svg"), 4)  # one reliability diagram per card
        self.assertEqual(page.count("aria-label=\"reliability diagram\""), 4)

    def test_reliability_points_follow_the_bins(self):
        ledger = Ledger(data_mod.paths(self.c).ledger)
        card = next(c for c in ledger.read() if c.model == "climatology-seasonal")
        populated = [b for b in card.scorecard["reliability_bins"] if b["count"]]
        svg = dashboard.reliability_svg(
            card.scorecard["reliability_bins"], self.c.reliability_tolerance_pp
        )
        self.assertEqual(svg.count("<circle"), len(populated))

    def test_empty_ledger_renders(self):
        other = make_contract(name="empty-zz")
        page = dashboard.render(other, Ledger(data_mod.paths(other).ledger))
        self.assertIn("No experiments yet", page)

    def test_html_is_escaped(self):
        page = dashboard.render(
            make_contract(name="flood-zz", description="<b>bold</b>"),
            Ledger(data_mod.paths(self.c).ledger),
        )
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", page)


class TestWrite(DashboardCase):
    def test_write_lands_next_to_the_ledger(self):
        out = dashboard.write(self.c)
        self.assertEqual(out, self.dir / "experiments" / "flood-zz" / "dashboard.html")
        self.assertTrue(out.exists())

    def test_write_all_adds_an_index(self):
        written = dashboard.write_all()
        names = [p.name for p in written]
        self.assertEqual(names, ["dashboard.html", "index.html"])
        index = (self.dir / "experiments" / "index.html").read_text()
        self.assertIn("flood-zz/dashboard.html", index)
        self.assertIn("1 rejected by the canary", index)

    def test_cli_writes_the_page(self):
        import contextlib
        import io

        from readiness import cli

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["dashboard", "-c", "flood-zz"])
        self.assertEqual(code, 0)
        self.assertTrue(re.search(r"wrote .*flood-zz/dashboard\.html", out.getvalue()))


class TestExperimentsDirOverride(unittest.TestCase):
    def test_env_var_moves_every_contracts_paths(self):
        c = make_contract(name="flood-zz")
        with mock.patch.dict(os.environ, {data_mod.EXPERIMENTS_DIR_ENV: "/elsewhere"}):
            self.assertEqual(
                data_mod.paths(c).ledger, pathlib.Path("/elsewhere/flood-zz/ledger.jsonl")
            )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(data_mod.EXPERIMENTS_DIR_ENV, None)
            self.assertEqual(data_mod.paths(c).ledger.parent.parent, data_mod.EXPERIMENTS_DIR)


if __name__ == "__main__":
    unittest.main()
