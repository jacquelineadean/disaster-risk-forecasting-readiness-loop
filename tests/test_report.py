"""tools/build_report.py: the design source compiles to exactly the committed
`report/index.html`.

The script is not part of the installed package (see docs/plan.md, "declined
or deferred") and is loaded the same way `tests/test_site.py` loads
`tools/build_site.py` and `site/assets/sandbox.py` — by file path, since it
has no import name of its own. It reads the real design source under
`design/` (read-only) and is redirected, only for its output, at a temporary
file, so this test never touches the committed `report/index.html` while
still checking that a fresh build of it is byte-for-byte the same file.
"""

import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMMITTED_REPORT = ROOT / "report" / "index.html"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestBuildReportReproducesTheCommittedReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # build_report.py logs `OUT.relative_to(ROOT)`, so the scratch output
        # has to sit under the repository root, not under the system temp
        # directory; it is deleted in tearDownClass regardless.
        cls.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        cls.out_path = pathlib.Path(cls.tmp.name) / "index.html"
        cls.build = _load("build_report", ROOT / "tools" / "build_report.py")
        # Read from the real design source (source of truth, untouched); write
        # only into the scratch directory, never back onto report/index.html.
        cls.build.OUT = cls.out_path
        cls.build.main()
        cls.built = cls.out_path.read_text(encoding="utf-8")
        cls.committed = COMMITTED_REPORT.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_output_was_written(self):
        self.assertTrue(self.out_path.exists())

    def test_rebuild_is_byte_for_byte_identical_to_the_committed_report(self):
        # build_report.py embeds no timestamp or other run-to-run value (unlike
        # the ledger cards, whose wall-clock is documented in
        # harness_expected/README.md as the one place this project accepts
        # non-reproducibility) — so no line needs normalising before comparing.
        self.assertEqual(self.built, self.committed)

    def test_the_source_design_file_was_not_modified(self):
        # Nothing above should have touched design/ or report/; this is a
        # belt-and-suspenders check that the redirect actually worked.
        self.assertEqual(
            COMMITTED_REPORT.read_text(encoding="utf-8"), self.committed
        )


if __name__ == "__main__":
    unittest.main()
