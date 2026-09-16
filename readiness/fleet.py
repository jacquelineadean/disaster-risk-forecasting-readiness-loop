"""The fleet: the same loop, run over every registered contract in turn.

Plan §3: "Six national contracts registered as data; `readiness fleet` runs
them sequentially through the Phase 1 queue; per-contract ledgers and
budgets." Report §4 calls this work "embarrassingly parallel", and the design
annex says why the fleet is nonetheless sequential: every contract owns its
ledger and its touch budget, and the snapshot manifest the data plane saves
after each pull is not safe to write from two runs at once. Parallel
execution is deferred, not forgotten.

Two things live here and nothing else. `run_fleet` walks the contracts and
calls `orchestrator.run_local` for each, continuing past a contract whose
data is missing so that one unpulled extract does not cost the other five
their ledgers. `status` reads the fleet's record back — ledger, touch file
and backtest report only, through `readiness.verify.phase1` — so the table
it prints is the same evidence `readiness verify --phase 1` would accept,
computed without building a panel or fitting anything.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from readiness import contracts as contracts_mod
from readiness import data as data_mod
from readiness import verify
from readiness.agent import orchestrator
from readiness.agent.orchestrator import Candidate, LoopResult
from readiness.contracts import Contract
from readiness.harness.ledger import ExperimentCard, Ledger

Progress = Callable[[str], None]
Features = Sequence[str] | Callable[[Contract], Sequence[str]]


@dataclass(frozen=True)
class ContractStatus:
    """One row of `readiness fleet --status`, read from the committed record.

    `validate_passes` names the model versions with a passing validate card
    under the current contract digest; `test_card`, `promoted_model` and
    `test_bss` describe the one test card when there is exactly one; and
    `phase1_ok` is `verify.phase1` verbatim, with its first failure (or the
    passing summary) in `phase1_detail`.
    """

    name: str
    hazard: str
    scope_key: str
    period: str
    n_cards: int
    validate_passes: list[str]
    test_card: str | None
    promoted_model: str | None
    test_bss: float | None
    phase1_ok: bool
    phase1_detail: str


def _label(card: ExperimentCard) -> str:
    return f"{card.model}@{card.version}"


def _validate_passes(cards: Sequence[ExperimentCard], contract: Contract) -> list[str]:
    """Model versions that passed on validate under this digest, first seen first."""
    digest = contract.digest()
    seen: list[str] = []
    for card in cards:
        if (
            card.split == "validate"
            and card.contract_digest == digest
            and card.status == "PASS"
            and _label(card) not in seen
        ):
            seen.append(_label(card))
    return seen


def contract_status(
    contract: Contract, *, experiments_dir: pathlib.Path | None = None
) -> ContractStatus:
    """The status of one contract, from its ledger, touch file and backtest only."""
    where = data_mod.paths(contract, experiments_dir=experiments_dir)
    cards = list(Ledger(where.ledger).read())
    result = verify.phase1(
        contract,
        ledger_path=where.ledger,
        touch_path=where.touch_budget,
        backtest_path=where.directory / "backtest.html",
    )
    card = result.card
    failures = result.failures()
    return ContractStatus(
        name=contract.name,
        hazard=contract.hazard,
        scope_key=contract.scope_key,
        period=contract.period,
        n_cards=len(cards),
        validate_passes=_validate_passes(cards, contract),
        test_card=card.experiment_id if card else None,
        promoted_model=_label(card) if card else None,
        test_bss=card.scorecard.get("brier_skill_score") if card else None,
        phase1_ok=result.passed,
        phase1_detail=failures[0] if failures else "Phase 1 exit criteria met",
    )


def status(
    registry: Mapping[str, Contract] | None = None,
    experiments_dir: pathlib.Path | None = None,
) -> list[ContractStatus]:
    """Every contract's status, by name. `registry` defaults to the registered set."""
    known = contracts_mod.registered() if registry is None else registry
    return [
        contract_status(known[name], experiments_dir=experiments_dir)
        for name in sorted(known)
    ]


def national(registry: Mapping[str, Contract]) -> dict[str, Contract]:
    """The contracts whose scope is the whole country (`states: []`)."""
    return {name: c for name, c in registry.items() if not c.states}


def format_status(rows: Sequence[ContractStatus]) -> str:
    """A text table, one contract per row; every row rendered, passing or not."""
    if not rows:
        return "no contracts registered"
    header = ("contract", "hazard", "scope", "period", "cards", "validate pass",
              "test card", "test BSS", "phase 1")
    body = [
        (
            r.name, r.hazard, r.scope_key, r.period, str(r.n_cards),
            ", ".join(r.validate_passes) or "-",
            f"{r.test_card} {r.promoted_model}" if r.test_card else "-",
            f"{r.test_bss:+.4f}" if r.test_bss is not None else "-",
            "ok" if r.phase1_ok else f"NOT met: {r.phase1_detail}",
        )
        for r in rows
    ]
    widths = [
        max(len(line[i]) for line in [header, *body]) for i in range(len(header) - 1)
    ]
    lines = []
    for line in [header, *body]:
        cells = [cell.ljust(widths[i]) for i, cell in enumerate(line[:-1])]
        lines.append("  " + "  ".join(cells + [line[-1]]).rstrip())
    return "\n".join(lines)


def _queue(queue: str | Sequence[Candidate]) -> Sequence[Candidate]:
    if isinstance(queue, str):
        try:
            return orchestrator.QUEUES[queue]
        except KeyError:
            raise ValueError(
                f"unknown queue {queue!r}; known: {sorted(orchestrator.QUEUES)}"
            ) from None
    return queue


def _features_of(features, contract: Contract) -> Sequence[str]:
    """The connectors one contract loads: a fixed list, or a rule of the contract."""
    return features(contract) if callable(features) else features


def run_fleet(
    contracts: Sequence[Contract],
    *,
    queue: str | Sequence[Candidate] = "phase2",
    features: Features = (),
    promote: bool = False,
    experiments_dir: pathlib.Path | None = None,
    progress: Progress = print,
    snapshot_dir: pathlib.Path = data_mod.SNAPSHOT_DIR,
    dataset_for: Callable[[Contract], data_mod.Dataset] | None = None,
) -> dict[str, LoopResult | Exception]:
    """Run the local loop for each contract in turn; never stop the fleet early.

    Sequential by design (see the module docstring). A contract whose data
    cannot be built — an extract not pinned, a state not in the county file,
    anything the data plane refuses — is recorded in the result map as the
    exception it raised and the fleet moves on: six contracts were registered
    so that four can pass, and one missing extract must not decide that for
    the other five. `features` is a list of connector names or a callable of
    the contract, so the CLI can load, per contract, whatever is pinned.
    `dataset_for` is an injection seam for tests; left out, the loop builds
    each dataset from the snapshots.
    """
    work = _queue(queue)
    results: dict[str, LoopResult | Exception] = {}
    for i, contract in enumerate(contracts, 1):
        progress(f"fleet        [{contract.name}] {i}/{len(contracts)}")
        try:
            results[contract.name] = orchestrator.run_local(
                contract,
                queue=work,
                features=_features_of(features, contract),
                promote=promote,
                experiments_dir=experiments_dir,
                snapshot_dir=snapshot_dir,
                dataset=dataset_for(contract) if dataset_for else None,
                progress=lambda m: progress("  " + m),
            )
        except Exception as exc:  # recorded and reported, not swallowed
            progress(f"fleet        [{contract.name}] not run: {exc}")
            results[contract.name] = exc
    return results


def format_results(results: Mapping[str, LoopResult | Exception]) -> str:
    """One block per contract: the loop's own summary, or the error that stopped it."""
    blocks = []
    for name, result in results.items():
        if isinstance(result, Exception):
            blocks.append(f"{name}: not run ({type(result).__name__}: {result})")
        else:
            blocks.append(result.format())
    return "\n\n".join(blocks)
