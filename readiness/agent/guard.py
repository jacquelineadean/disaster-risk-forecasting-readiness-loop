"""Harness-integrity guard for the agent backends.

The claude backend and its subagents are granted Bash, and Bash is a write
channel: a shell can edit the harness, the contracts and the config that
decide how the agent is judged, whatever the system prompt says. The prompt is
a request; this module is the check. It hashes every file under the guarded
paths before the agent runs and again after, and any difference fails the run.

The guard is itself on the list, because a guard the agent can edit is not a
guard. Paths that do not exist are skipped so the list can name modules a
later phase adds (`readiness/verify.py`) without breaking earlier trees.

Two snapshots bracket the run, but an agent that edits a guarded file, scores
with it and restores it before the run ends would leave the two snapshots
equal. So every experiment card also carries `tree_digest()` of the guarded
*code* as it stood when the card was scored (`data_snapshot.harness_digest`),
and the guard compares each card written during the run with the digest it
took before the run. Edit, run, restore then shows up on the card.
"""

from __future__ import annotations

import hashlib
import pathlib

from readiness.data import REPO_ROOT

#: Repository-relative paths whose bytes decide how the agent is judged and
#: what it is judged on: the harness, the contract machinery, the data plane
#: that builds the labels, the guard itself, the registered contracts and the
#: manifest that pins the data. Cheap to hash, so every card is stamped with
#: their digest. A directory means every file beneath it.
GUARDED_CODE: tuple[str, ...] = (
    "readiness/harness",
    "readiness/contracts.py",
    "readiness/config.py",
    "readiness/verify.py",
    "readiness/data.py",
    "readiness/connectors",
    "readiness/agent/guard.py",
    "contracts",
    "snapshots/manifest.json",
)

#: Everything the before/after snapshots cover: the code above plus the pinned
#: data itself. The extracts are large, so they are hashed twice per run, not
#: once per card; an edit to an extract also moves the panel digest on the
#: card, which `readiness verify` compares with the blessed fingerprints.
GUARDED: tuple[str, ...] = GUARDED_CODE + ("snapshots",)

#: Directory names that hold compiled or editor state, not source. Skipped so
#: an import during the run (which writes `__pycache__`) is not read as tampering.
_SKIP_DIRS = frozenset({"__pycache__"})


class HarnessTampered(RuntimeError):
    """A guarded file changed while the agent was running.

    Carries the sorted list of repository-relative paths that changed, so the
    operator can see what was touched before deciding what to do about it.
    """

    def __init__(self, paths: list[str]) -> None:
        self.paths = list(paths)
        listed = "\n".join(f"  {p}" for p in self.paths)
        super().__init__(
            "the harness changed while the agent was running:\n"
            f"{listed}\n"
            "Every card written by this run is not to be trusted and must not "
            "be committed; restore the guarded files from git before running "
            "again."
        )


def _files_under(path: pathlib.Path) -> list[pathlib.Path]:
    """Every regular file under `path` (or `path` itself), excluding cache dirs."""
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    found = []
    for candidate in sorted(path.rglob("*")):
        if any(part in _SKIP_DIRS for part in candidate.relative_to(path).parts):
            continue
        if candidate.is_file():
            found.append(candidate)
    return found


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(
    root: pathlib.Path = REPO_ROOT, guarded: tuple[str, ...] = GUARDED
) -> dict[str, str]:
    """Map every guarded file (relative to `root`, posix-style) to its sha256.

    Missing guarded paths are skipped rather than failing: the list names
    modules later phases will add, and a snapshot must be takeable on any tree.
    """
    digests: dict[str, str] = {}
    for rel in guarded:
        for file in _files_under(root / rel):
            digests[file.relative_to(root).as_posix()] = _sha256(file)
    return digests


def diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Sorted paths that were changed, added or removed between two snapshots."""
    changed = {p for p in before.keys() & after.keys() if before[p] != after[p]}
    return sorted(changed | (before.keys() ^ after.keys()))


def check(before: dict[str, str], root: pathlib.Path = REPO_ROOT) -> None:
    """Re-snapshot `root` and raise `HarnessTampered` if anything differs."""
    changed = diff(before, snapshot(root))
    if changed:
        raise HarnessTampered(changed)


def tree_digest(
    root: pathlib.Path = REPO_ROOT, guarded: tuple[str, ...] = GUARDED_CODE
) -> str:
    """One short digest of the guarded code, for stamping on a card.

    A card that carries the digest of the harness it was scored under can be
    checked after the fact against the harness that should have been there;
    that is what closes the edit-run-restore gap the two snapshots leave open.
    """
    h = hashlib.sha256()
    for path, digest in sorted(snapshot(root, guarded).items()):
        h.update(f"{path}\t{digest}\n".encode())
    return h.hexdigest()[:16]


def check_cards(cards, expected: str) -> None:
    """Raise `HarnessTampered` for any card not scored under `expected`.

    `cards` are the experiment cards written during a guarded run. A card with
    no digest at all is treated as tampered too: the orchestrator always
    stamps one, so its absence means the card was not written by it.
    """
    bad = []
    for card in cards:
        seen = (card.data_snapshot or {}).get("harness_digest")
        if seen != expected:
            bad.append(
                f"ledger card {card.experiment_id}: scored under harness "
                f"{seen or 'unknown'}, expected {expected}"
            )
    if bad:
        raise HarnessTampered(bad)
