"""Docs stay honest: no stale test counts, real subcommands, real digests.

These are not style checks. A README that names a subcommand the CLI no
longer has, or quotes a contract digest that has drifted from the code, is a
worse failure than a typo — it actively misleads whoever reads it first. Kept
here (rather than as prose review) so drift is caught by `make test`, the same
gate as everything else.
"""

import contextlib
import io
import re
import unittest
from pathlib import Path

from readiness import contracts
from readiness.cli import build_parser
from readiness.engine.features import FEATURE_SETS
from readiness.harness.features import STATIC, TRANSFORMS

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
HOW_IT_WORKS = REPO_ROOT / "docs" / "how-it-works.md"
FEATURES_DOC = REPO_ROOT / "docs" / "features.md"
BACKTEST_DOC = REPO_ROOT / "docs" / "backtest.md"

# INTEGRATOR: the Phase 1 command surface. The subcommands and flags below are
# implemented by the CLI cluster; the docs name them now. Until that parser is
# merged the parser-backed assertions skip; once it is, empty PENDING_PATHS,
# and every test here must run rather than skip.
PHASE1_COMMANDS = {"features", "promote", "backtest"}
PHASE1_FLAGS = (
    ("verify --phase", ["verify", "-c", "x", "--phase", "1"]),
    ("verify --replay", ["verify", "-c", "x", "--phase", "1", "--replay"]),
    ("loop --queue", ["loop", "-c", "x", "--queue", "phase1"]),
    ("loop --promote", ["loop", "-c", "x", "--promote"]),
    ("score --features", ["score", "m", "-c", "x", "--features", "era5"]),
    ("score --param", ["score", "m", "-c", "x", "--param", "l2=1"]),
    ("promote --spend-test-touch",
     ["promote", "m", "-c", "x", "--spend-test-touch"]),
)
#: Paths the README map names that arrive with the CLI cluster.
PENDING_PATHS = {"readiness/backtest.py"}


def parser_accepts(argv: list[str]) -> bool:
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            build_parser().parse_args(argv)
    except SystemExit:
        return False
    return True


def parser_commands() -> set[str]:
    parser = build_parser()
    action = next(a for a in parser._subparsers._group_actions if a.choices)
    return set(action.choices)

# A run of 16 hex digits, standing alone (not part of a longer hex run such as
# a sha256 or a git commit) — the contract digest format `readiness contract`
# and every experiment card print.
DIGEST_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])")

# "260 tests", "312 tests", etc: a hard-coded count that goes stale as the
# suite grows. Wording without a number is fine ("no network needed", "the
# test suite").
TEST_COUNT_RE = re.compile(r"\b\d+\s+tests\b")


class TestReadmeHasNoStaleCounts(unittest.TestCase):
    def test_no_hardcoded_test_count(self):
        text = README.read_text()
        matches = TEST_COUNT_RE.findall(text)
        self.assertEqual(
            matches, [], f"README.md names a hard-coded test count: {matches}"
        )

    def test_no_hardcoded_test_count_in_how_it_works(self):
        matches = TEST_COUNT_RE.findall(HOW_IT_WORKS.read_text())
        self.assertEqual(matches, [], f"how-it-works.md names a test count: {matches}")


class TestReadmeCommandsExist(unittest.TestCase):
    """Every `readiness <subcommand>` named in the Commands block is real."""

    def test_commands_block_matches_cli(self):
        text = README.read_text()
        block_match = re.search(r"## Commands\n\n```\n(.*?)```", text, re.S)
        self.assertIsNotNone(block_match, "README.md has no ## Commands block")
        block = block_match.group(1)

        named = set()
        for line in block.splitlines():
            line = line.strip()
            if not line or not line.startswith("readiness "):
                continue
            named.add(line.split()[1])
        self.assertTrue(named, "found no `readiness <subcommand>` lines to check")

        available = parser_commands()
        missing = named - available
        self.assertEqual(
            missing - PHASE1_COMMANDS, set(),
            f"README's Commands block names subcommands the CLI does not have: {missing}",
        )
        if missing:
            self.skipTest(
                f"Phase 1 subcommands not in this tree's parser yet: {sorted(missing)}"
            )

    def test_commands_block_names_the_phase1_surface(self):
        # Text-only, so it runs in every tree: the README documents exactly the
        # Phase 1 command surface, and the withdrawn flag is gone.
        text = README.read_text()
        block = re.search(r"## Commands\n\n```\n(.*?)```", text, re.S).group(1)
        for command in sorted(PHASE1_COMMANDS):
            self.assertRegex(block, rf"(?m)^readiness {command}\b")
        self.assertRegex(block, r"(?m)^readiness verify .*--phase 0\|1")
        self.assertRegex(block, r"(?m)^readiness loop .*--queue baseline\|phase1")
        self.assertRegex(block, r"(?m)^readiness promote MODEL .*--spend-test-touch")
        score = re.search(r"(?m)^readiness score MODEL .*$", block).group(0)
        self.assertNotIn("--spend-test-touch", score)
        self.assertNotIn("test", score.split("--split", 1)[1].split("]")[0])

    def test_phase1_subcommands_are_in_the_parser(self):
        missing = PHASE1_COMMANDS - parser_commands()
        if missing:
            self.skipTest(f"INTEGRATOR: parser lacks {sorted(missing)}")
        # `score --split test` is withdrawn in favour of promote.
        self.assertFalse(parser_accepts(["score", "m", "-c", "x", "--spend-test-touch"]))
        self.assertFalse(parser_accepts(["loop", "-c", "x", "--split", "test"]))

    def test_phase1_flags_are_in_the_parser(self):
        for label, argv in PHASE1_FLAGS:
            with self.subTest(flag=label):
                if not parser_accepts(argv):
                    self.skipTest(f"INTEGRATOR: parser lacks `{label}`")


class TestRepositoryMapPathsExist(unittest.TestCase):
    """Every path the README's repository map names is on disk.

    The map is the reader's index into the tree; an entry for a module that
    was renamed or never landed sends them to a 404. The block is parsed by
    indentation: an unindented entry is a top-level path, an entry indented by
    two spaces sits under the last top-level directory, and every `x.py`,
    `x.md` or `dir/` token in a description is resolved under that entry.
    """

    # A module, a document, or a directory (`x/` not followed by a name, so
    # `input_keys/pinned` in a description is prose, not a path).
    TOKEN = re.compile(r"[\w][\w./-]*(?:\.py|\.md|/(?!\w))")

    def _candidates(self, token: str, *dirs: str) -> list[Path]:
        return [REPO_ROOT / f"{d}{token}" for d in ("", *dirs)]

    def test_every_path_in_the_map_exists(self):
        text = README.read_text()
        block = re.search(r"## Repository map\n\n```\n(.*?)```", text, re.S)
        self.assertIsNotNone(block, "README.md has no ## Repository map block")
        top, entry = "", ""
        missing, pending = [], []
        for line in block.group(1).splitlines():
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            tokens = self.TOKEN.findall(line)
            if not tokens:
                continue
            if indent == 0:
                top = tokens[0] if tokens[0].endswith("/") else ""
                entry = top
            elif indent == 2:
                entry = f"{top}{tokens[0]}" if tokens[0].endswith("/") else top
            # A directory named in a description scopes the names after it on
            # the same line: `climada/ (run_event_set.py, ...)`.
            line_dir = entry
            for token in tokens:
                found = next(
                    (p for p in self._candidates(token, top, entry, line_dir)
                     if p.exists()), None,
                )
                if found is not None:
                    if token.endswith("/"):
                        line_dir = found.relative_to(REPO_ROOT).as_posix() + "/"
                    continue
                target = f"{top}{token}" if indent else token
                (pending if target in PENDING_PATHS else missing).append(target)
        self.assertEqual(missing, [], "README's repository map names missing paths")
        if pending:
            self.skipTest(f"INTEGRATOR: paths pending from the CLI cluster: {pending}")


class TestFeatureDocsCoverTheCode(unittest.TestCase):
    """docs/features.md names every transform and every feature set, so a
    reader of the firewall never meets a transform the doc does not explain."""

    def test_every_transform_is_named(self):
        text = FEATURES_DOC.read_text()
        for name in sorted(TRANSFORMS) + [STATIC]:
            self.assertIn(f"`{name}`", text, f"docs/features.md does not name {name!r}")

    def test_every_feature_set_is_named(self):
        text = FEATURES_DOC.read_text()
        for name, specs in FEATURE_SETS.items():
            self.assertIn(f"`{name}`", text, f"docs/features.md does not name set {name!r}")
            for spec in specs:
                self.assertIn(f"`{spec.column}`", text, f"column {spec.column!r} missing")

    def test_the_firewall_doc_states_the_rules(self):
        text = FEATURES_DOC.read_text()
        for phrase in ("through May", "National Risk Index", "2023", "finding",
                       "poison", "proves", "trusts", "lag_months >= 1"):
            self.assertIn(phrase, text)

    def test_the_backtest_doc_states_the_one_touch_rule(self):
        text = BACKTEST_DOC.read_text()
        for phrase in ("committed files only", "ledger-head", "new contract",
                       "first", "readiness promote"):
            self.assertIn(phrase, text)


class TestQuotedDigestsMatchTheCode(unittest.TestCase):
    """16-hex-digit digests quoted in the docs must match the live contracts.

    A digest is recomputed from every criterion in the contract file; if a
    quoted one is stale, either the doc or the contract changed without the
    other, and a reader who checks it by hand gets a mismatch that looks like
    a bug in the harness rather than a stale doc.
    """

    def test_readme_digests(self):
        self._check_file(README)

    def test_how_it_works_digests(self):
        self._check_file(HOW_IT_WORKS)

    def _check_file(self, path):
        text = path.read_text()
        found = set(DIGEST_RE.findall(text))
        if not found:
            return
        live = {c.digest() for c in contracts.registered().values()}
        stale = found - live
        self.assertEqual(
            stale, set(),
            f"{path.name} quotes digest(s) not produced by any registered "
            f"contract (stale or a typo): {sorted(stale)}",
        )


if __name__ == "__main__":
    unittest.main()
