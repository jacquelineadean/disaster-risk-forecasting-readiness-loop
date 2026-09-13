"""The ledger is append-only because tampering breaks a hash chain.

"Append-only" enforced by convention is a promise. Enforced by chaining it is a
property anyone with a clone can check, which is what a project asking to be
trusted actually needs.
"""

import json
import pathlib
import tempfile
import unittest

from readiness.harness.ledger import GENESIS, ExperimentCard, Ledger, utc_now


def make_card(n: int = 1, **overrides) -> ExperimentCard:
    base = dict(
        experiment_id=f"exp-{n:04d}",
        timestamp=utc_now(),
        model="climatology-pooled",
        version="1.0.0",
        split="validate",
        changed="initial run",
        hypothesis="the reference scored against itself yields zero skill",
        outcome="passed the contract",
        scorecard={"brier_skill_score": 0.0, "auc": 0.5},
        verdict={"passed": True},
    )
    base.update(overrides)
    return ExperimentCard(**base)


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "ledger.jsonl"
        self.ledger = Ledger(self.path)

    def tearDown(self):
        self.tmp.cleanup()


class TestAppend(LedgerTestCase):
    def test_empty_ledger_verifies(self):
        status = self.ledger.verify()
        self.assertTrue(status.valid)
        self.assertEqual(status.n_cards, 0)

    def test_first_card_links_to_genesis(self):
        card = self.ledger.append(make_card(1))
        self.assertEqual(card.prev_hash, GENESIS)
        self.assertTrue(card.card_hash)

    def test_cards_chain(self):
        a = self.ledger.append(make_card(1))
        b = self.ledger.append(make_card(2))
        self.assertEqual(b.prev_hash, a.card_hash)
        self.assertTrue(self.ledger.verify().valid)

    def test_ids_increment(self):
        self.assertEqual(self.ledger.next_id(), "exp-0001")
        self.ledger.append(make_card(1))
        self.assertEqual(self.ledger.next_id(), "exp-0002")

    def test_file_is_jsonl(self):
        self.ledger.append(make_card(1))
        self.ledger.append(make_card(2))
        lines = self.path.read_text().strip().split("\n")
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)  # each line stands alone

    def test_long_chain_verifies(self):
        for i in range(1, 26):
            self.ledger.append(make_card(i))
        status = self.ledger.verify()
        self.assertTrue(status.valid)
        self.assertEqual(status.n_cards, 25)


class TestTamperDetection(LedgerTestCase):
    def setUp(self):
        super().setUp()
        for i in range(1, 5):
            self.ledger.append(make_card(i))

    def _rewrite(self, transform):
        lines = self.path.read_text().strip().split("\n")
        self.path.write_text("\n".join(transform(lines)) + "\n")

    def test_editing_a_score_is_detected(self):
        def transform(lines):
            card = json.loads(lines[1])
            card["scorecard"]["brier_skill_score"] = 0.99  # a flattering edit
            lines[1] = json.dumps(card, sort_keys=True, separators=(",", ":"))
            return lines

        self._rewrite(transform)
        status = self.ledger.verify()
        self.assertFalse(status.valid)
        self.assertEqual(status.broken_at, 1)
        self.assertIn("modified after sealing", status.reason)

    def test_deleting_a_card_is_detected(self):
        self._rewrite(lambda lines: lines[:1] + lines[2:])
        status = self.ledger.verify()
        self.assertFalse(status.valid)
        self.assertIn("does not match", status.reason)

    def test_reordering_cards_is_detected(self):
        self._rewrite(lambda lines: [lines[0], lines[2], lines[1], lines[3]])
        self.assertFalse(self.ledger.verify().valid)

    def test_appending_a_forged_card_is_detected(self):
        forged = make_card(99, scorecard={"brier_skill_score": 0.95, "auc": 0.99})
        forged.prev_hash = "deadbeef" * 8
        forged.seal()
        with self.path.open("a") as fh:
            fh.write(
                json.dumps(
                    forged.payload() | {"card_hash": forged.card_hash},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
        status = self.ledger.verify()
        self.assertFalse(status.valid)
        self.assertEqual(status.broken_at, 4)

    def test_a_failure_cannot_be_quietly_removed(self):
        # The scenario this exists to prevent: an agent deletes the experiments
        # where its model failed and reports only the run that passed.
        #
        # Chaining alone does NOT catch this — any prefix of a hash chain is
        # perfectly self-consistent. The committed anchor is what closes it.
        self._rewrite(lambda lines: [lines[0]])
        status = self.ledger.verify()
        self.assertFalse(status.valid)
        self.assertIn("removed from the end", status.reason)

    def test_truncating_to_nothing_is_detected(self):
        self.path.write_text("")
        self.assertFalse(self.ledger.verify().valid)

    def test_deleting_the_anchor_is_itself_detected(self):
        self.ledger.anchor_path.unlink()
        status = self.ledger.verify()
        self.assertFalse(status.valid)
        self.assertIn("anchor file is missing", status.reason)

    def test_a_corrupt_anchor_is_detected(self):
        self.ledger.anchor_path.write_text("{not json")
        self.assertFalse(self.ledger.verify().valid)

    def test_anchor_tracks_the_head(self):
        cards = list(self.ledger.read())
        anchor = json.loads(self.ledger.anchor_path.read_text())
        self.assertEqual(anchor["n_cards"], len(cards))
        self.assertEqual(anchor["head"], cards[-1].card_hash)


class TestSummary(LedgerTestCase):
    def test_empty(self):
        self.assertEqual(self.ledger.summary(), "ledger is empty")

    def test_summary_includes_chain_status(self):
        self.ledger.append(make_card(1))
        self.assertIn("chain intact", self.ledger.summary())

    def test_summary_flags_a_canary_rejection(self):
        self.ledger.append(
            make_card(1, model="leaky-oracle", canary={"rejected": True})
        )
        self.assertIn("REJECTED", self.ledger.summary())


if __name__ == "__main__":
    unittest.main()
