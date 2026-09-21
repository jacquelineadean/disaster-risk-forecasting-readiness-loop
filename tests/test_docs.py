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

from readiness import cite, contracts, verify
from readiness.harness.ledger import ExperimentCard
from readiness.cli import build_parser
from readiness.engine.features import FEATURE_SETS
from readiness.harness.features import STATIC, TRANSFORMS

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
HOW_IT_WORKS = REPO_ROOT / "docs" / "how-it-works.md"
FEATURES_DOC = REPO_ROOT / "docs" / "features.md"
BACKTEST_DOC = REPO_ROOT / "docs" / "backtest.md"
BRIEF_DOC = REPO_ROOT / "docs" / "brief.md"
PLAN = REPO_ROOT / "docs" / "plan.md"
CARD_SKILL = REPO_ROOT / "skills" / "experiment-card.md"

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
# INTEGRATOR: the Phase 2 command surface, implemented by the Phase 2 CLI
# cluster (`readiness/issue.py`, `readiness/brief.py`, `exposure ...`). The
# docs name it now; the parser-backed assertions skip until that parser is
# merged and must run, not skip, afterwards.
PHASE2_COMMANDS = {"exposure", "issue", "brief"}
PHASE2_FLAGS = (
    ("verify --phase 2", ["verify", "--phase", "2"]),
    ("fleet --status", ["fleet", "--status"]),
    ("contracts --names --national", ["contracts", "--names", "--national"]),
    ("exposure snapshot --all-states", ["exposure", "snapshot", "--all-states"]),
    ("exposure show --state", ["exposure", "show", "--state", "OK"]),
    ("exposure spot-check --counts", ["exposure", "spot-check", "--counts", "x.csv"]),
    ("issue --period --param",
     ["issue", "m", "-c", "x", "--period", "2026-Q4", "--param", "l2=1"]),
    ("brief --county --period --out",
     ["brief", "--county", "40001", "--period", "2026-Q4", "--out", "d"]),
    ("brief --state", ["brief", "--state", "OK", "--period", "2026-Q4"]),
    ("issue --reissue",
     ["issue", "m", "-c", "x", "--period", "2026-Q4", "--reissue"]),
)
#: Paths the README map names that arrive with the CLI clusters.
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
            missing - PHASE1_COMMANDS - PHASE2_COMMANDS, set(),
            f"README's Commands block names subcommands the CLI does not have: {missing}",
        )
        if missing:
            self.skipTest(
                f"Phase 1/2 subcommands not in this tree's parser yet: {sorted(missing)}"
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

    def test_commands_block_names_the_phase2_surface(self):
        # Text-only, so it runs in every tree: the README documents exactly
        # the Phase 2 command surface, period labels included.
        block = re.search(r"## Commands\n\n```\n(.*?)```", README.read_text(), re.S).group(1)
        self.assertRegex(block, r"(?m)^readiness fleet .*--status")
        self.assertRegex(block, r"(?m)^readiness contracts .*--names")
        for sub in ("snapshot", "show", "spot-check"):
            self.assertRegex(block, rf"(?m)^readiness exposure {sub}\b")
        self.assertRegex(block, r"(?m)^readiness exposure snapshot .*--all-states")
        self.assertRegex(block, r"(?m)^readiness exposure spot-check .*--counts PATH")
        self.assertRegex(block, r"(?m)^readiness issue MODEL .*--period YYYY-Qn\|YYYY-Mnn\|YYYY")
        self.assertRegex(block, r"(?m)^readiness brief .*--county FIPS \| --state XX")
        self.assertRegex(block, r"(?m)^readiness verify .*--phase 0\|1\|2")
        # The queues the loop takes, and the fleet's own contract filter.
        self.assertRegex(block, r"(?m)^readiness loop .*--queue baseline\|phase1\|phase2")
        self.assertRegex(block, r"(?m)^readiness fleet .*--contracts A,B")
        issue = re.search(r"(?m)^readiness issue MODEL .*$", block).group(0)
        self.assertNotIn("label", issue)  # no parameter through which one could arrive
        self.assertIn("--reissue", issue)  # the only way to replace a published file

    def test_phase2_subcommands_are_in_the_parser(self):
        missing = PHASE2_COMMANDS - parser_commands()
        if missing:
            self.skipTest(f"INTEGRATOR: parser lacks {sorted(missing)}")
        # `verify --phase 2` takes no contract; `issue` has no label flag.
        self.assertTrue(parser_accepts(["verify", "--phase", "2"]))
        self.assertFalse(parser_accepts(
            ["issue", "m", "-c", "x", "--period", "2026-Q4", "--labels", "f"]))

    def test_phase2_flags_are_in_the_parser(self):
        for label, argv in PHASE2_FLAGS:
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


class TestBriefDocStatesTheRules(unittest.TestCase):
    """docs/brief.md names every violation code the validator can raise, the
    sentence shapes and the things a brief never contains, so a reader of a
    refusal can find the rule without reading readiness/cite.py."""

    CODES = (cite.UNCITED, cite.UNKNOWN_CLAIM, cite.UNRESOLVED,
             cite.VALUE_MISMATCH, cite.NUMBER_WITHOUT_CLAIM, cite.FORBIDDEN_PHRASE)

    def test_names_every_violation_code(self):
        text = BRIEF_DOC.read_text()
        for code in self.CODES:
            self.assertIn(f"`{code}`", text, f"docs/brief.md does not name {code}")
        # The codes above are the module's whole vocabulary.
        constants = {n for n, v in vars(cite).items()
                     if n.isupper() and isinstance(v, str) and v == n}
        self.assertEqual(constants, set(self.CODES))

    def test_states_the_shapes_guards_and_exclusions(self):
        text = BRIEF_DOC.read_text()
        for phrase in ("chance of at least one damaging", "This comes from",
                       cite.NOT_A_WARNING_SENTENCE, "nws-ipaws",
                       "passing", "digest", "cannot be issued yet",
                       "below the county", "would touch", "will occur",
                       "no parameter through which a label", "USA Structures",
                       "readiness brief --county", "--state"):
            self.assertIn(phrase, text, f"docs/brief.md lacks {phrase!r}")
        for phrase in cite.FORBIDDEN_PHRASES:
            self.assertIn(f'"{phrase}"', text, f"docs/brief.md lacks {phrase!r}")

    def test_how_it_works_names_the_phase2_commands(self):
        text = HOW_IT_WORKS.read_text()
        for command in ("readiness fleet --national", "readiness fleet --status",
                        "readiness exposure snapshot", "readiness exposure spot-check",
                        "readiness issue", "readiness brief --county",
                        "readiness brief --state", "readiness verify --phase 2"):
            self.assertIn(command, text, f"how-it-works.md lacks {command!r}")

    def test_how_it_works_states_the_issued_criterion_as_the_code_checks_it(self):
        # `verify._issued_check` needs MIN_NATIONAL_PASSES of the passing
        # contracts to have issued the same period, not all of them.
        text = HOW_IT_WORKS.read_text()
        self.assertIn(
            "at least four of the passing contracts have an issued file for the "
            "same period", text.replace("\n", " ").replace("  ", " "),
        )
        self.assertEqual(verify.MIN_NATIONAL_PASSES, 4)

    def test_the_readme_states_the_four_issuance_guards(self):
        text = README.read_text().replace("\n", " ")
        self.assertIn("`issue` has four guards", text)
        for guard in ("test* card", "training and feature digests",
                      "clean feature audit", "after every year the contract spans"):
            self.assertIn(guard, text, f"README does not state the guard {guard!r}")

    def test_the_plan_states_what_the_fleet_runs_and_the_spot_check_rule(self):
        text = PLAN.read_text().replace("\n", " ")
        while "  " in text:
            text = text.replace("  ", " ")
        self.assertIn("(delivered as the Phase 2 queue, §3.1)", text)
        self.assertIn("the ten must come from at least three states", text)
        self.assertIn("the fleet's default queue is the Phase 2 queue", text)


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


class TestExperimentCardSkillMatchesTheCard(unittest.TestCase):
    """The card runbook's claims about `wall_clock_s`, checked against the card.

    A runbook that says a field is not hashed, when it is, teaches a reader to
    expect two runs of one queue to produce the same hashes. They do not.
    """

    def _card(self, seconds: float) -> ExperimentCard:
        return ExperimentCard(
            experiment_id="exp-0001", timestamp="2026-01-01T00:00:00+00:00",
            model="logistic", version="1.0.0", split="validate", changed="",
            hypothesis="", outcome="", scorecard={}, verdict={},
            data_snapshot={"wall_clock_s": seconds},
        )

    def test_wall_clock_is_inside_the_card_hash_and_in_no_fingerprint(self):
        self.assertNotEqual(
            self._card(1.0).compute_hash(), self._card(2.0).compute_hash()
        )
        self.assertNotIn("wall_clock_s", verify.REPRO_FIELDS)
        text = " ".join(CARD_SKILL.read_text().split())
        self.assertIn("inside `card_hash`", text)
        self.assertIn("no reproducibility fingerprint contains it", text)
        self.assertNotIn("Nothing hashes or judges it", text)
        self.assertNotIn("is the same experiment", text)
