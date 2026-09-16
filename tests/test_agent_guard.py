"""The harness-integrity guard: a run that edits a guarded file fails.

The claude backend and its subagents hold Bash, which can edit anything the
system prompt says not to. These tests never import the SDK: the guard
functions are exercised on a temporary tree, and the orchestrator's guarded
runner is given a fake agent that edits, adds or removes a guarded file.
"""

import pathlib
import tempfile
import unittest

from readiness.agent import guard, orchestrator
from readiness.harness.ledger import ExperimentCard, Ledger


def make_tree(root: pathlib.Path) -> None:
    """A miniature repository with one file under each guarded path."""
    (root / "readiness" / "harness").mkdir(parents=True)
    (root / "readiness" / "harness" / "metrics.py").write_text("def brier(): ...\n")
    (root / "readiness" / "harness" / "__pycache__").mkdir()
    (root / "readiness" / "harness" / "__pycache__" / "m.pyc").write_bytes(b"\x00")
    (root / "readiness" / "contracts.py").write_text("SPEC = 1\n")
    (root / "readiness" / "config.py").write_text("HAZARDS = ()\n")
    (root / "readiness" / "agent").mkdir()
    (root / "readiness" / "agent" / "guard.py").write_text("GUARDED = ()\n")
    (root / "contracts").mkdir()
    (root / "contracts" / "flood-zz.json").write_text("{}\n")
    (root / "snapshots" / "storm_events").mkdir(parents=True)
    (root / "snapshots" / "manifest.json").write_text('{"records": {}}\n')
    (root / "snapshots" / "storm_events" / "99_2004.jsonl").write_text("{}\n")
    (root / "readiness" / "engine.py").write_text("free = True\n")


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        make_tree(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_covers_every_guarded_file_and_nothing_else(self):
        snap = guard.snapshot(self.root)
        self.assertEqual(
            sorted(snap),
            [
                "contracts/flood-zz.json",
                "readiness/agent/guard.py",
                "readiness/config.py",
                "readiness/contracts.py",
                "readiness/harness/metrics.py",
                "snapshots/manifest.json",
                "snapshots/storm_events/99_2004.jsonl",
            ],
        )
        self.assertNotIn("readiness/engine.py", snap)
        self.assertTrue(all(len(v) == 64 for v in snap.values()))

    def test_missing_guarded_paths_are_skipped(self):
        # readiness/verify.py does not exist in the tree; nothing raises.
        self.assertNotIn("readiness/verify.py", guard.snapshot(self.root))

    def test_pycache_is_ignored(self):
        before = guard.snapshot(self.root)
        (self.root / "readiness" / "harness" / "__pycache__" / "n.pyc").write_bytes(b"1")
        self.assertEqual(guard.diff(before, guard.snapshot(self.root)), [])

    def test_diff_reports_changed_added_and_removed(self):
        before = guard.snapshot(self.root)
        (self.root / "readiness" / "contracts.py").write_text("SPEC = 2\n")
        (self.root / "contracts" / "new.json").write_text("{}\n")
        (self.root / "readiness" / "config.py").unlink()
        after = guard.snapshot(self.root)
        self.assertEqual(
            guard.diff(before, after),
            ["contracts/new.json", "readiness/config.py", "readiness/contracts.py"],
        )

    def test_unchanged_tree_is_clean(self):
        before = guard.snapshot(self.root)
        guard.check(before, self.root)  # does not raise
        self.assertEqual(guard.diff(before, before), [])

    def test_the_real_repository_snapshot_includes_the_guard_itself(self):
        snap = guard.snapshot()
        self.assertIn("readiness/agent/guard.py", snap)
        self.assertIn("readiness/harness/metrics.py", snap)
        self.assertIn("readiness/contracts.py", snap)
        self.assertTrue(any(p.startswith("contracts/") for p in snap))


class TestGuardedRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        make_tree(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_honest_run_passes(self):
        ran = []
        orchestrator._run_guarded(lambda: ran.append(True), root=self.root)
        self.assertEqual(ran, [True])

    def test_a_run_that_edits_the_harness_raises(self):
        def tamper():
            (self.root / "readiness" / "harness" / "metrics.py").write_text("def brier(): return 1\n")

        with self.assertRaises(guard.HarnessTampered) as cm:
            orchestrator._run_guarded(tamper, root=self.root)
        self.assertEqual(cm.exception.paths, ["readiness/harness/metrics.py"])
        self.assertIn("must not be committed", str(cm.exception))
        self.assertIn("readiness/harness/metrics.py", str(cm.exception))

    def test_a_run_that_edits_the_guard_raises(self):
        def tamper():
            (self.root / "readiness" / "agent" / "guard.py").write_text("GUARDED = ('x',)\n")

        with self.assertRaises(guard.HarnessTampered):
            orchestrator._run_guarded(tamper, root=self.root)

    def test_tampering_is_reported_even_when_the_agent_crashes(self):
        def tamper_then_crash():
            (self.root / "contracts" / "flood-zz.json").write_text('{"v": 2}\n')
            raise ValueError("agent fell over")

        with self.assertRaises(guard.HarnessTampered) as cm:
            orchestrator._run_guarded(tamper_then_crash, root=self.root)
        self.assertEqual(cm.exception.paths, ["contracts/flood-zz.json"])
        self.assertIsInstance(cm.exception.__context__, ValueError)

    def test_an_honest_crash_is_the_agents_own_error(self):
        def crash():
            raise ValueError("agent fell over")

        with self.assertRaises(ValueError):
            orchestrator._run_guarded(crash, root=self.root)

    def test_editing_an_unguarded_file_is_allowed(self):
        def edit():
            (self.root / "readiness" / "engine.py").write_text("free = False\n")

        orchestrator._run_guarded(edit, root=self.root)


class TestTreeDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        make_tree(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_is_stable_and_short(self):
        a = guard.tree_digest(self.root)
        self.assertEqual(a, guard.tree_digest(self.root))
        self.assertEqual(len(a), 16)

    def test_moves_when_guarded_code_changes(self):
        before = guard.tree_digest(self.root)
        (self.root / "readiness" / "config.py").write_text("HAZARDS = (1,)\n")
        self.assertNotEqual(before, guard.tree_digest(self.root))

    def test_ignores_the_data_extracts_but_not_the_manifest(self):
        before = guard.tree_digest(self.root)
        (self.root / "snapshots" / "storm_events" / "99_2004.jsonl").write_text('{"x":1}\n')
        self.assertEqual(before, guard.tree_digest(self.root))
        (self.root / "snapshots" / "manifest.json").write_text('{"records": {"a": 1}}\n')
        self.assertNotEqual(before, guard.tree_digest(self.root))


class TestCardCheck(unittest.TestCase):
    """Edit, run, restore leaves the two snapshots equal; the card gives it away."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        make_tree(self.root)
        self.ledger = Ledger(self.root / "experiments" / "flood-zz" / "ledger.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def card(self, digest):
        snapshot = {} if digest is None else {"harness_digest": digest}
        return ExperimentCard(
            experiment_id=self.ledger.next_id(), timestamp="t", model="m", version="1",
            split="validate", changed="", hypothesis="", outcome="",
            scorecard={}, verdict={"passed": False}, data_snapshot=snapshot,
        )

    def test_a_card_scored_under_the_starting_harness_passes(self):
        expected = guard.tree_digest(self.root)
        run = lambda: self.ledger.append(self.card(guard.tree_digest(self.root)))
        orchestrator._run_guarded(run, root=self.root, ledger=self.ledger)
        self.assertEqual(list(self.ledger.read())[0].data_snapshot["harness_digest"], expected)

    def test_edit_run_restore_is_caught_by_the_card(self):
        metrics = self.root / "readiness" / "harness" / "metrics.py"
        original = metrics.read_text()

        def edit_run_restore():
            metrics.write_text("def brier(): return 0\n")
            self.ledger.append(self.card(guard.tree_digest(self.root)))
            metrics.write_text(original)

        with self.assertRaises(guard.HarnessTampered) as cm:
            orchestrator._run_guarded(edit_run_restore, root=self.root, ledger=self.ledger)
        self.assertEqual(len(cm.exception.paths), 1)
        self.assertIn("ledger card exp-0001", cm.exception.paths[0])

    def test_a_card_with_no_digest_is_refused(self):
        run = lambda: self.ledger.append(self.card(None))
        with self.assertRaises(guard.HarnessTampered) as cm:
            orchestrator._run_guarded(run, root=self.root, ledger=self.ledger)
        self.assertIn("unknown", cm.exception.paths[0])

    def test_cards_written_before_the_run_are_not_judged(self):
        self.ledger.append(self.card("stale"))
        orchestrator._run_guarded(lambda: None, root=self.root, ledger=self.ledger)


class TestTamperedError(unittest.TestCase):
    def test_is_a_runtime_error_carrying_the_paths(self):
        exc = guard.HarnessTampered(["b", "a"])
        self.assertIsInstance(exc, RuntimeError)
        self.assertEqual(exc.paths, ["b", "a"])


if __name__ == "__main__":
    unittest.main()
