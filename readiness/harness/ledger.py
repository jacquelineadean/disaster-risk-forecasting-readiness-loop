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
import os
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

    def record(self) -> dict:
        """Exactly the JSON object one ledger line holds: the payload plus its seal.

        A method rather than a field so that `asdict`, and therefore the hash,
        is untouched; every writer and reader of a card line goes through here
        so the line's shape is defined once.
        """
        return self.payload() | {"card_hash": self.card_hash}

    @property
    def status(self) -> str:
        """`REJECTED`, `PASS` or `FAIL` — the one word every view of a card prints.

        A canary rejection wins over the verdict: a rejected model's scores are
        not believed, so whether they cleared the thresholds is beside the point.
        """
        if self.canary and self.canary.get("rejected"):
            return "REJECTED"
        return "PASS" if (self.verdict or {}).get("passed") else "FAIL"


#: What a broken chain means unless `verify()` can say something more precise.
TAMPERED = (
    "history has been edited or reordered; the scores above this point cannot "
    "be trusted"
)


@dataclass(frozen=True)
class ChainStatus:
    valid: bool
    n_cards: int
    broken_at: int | None = None
    reason: str = ""
    #: The second line of `format()`: what to make of `reason`.
    hint: str = TAMPERED

    def format(self) -> str:
        if self.valid:
            return f"ledger chain intact: {self.n_cards} card(s)"
        return (
            f"ledger chain BROKEN at card index {self.broken_at}: {self.reason}\n"
            f"  {self.hint}"
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
        """Replace the anchor atomically: a reader sees the old one or the new one.

        Written to a sibling temp file and renamed over the anchor, so a crash
        mid-write cannot leave a half-written (unparseable) anchor behind.
        """
        self.anchor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.anchor_path.with_name(self.anchor_path.name + ".tmp")
        try:
            _write_durably(tmp, json.dumps({"n_cards": n_cards, "head": head}, indent=2))
            os.replace(tmp, self.anchor_path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

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
        # The line is on disk before the anchor names it, so a crash between the
        # two leaves an anchor one card behind — a state `verify()` recognises —
        # and never an anchor that promises a card the ledger does not hold.
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(card_line(card))
            fh.flush()
            os.fsync(fh.fileno())
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
        tail_prev = GENESIS
        for i, card in enumerate(self.read()):
            n = i + 1
            tail_prev = prev
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
            return _anchor_disagrees(anchor, n, prev, tail_prev)
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
            lines.append(
                f"{c.experiment_id:<10}{c.model + '@' + c.version:<{width}}"
                f"{c.split:<10}{sc.get('brier_skill_score', float('nan')):>+9.4f}"
                f"{sc.get('auc', float('nan')):>8.4f}  {c.status}"
            )
        lines.append("")
        lines.append(self.verify().format())
        return "\n".join(lines)


def card_line(card: ExperimentCard) -> str:
    """The canonical JSONL line for a card — the bytes `Ledger.append` writes.

    Canonical (sorted keys, no whitespace) so that anyone can re-hash a line
    with its `card_hash` member removed and get `card_hash` back.
    """
    return json.dumps(card.record(), sort_keys=True, separators=(",", ":")) + "\n"


def _write_durably(path: pathlib.Path, text: str) -> None:
    """Write `text` and make sure it has reached the disk before returning."""
    with path.open("w", encoding="utf-8") as fh:
        fh.write(text + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _anchor_disagrees(anchor: dict, n: int, head: str, tail_prev: str) -> ChainStatus:
    """Say what a disagreeing anchor means, without accusing an interrupted append.

    An anchor exactly one card behind a ledger whose chain is intact is what a
    crash between writing the card and updating the anchor leaves behind; it is
    told apart from a truncated ledger, where the anchor promises *more* cards
    than the file holds.
    """
    expected = anchor.get("n_cards")
    disagreement = (
        f"anchor expects {expected} card(s) ending at "
        f"{str(anchor.get('head'))[:12]}..., but the ledger holds {n} ending at "
        f"{head[:12]}..."
    )
    if expected == n - 1 and anchor.get("head") == tail_prev:
        return ChainStatus(
            False, n, n,
            f"{disagreement} — the anchor is one card behind the ledger: the last "
            "append was interrupted after its card was written and before the "
            "anchor was updated",
            hint="the chain itself is intact; the next append re-anchors it",
        )
    if isinstance(expected, int) and expected > n:
        return ChainStatus(
            False, n, n, f"{disagreement} — cards have been removed from the end"
        )
    return ChainStatus(
        False, n, n, f"{disagreement} — the anchor and the ledger disagree"
    )


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
