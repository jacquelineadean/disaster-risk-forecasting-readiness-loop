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
        "readiness/cite.py",
        ("readiness.harness.labels", "readiness.harness.scoring", "readiness.agent",
         "claude_agent_sdk", "anthropic"),
        "the citation validator checks model prose; it must see neither labels, "
        "scores, the agent plane nor an LLM client",
    ),
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
    """Every imported module name in `tree`, with whether it sits under a try."""
    found: list[tuple[str, bool]] = []

    def walk(node: ast.AST, guarded: bool) -> None:
        for child in ast.iter_child_nodes(node):
            inner = guarded or isinstance(child, ast.Try)
            if isinstance(child, ast.Import):
                found.extend((alias.name, inner) for alias in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module:
                found.append((child.module, inner))
            walk(child, inner)

    walk(tree, False)
    return found


def matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def top_level(module: str) -> str:
    return module.split(".", 1)[0]


class TestImportRules(unittest.TestCase):
    def test_every_rule_holds(self):
        for subtree, forbidden, why in RULES:
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
        for subtree, _, _ in RULES:
            self.assertTrue((REPO_ROOT / subtree).exists(), f"{subtree} does not exist")


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


if __name__ == "__main__":
    unittest.main()
