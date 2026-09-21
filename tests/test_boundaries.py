"""Import boundaries, checked on the AST so nothing under test is imported.

The rules the audit found enforced only by docstrings (plan §1 R6): the engine
never imports the contract judge or the canary, the harness never imports the
engine, the agent plane or an LLM client, and the whole package is standard
library plus itself except for the SDK behind `try:` in `readiness/agent/`.

`RULES` is the table later phases extend: each row is a subtree, the module
prefixes forbidden there, and the reason. `ALLOWED_THIRD_PARTY` maps the only
tolerated non-stdlib imports to the subtree they may appear in; they must be
inside a `try:` block so the package imports without them.
"""

import ast
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "readiness"

#: (subtree, forbidden module prefixes, why). A module matches a prefix when it
#: equals it or starts with it followed by a dot.
RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "readiness/engine",
        ("readiness.harness.contract", "readiness.harness.canary", "readiness.contracts"),
        "a model must not see the judge, the canary or the contract it is scored on",
    ),
    (
        "readiness/harness",
        ("readiness.engine", "readiness.agent", "claude_agent_sdk", "anthropic"),
        "the harness judges models and agents; it must not depend on either",
    ),
    (
        "readiness/verify.py",
        ("claude_agent_sdk", "anthropic", "readiness.agent"),
        "verification is what the agent is checked against; no LLM client",
    ),
    (
        "readiness/backtest.py",
        ("claude_agent_sdk", "anthropic", "readiness.agent",
         "readiness.harness.scoring"),
        "the backtest report is built from committed files; it never scores, "
        "never calls an agent and never an LLM",
    ),
    (
        "readiness/exposure",
        ("readiness.harness.labels", "readiness.harness.scoring", "readiness.agent",
         "claude_agent_sdk", "anthropic"),
        "exposure is a join, never a covariate: it never sees labels, scores, "
        "the agent plane or an LLM client",
    ),
    (
        "readiness/issue.py",
        ("claude_agent_sdk", "anthropic", "readiness.agent.orchestrator",
         "readiness.agent.subagents", "readiness.harness.scoring"),
        "issuance refits a model the ledger already validated and forecasts a "
        "period nobody scored: no LLM client, no agent control flow, and no "
        "scoring, because there is nothing yet to score it against "
        "(`readiness.agent.guard` is the hashing utility that stamps the "
        "harness digest, and is not the agent plane)",
    ),
    (
        "readiness/brief.py",
        ("readiness.harness.labels", "readiness.harness.scoring", "readiness.agent",
         "claude_agent_sdk", "anthropic"),
        "the brief is prose about numbers computed elsewhere: it sees neither "
        "labels, scores, the agent plane nor an LLM client",
    ),
    (
        "readiness/cite.py",
        ("readiness.harness.labels", "readiness.harness.scoring", "readiness.agent",
         "claude_agent_sdk", "anthropic"),
        "the citation validator checks model prose; it must see neither labels, "
        "scores, the agent plane nor an LLM client",
    ),
    (
        "readiness/plans",
        ("readiness.data", "readiness.engine", "readiness.backtest",
         "readiness.harness.features", "readiness.harness.labels",
         "readiness.harness.metrics", "readiness.harness.scoring",
         "readiness.harness.splits",
         "readiness.agent.orchestrator", "readiness.agent.subagents",
         "claude_agent_sdk", "anthropic"),
        "the planning thought-partner reasons over a facility record and the "
        "issued risk layer: it reads no panel, fits and scores nothing, sees no "
        "labels, no splits and no agent control flow, and it never calls an LLM "
        "— the optional drafter lives in readiness/agent/planner.py and is "
        "reached only through an explicit `--drafter claude`, at which point "
        "every sentence it returns still has to pass readiness.cite.validate. "
        "`readiness.harness.ledger` stays allowed: `plans/risk.py` reads the "
        "card an issued file names, and keeps three fields of it",
    ),
)

#: The other direction. Phase 3 sits on top of the phases below it and is
#: never depended on by them: a harness or a connector that imported the
#: planning layer would make the eval plane depend on the product built out of
#: its outputs, and the cycle would be invisible until something moved.
UPWARD_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = tuple(
    (
        subtree,
        ("readiness.plans", "readiness.agent.planner"),
        "the phases below Phase 3 must not depend on the planning layer built "
        "on top of them",
    )
    for subtree in (
        "readiness/harness", "readiness/engine", "readiness/data.py",
        "readiness/connectors", "readiness/cite.py", "readiness/issue.py",
        "readiness/brief.py",
    )
)

#: Modules that may import an optional SDK, and only inside a `try:`. Checked
#: by `TestStdlibOnly`; listed here so a new file under `readiness/agent/`
#: cannot quietly become a second import site.
SDK_IMPORT_SITES: tuple[str, ...] = (
    "readiness/agent/orchestrator.py",
    "readiness/agent/planner.py",
)

#: Non-stdlib, non-package modules tolerated anywhere, and where. Each must be
#: imported inside a `try:` so the package works without the dependency.
ALLOWED_THIRD_PARTY: dict[str, str] = {
    "claude_agent_sdk": "readiness/agent",
}


def python_files(subtree: str) -> list[pathlib.Path]:
    path = REPO_ROOT / subtree
    if path.is_file():
        return [path]
    return sorted(path.rglob("*.py")) if path.is_dir() else []


def imports_of(tree: ast.AST) -> list[tuple[str, bool]]:
    """Every imported module name in `tree`, with whether it sits under a try.

    `from readiness.harness import labels` imports the module
    `readiness.harness.labels`, and recording only `readiness.harness` made
    every rule in `RULES` one import style away from being unenforced — the
    rules are dotted prefixes, and that name matches none of them. So an
    `ImportFrom` contributes the module *and* `module.alias` for every alias:
    `readiness.harness.labels` may be a module or a name inside one, and for
    a boundary table the distinction does not matter.
    """
    found: list[tuple[str, bool]] = []

    def walk(node: ast.AST, guarded: bool) -> None:
        for child in ast.iter_child_nodes(node):
            inner = guarded or isinstance(child, ast.Try)
            if isinstance(child, ast.Import):
                found.extend((alias.name, inner) for alias in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module:
                found.append((child.module, inner))
                found.extend(
                    (f"{child.module}.{alias.name}", inner) for alias in child.names
                )
            walk(child, inner)

    walk(tree, False)
    return found


def matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def top_level(module: str) -> str:
    return module.split(".", 1)[0]


class TestImportRules(unittest.TestCase):
    def test_every_rule_holds(self):
        for subtree, forbidden, why in (*RULES, *UPWARD_RULES):
            for file in python_files(subtree):
                tree = ast.parse(file.read_text(), filename=str(file))
                for module, _ in imports_of(tree):
                    for prefix in forbidden:
                        with self.subTest(file=str(file.relative_to(REPO_ROOT)), imp=module):
                            self.assertFalse(
                                matches(module, prefix),
                                f"{file.relative_to(REPO_ROOT)} imports {module}: {why}",
                            )

    def test_rules_name_existing_paths(self):
        # A rule for a path that does not exist is a rule that never runs.
        for subtree, _, _ in (*RULES, *UPWARD_RULES):
            self.assertTrue((REPO_ROOT / subtree).exists(), f"{subtree} does not exist")

    def test_a_from_import_is_recorded_under_its_own_name(self):
        # The hole this table had: `from readiness.harness import labels` was
        # recorded as `readiness.harness`, which matches no forbidden prefix.
        tree = ast.parse(
            "from readiness.harness import labels, scoring\n"
            "from readiness.agent import orchestrator\n"
            "import readiness.data\n"
        )
        names = [module for module, _guarded in imports_of(tree)]
        for expected in ("readiness.harness", "readiness.harness.labels",
                         "readiness.harness.scoring", "readiness.agent",
                         "readiness.agent.orchestrator", "readiness.data"):
            self.assertIn(expected, names)
        self.assertTrue(matches("readiness.harness.labels", "readiness.harness.labels"))

    def test_the_plans_row_would_catch_the_dotted_and_the_from_form(self):
        forbidden = next(f for subtree, f, _ in RULES if subtree == "readiness/plans")
        for source in (
            "from readiness.harness import labels",
            "from readiness.harness.scoring import score",
            "from readiness.data import panel_path",
            "from readiness.engine.boosting import Boosting",
            "from readiness.harness.metrics import brier",
            "from readiness.harness.features import build",
            "from readiness import backtest",
            "from readiness.agent import orchestrator, subagents",
            "import readiness.harness.splits",
        ):
            with self.subTest(source=source):
                names = [m for m, _g in imports_of(ast.parse(source))]
                self.assertTrue(
                    any(matches(m, prefix) for m in names for prefix in forbidden),
                    source,
                )
        # The ledger is the one harness module a plans object may read.
        names = [m for m, _g in imports_of(
            ast.parse("from readiness.harness.ledger import Ledger")
        )]
        self.assertFalse(
            any(matches(m, prefix) for m in names for prefix in forbidden)
        )


class TestStdlibOnly(unittest.TestCase):
    def test_every_import_is_stdlib_or_the_package(self):
        for file in python_files("readiness"):
            rel = file.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(file.read_text(), filename=str(file))
            for module, guarded in imports_of(tree):
                root = top_level(module)
                with self.subTest(file=rel, imp=module):
                    if root == "readiness" or root in sys.stdlib_module_names:
                        continue
                    self.assertIn(
                        root, ALLOWED_THIRD_PARTY, f"{rel} imports third-party {module}"
                    )
                    where = ALLOWED_THIRD_PARTY[root]
                    self.assertTrue(
                        rel.startswith(where + "/"),
                        f"{rel} imports {module}, allowed only under {where}/",
                    )
                    self.assertTrue(guarded, f"{rel} imports {module} outside a try:")

    def test_the_sdk_is_actually_used_where_allowed(self):
        # If the SDK import moves or disappears, the allow-list should be revisited.
        seen = {
            module
            for file in python_files("readiness/agent")
            for module, _ in imports_of(ast.parse(file.read_text()))
        }
        self.assertIn("claude_agent_sdk", seen)

    def test_the_sdk_is_imported_only_where_the_table_says(self):
        sites = set()
        for file in python_files("readiness"):
            rel = file.relative_to(REPO_ROOT).as_posix()
            for module, guarded in imports_of(ast.parse(file.read_text())):
                if module == "claude_agent_sdk":
                    sites.add(rel)
                    self.assertTrue(guarded, f"{rel} imports the SDK outside a try:")
        self.assertEqual(sorted(sites), sorted(SDK_IMPORT_SITES))


if __name__ == "__main__":
    unittest.main()
