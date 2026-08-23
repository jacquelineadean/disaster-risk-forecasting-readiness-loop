"""The append-only experiment ledger.

Report §5: "an append-only experiment ledger"; report §4: the agent "writes an
experiment card (what changed, why, result)".

Append-only is enforced by hash chaining, not by filesystem permissions: every
card carries the hash of the card before it, so editing or deleting history
breaks the chain and `verify()` says exactly where. That makes tampering
detectable by anyone with a clone, which is the property that matters for a
project asking to be trusted.

The file is JSONL so it diffs cleanly in git and can be read without this code.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import pathlib
from dataclasses import asdict, dataclass, field
from typing import Iterator

GENESIS = "0" * 64


@dataclass
class ExperimentCard:
    """One iteration of gather -> act -> verify -> repeat, written down."""

    experiment_id: str
    timestamp: str
    model: str
    version: str
    split: str
    #: What changed since the previous experiment, in the author's words.
    changed: str
    #: Why the author expected that change to help.
    hypothesis: str
    #: What actually happened.
    outcome: str
    scorecard: dict
    verdict: dict
    canary: dict | None = None
    data_snapshot: dict = field(default_factory=dict)
    contract_digest: str = ""
    prev_hash: str = GENESIS
    card_hash: str = ""

    def payload(self) -> dict:
        """Everything that is hashed — i.e. everything except the hash itself."""
        d = asdict(self)
        d.pop("card_hash")
        return d

    def compute_hash(self) -> str:
        blob = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def seal(self) -> "ExperimentCard":
        self.card_hash = self.compute_hash()
        return self


@dataclass(frozen=True)
class ChainStatus:
    valid: bool
    n_cards: int
    broken_at: int | None = None
    reason: str = ""

    def format(self) -> str:
        if self.valid:
            return f"ledger chain intact: {self.n_cards} card(s)"
        return (
            f"ledger chain BROKEN at card index {self.broken_at}: {self.reason}\n"
            f"  history has been edited or reordered; the scores above this "
            f"point cannot be trusted"
        )


class Ledger:
    """Hash-chained JSONL of experiment cards, anchored against truncation.

    Chaining alone catches edits, reorderings and deletions from the middle —
    but *not* deletion from the end, because any prefix of a hash chain is
    perfectly self-consistent. That is precisely the attack that matters here:
    run ten experiments, delete the nine that failed, present the one that
    passed.

    So the head hash and card count are anchored in a small sidecar file that is
    committed alongside the ledger. Truncating the ledger then contradicts the
    anchor. Forging both is still possible for anyone with write access to the
    repo, but it is now two coordinated edits visible in git history rather than
    one silent `head -n 1`.
    """

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.anchor_path = path.with_suffix(path.suffix + ".anchor.json")

    def _read_anchor(self) -> dict | None:
        if not self.anchor_path.exists():
            return None
        try:
            return json.loads(self.anchor_path.read_text())
        except json.JSONDecodeError:
            return {"corrupt": True}

    def _write_anchor(self, n_cards: int, head: str) -> None:
        self.anchor_path.parent.mkdir(parents=True, exist_ok=True)
        self.anchor_path.write_text(
            json.dumps({"n_cards": n_cards, "head": head}, indent=2) + "\n"
        )

    def __len__(self) -> int:
        return sum(1 for _ in self.read())

    def read(self) -> Iterator[ExperimentCard]:
        if not self.path.exists():
            return iter(())
        def _iter() -> Iterator[ExperimentCard]:
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield ExperimentCard(**json.loads(line))
        return _iter()

    def head(self) -> str:
        """Hash of the most recent card, or the genesis value."""
        last = GENESIS
        for card in self.read():
            last = card.card_hash
        return last

    def next_id(self) -> str:
        return f"exp-{len(self) + 1:04d}"

    def append(self, card: ExperimentCard) -> ExperimentCard:
        """Link `card` to the current head and write it. Never rewrites a line."""
        card.prev_hash = self.head()
        card.seal()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(card.payload() | {"card_hash": card.card_hash},
                                sort_keys=True, separators=(",", ":")) + "\n")
        self._write_anchor(self._count_lines(), card.card_hash)
        return card

    def _count_lines(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open(encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    def verify(self) -> ChainStatus:
        """Walk the chain, check each link and hash, then check the anchor."""
        prev = GENESIS
        n = 0
        for i, card in enumerate(self.read()):
            n = i + 1
            if card.prev_hash != prev:
                return ChainStatus(
                    False,
                    n,
                    i,
                    f"prev_hash {card.prev_hash[:12]}... does not match "
                    f"preceding card {prev[:12]}...",
                )
            recomputed = card.compute_hash()
            if recomputed != card.card_hash:
                return ChainStatus(
                    False,
                    n,
                    i,
                    f"card contents were modified after sealing "
                    f"(stored {card.card_hash[:12]}..., recomputed "
                    f"{recomputed[:12]}...)",
                )
            prev = card.card_hash

        anchor = self._read_anchor()
        if anchor is None:
            # No anchor yet: only acceptable for a ledger that has never been
            # written to. Otherwise the anchor was deleted, which is itself the
            # truncation attack minus one step.
            if n == 0:
                return ChainStatus(True, 0)
            return ChainStatus(
                False, n, n, "anchor file is missing; tail truncation cannot be ruled out"
            )
        if anchor.get("corrupt"):
            return ChainStatus(False, n, n, "anchor file is not valid JSON")
        if anchor.get("n_cards") != n or anchor.get("head") != prev:
            return ChainStatus(
                False,
                n,
                n,
                f"anchor expects {anchor.get('n_cards')} card(s) ending at "
                f"{str(anchor.get('head'))[:12]}..., but the ledger holds {n} "
                f"ending at {prev[:12]}... — cards have been removed from the end",
            )
        return ChainStatus(True, n)

    def summary(self) -> str:
        rows = list(self.read())
        if not rows:
            return "ledger is empty"
        # Width the model column to the widest entry, so a long name cannot run
        # into the next column and make the table unreadable.
        width = max(
            [len("model")] + [len(f"{c.model}@{c.version}") for c in rows]
        ) + 2
        lines = [
            f"{'id':<10}{'model':<{width}}{'split':<10}{'BSS':>9}{'AUC':>8}  verdict"
        ]
        for c in rows:
            sc = c.scorecard or {}
            passed = (c.verdict or {}).get("passed")
            mark = "PASS" if passed else ("FAIL" if passed is not None else "-")
            if c.canary and c.canary.get("rejected"):
                mark = "REJECTED"
            lines.append(
                f"{c.experiment_id:<10}{c.model + '@' + c.version:<{width}}"
                f"{c.split:<10}{sc.get('brier_skill_score', float('nan')):>+9.4f}"
                f"{sc.get('auc', float('nan')):>8.4f}  {mark}"
            )
        lines.append("")
        lines.append(self.verify().format())
        return "\n".join(lines)


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
