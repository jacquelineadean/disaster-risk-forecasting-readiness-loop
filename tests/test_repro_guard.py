"""The reproducibility guard: what a refactor may not move.

Hard constraint of every phase after Phase 0: the committed ledgers, the
blessed fingerprints and the contract digests stay bit-for-bit valid. The real
NOAA data is not available to a test, so this file pins the same quantities on
the synthetic fixture panels (blessed before any harness edit, in
`tests/expected/synthetic_fingerprints.json`) and re-checks the committed
artefacts themselves: every ledger verifies, every card re-hashes to its stored
hash, every contract digest is the one its fingerprint file records.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import unittest

from readiness import contracts
from readiness.engine import build_model
from readiness.harness import scoring
from readiness.harness.ledger import ExperimentCard, Ledger
from readiness.harness.splits import TrainingView, split_panel
from readiness.verify import REPRO_FIELDS
from tests.fixtures import make_contract, make_panel

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXPECTED = pathlib.Path(__file__).resolve().parent / "expected" / "synthetic_fingerprints.json"

#: The three example contracts and the digests their committed ledgers carry.
COMMITTED_DIGESTS = {
    "inland-flood-la": "477c493035c9c70d",
    "tornado-ok": "827313e1273a5e76",
    "tropical-cyclone-gulf": "d01b00aa1baedc98",
}

FIXTURES = {
    "dense-quarter": ({}, {"n_regions": 12}),
    "rare-month": ({"period": "month"}, {"n_regions": 12, "rare": True}),
}


def rounded_hash(obj) -> str:
    """sha256 of a JSON rendering with every float rounded to 12 places.

    Python 3.12 gave `sum()` compensated summation, so the last ulp of a mean
    can differ between interpreters. The guard is meant to catch changes to
    the arithmetic, not to the interpreter, so it hashes at 12 places.
    """

    def r(x):
        if isinstance(x, float):
            return round(x, 12)
        if isinstance(x, dict):
            return {k: r(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [r(v) for v in x]
        return x

    blob = json.dumps(r(obj), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def fingerprint(contract_kwargs: dict, panel_kwargs: dict) -> dict:
    c = make_contract(**contract_kwargs)
    p = make_panel(contract=c, **panel_kwargs)
    out: dict = {"panel_digest": p.digest(), "units_digest": p.units_digest(), "n_units": len(p)}
    view = TrainingView(split_panel(p, c.splits.train), c.splits.train)
    out["train_digest"] = view.digest
    for name in ("climatology-pooled", "climatology-seasonal", "persistence-last-year"):
        card = scoring.score(build_model(name), p, c, "validate")
        d = {f: getattr(card, f) for f in REPRO_FIELDS}
        d["reliability_bins_rounded_sha256"] = rounded_hash(card.reliability_bins)
        _u, probs, _o = scoring.predictions_for(build_model(name), p, c, "validate")
        d["probs_rounded_sha256"] = rounded_hash(probs)
        out[name] = d
    out["_contract"] = c.digest()
    return out


class TestSyntheticFingerprints(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.blessed = json.loads(EXPECTED.read_text())

    def test_repro_fields_are_frozen(self):
        self.assertEqual(list(REPRO_FIELDS), self.blessed["_repro_fields"])

    def test_synthetic_baselines_reproduce(self):
        for key, (ckw, pkw) in FIXTURES.items():
            with self.subTest(fixture=key):
                observed = fingerprint(ckw, pkw)
                expected = self.blessed[key]
                self.assertEqual(set(observed), set(expected))
                for field, want in expected.items():
                    got = observed[field]
                    if isinstance(want, dict):
                        self.assertEqual(set(got), set(want), field)
                        for k, v in want.items():
                            if isinstance(v, float):
                                self.assertAlmostEqual(got[k], v, places=12, msg=f"{key}/{field}/{k}")
                            else:
                                self.assertEqual(got[k], v, f"{key}/{field}/{k}")
                    else:
                        self.assertEqual(got, want, f"{key}/{field}")


class TestCommittedArtefacts(unittest.TestCase):
    def test_committed_contract_digests_are_stable(self):
        for name, digest in COMMITTED_DIGESTS.items():
            with self.subTest(contract=name):
                self.assertEqual(contracts.load(name).digest(), digest)
                blessed = json.loads((ROOT / "harness_expected" / f"{name}.json").read_text())
                self.assertEqual(blessed["_contract"], digest)

    def test_committed_ledgers_verify(self):
        for name in COMMITTED_DIGESTS:
            with self.subTest(contract=name):
                status = Ledger(ROOT / "experiments" / name / "ledger.jsonl").verify()
                self.assertTrue(status.valid, status.format())
                self.assertEqual(status.n_cards, 4)

    def test_committed_card_hashes_survive_round_trip(self):
        """Adding a field to ExperimentCard would re-hash every committed card."""
        for name in COMMITTED_DIGESTS:
            path = ROOT / "experiments" / name / "ledger.jsonl"
            for line in path.read_text().splitlines():
                raw = json.loads(line)
                card = ExperimentCard(**raw)
                self.assertEqual(card.compute_hash(), raw["card_hash"], f"{name}: {raw['experiment_id']}")
                self.assertEqual(raw["contract_digest"], COMMITTED_DIGESTS[name])

    def test_blessed_fingerprint_key_sets(self):
        for name in COMMITTED_DIGESTS:
            blessed = json.loads((ROOT / "harness_expected" / f"{name}.json").read_text())
            for model in ("climatology-pooled", "climatology-seasonal"):
                self.assertEqual(
                    set(blessed[model]), set(REPRO_FIELDS) | {"reliability_bins_sha256"}, f"{name}/{model}"
                )


if __name__ == "__main__":
    unittest.main()
