"""site/assets/sandbox.py `tamper()`: what happens when the ledger is too short
for the requested action.

`tests/test_site.py::TestSandboxModule` already covers `tamper()` against a
four-card ledger, where every action has enough lines to bite. This file
covers the case it does not: a ledger with only one card, where "swap the
lines for exp-0002 and exp-0003" or "delete the line for exp-0003" cannot be
carried out at all. The module is loaded in-process exactly as
`test_site.py` does it, since the sandbox is not a package module and has no
import path of its own.
"""

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from readiness import contracts
from readiness.agent import orchestrator
from tests.fixtures import make_contract
from tests.test_orchestrator import synthetic_dataset
from tests.test_site import _load, ROOT, SITE


class TestTamperOnAShortLedger(unittest.TestCase):
    """One card is not enough for any action but `no-anchor`, `truncate` or `forge`."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.tmp.name)
        cls.contracts_dir = root / "contracts"
        cls.contract = make_contract(name="flood-short")
        cls.contract.save(cls.contracts_dir)
        cls.env = mock.patch.dict(os.environ, {
            "SANDBOX_REPO": str(ROOT),
            "SANDBOX_ROOT": str(root / "sandbox"),
            "READINESS_EXPERIMENTS_DIR": str(root / "sandbox" / "experiments"),
            contracts.CONTRACTS_DIR_ENV: str(cls.contracts_dir),
        })
        cls.env.start()
        cls.cwd = os.getcwd()
        cls.sb = _load("sandbox_tamper_short", SITE / "assets" / "sandbox.py")
        dataset = synthetic_dataset(cls.contract, root)
        cls.sb._DATASETS[cls.contract.digest()] = dataset

        # A one-card ledger: run just the reference model, no canary.
        orchestrator.run_local(
            cls.contract,
            dataset=dataset,
            queue=orchestrator.BASELINE_QUEUE[:1],
            include_canary=False,
            progress=lambda _m: None,
        )

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        cls.env.stop()
        cls.tmp.cleanup()

    def call(self, fn, **args):
        return json.loads(getattr(self.sb, fn)(json.dumps(args)))

    def _ledger_text(self):
        where = self.sb.data_mod.paths(self.contract)
        return where.ledger.read_text(encoding="utf-8")

    def test_one_card_ledger_exists(self):
        state = self.call("ledger_state", contract="flood-short")
        self.assertEqual(state["n_cards"], 1)
        self.assertTrue(state["valid"])

    def test_edit_on_one_card_is_not_applied_and_says_why(self):
        before = self._ledger_text()
        result = self.call("tamper", contract="flood-short", action="edit")
        self.assertFalse(result["applied"])
        self.assertIn("could not", result["description"].lower())
        self.assertIn("1", result["description"])
        self.assertIn("edit", result["description"])
        # The ledger really was not touched.
        self.assertEqual(self._ledger_text(), before)
        # And it is still a valid, unbroken chain — not "valid" as a
        # misleading side effect of a no-op tamper.
        self.assertTrue(result["valid"])

    def test_swap_on_one_card_is_not_applied(self):
        before = self._ledger_text()
        result = self.call("tamper", contract="flood-short", action="swap")
        self.assertFalse(result["applied"])
        self.assertIn("could not", result["description"].lower())
        self.assertEqual(self._ledger_text(), before)

    def test_delete_middle_on_one_card_is_not_applied(self):
        before = self._ledger_text()
        result = self.call("tamper", contract="flood-short", action="delete-middle")
        self.assertFalse(result["applied"])
        self.assertIn("could not", result["description"].lower())
        self.assertEqual(self._ledger_text(), before)

    def test_truncate_still_works_on_one_card(self):
        # truncate/forge only need one line, which a one-card ledger has.
        result = self.call("tamper", contract="flood-short", action="truncate")
        self.assertTrue(result["applied"])
        self.assertFalse(result["valid"])  # the anchor now disagrees
        self.call("restore_ledger", contract="flood-short")

    def test_no_anchor_still_works_on_one_card(self):
        result = self.call("tamper", contract="flood-short", action="no-anchor")
        self.assertTrue(result["applied"])
        self.call("restore_ledger", contract="flood-short")


if __name__ == "__main__":
    unittest.main()
