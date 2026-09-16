"""Docs stay honest: no stale test counts, real subcommands, real digests.

These are not style checks. A README that names a subcommand the CLI no
longer has, or quotes a contract digest that has drifted from the code, is a
worse failure than a typo — it actively misleads whoever reads it first. Kept
here (rather than as prose review) so drift is caught by `make test`, the same
gate as everything else.
"""

import re
import unittest
from pathlib import Path

from readiness import contracts
from readiness.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
HOW_IT_WORKS = REPO_ROOT / "docs" / "how-it-works.md"

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

        parser = build_parser()
        subparsers_action = next(
            a for a in parser._subparsers._group_actions if a.choices
        )
        available = set(subparsers_action.choices)

        missing = named - available
        self.assertEqual(
            missing, set(),
            f"README's Commands block names subcommands the CLI does not have: {missing}",
        )


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
