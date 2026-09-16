"""The experimental loop: gather context -> take action -> verify work -> repeat.

Report §4 maps the Claude Agent SDK's loop onto this problem one-to-one, and
this module is that map made executable. The loop is run *against a contract*:
the same code, the same queue of baselines and the same verification runs for
any registered hazard and geography.

Two backends run the *same* loop:

`local`   deterministic, no model calls, no API key. It walks a fixed queue of
          candidate models. This is what CI runs and what the Phase 0 exit
          criterion ("reproduces the baseline bit-for-bit from a clean clone")
          is checked against — a loop whose control flow depends on an LLM
          cannot have a bit-for-bit criterion.

`claude`  the real thing: a Claude Agent SDK orchestrator that reads the score
          reports, writes experiment cards, and decides what to try next. It
          proposes; the harness below still disposes, and every number on every
          card still comes from `readiness.harness`.

Phase 0 only needs `local` to pass. `claude` is wired here so Phase 1 is a
matter of giving the agent more models to propose, not a rewrite.
"""

from __future__ import annotations

import asyncio
import pathlib
from dataclasses import dataclass, field
from textwrap import indent
from typing import Callable, Sequence

from readiness import data as data_mod
from readiness.agent import guard
from readiness.contracts import Contract, Split
from readiness.engine import build_model
from readiness.harness import contract as contract_mod
from readiness.harness import scoring
from readiness.harness.ledger import ExperimentCard, Ledger, utc_now
from readiness.harness.splits import TouchBudget, get_split

Progress = Callable[[str], None]


@dataclass
class Candidate:
    """One thing the loop intends to try, and why."""

    model: str
    changed: str
    hypothesis: str
    kwargs: dict = field(default_factory=dict)


#: The baseline queue. Deliberately short and deliberately unambitious: the
#: point of Phase 0 is that the plumbing works for any contract, not that the
#: forecast is good.
BASELINE_QUEUE: tuple[Candidate, ...] = (
    Candidate(
        model="climatology-pooled",
        changed="Initial run: the contract's reference forecast, scored against itself.",
        hypothesis=(
            "Scored against itself the reference must produce BSS exactly 0.0 and "
            "AUC exactly 0.5. Any other result means the harness is wrong, not the "
            "model."
        ),
    ),
    Candidate(
        model="climatology-seasonal",
        changed=(
            "Replaced the single pooled rate with a per-region, per-period-of-year "
            "empirical frequency, shrunk toward the scope-wide seasonal rate and "
            "then the pooled rate."
        ),
        hypothesis=(
            "Hazard occurrence is usually seasonal and spatial, so conditioning on "
            "region and period-of-year should add resolution without hurting "
            "reliability — BSS > 0 against the pooled climatology. Falsified if "
            "resolution does not improve, or if reliability degrades beyond the "
            "contract's tolerance in any populated bin."
        ),
    ),
    Candidate(
        model="persistence-last-year",
        changed=(
            "Swapped in a sharp, deliberately uncalibrated forecast: fixed high "
            "probability if the same region-period had an event last year."
        ),
        hypothesis=(
            "Expected to FAIL the reliability clause while looking acceptable on "
            "AUC. Included so the contract is demonstrated rejecting something, "
            "not only accepting things."
        ),
    ),
)

CANARY_CANDIDATE = Candidate(
    model="leaky-oracle",
    changed=(
        "Canary run: a model constructed with direct access to the outcomes it "
        "is scored on."
    ),
    hypothesis=(
        "Phase 0 exit criterion. The harness must REJECT this, regardless of how "
        "good its scores look."
    ),
)


@dataclass
class LoopResult:
    contract: Contract
    cards: list[ExperimentCard]
    dataset: data_mod.Dataset
    passed: list[str]
    failed: list[str]
    rejected: list[str]

    def format(self) -> str:
        lines = [
            f"ran {len(self.cards)} experiment(s) against {self.contract.name}",
            f"  passed contract   {self.passed or '-'}",
            f"  failed contract   {self.failed or '-'}",
            f"  rejected (canary) {self.rejected or '-'}",
        ]
        return "\n".join(lines)


def run_experiment(
    candidate: Candidate,
    dataset: data_mod.Dataset,
    split: Split,
    ledger: Ledger,
    *,
    touch_budget: TouchBudget | None = None,
    progress: Progress = lambda _m: None,
) -> ExperimentCard:
    """One full turn of the loop for one candidate model.

    Order matters and is the whole safety argument: build the model, fit it
    through the training view, predict, score, screen for leakage, judge against
    the contract, then write an immutable card.
    """
    contract = dataset.contract
    panel = dataset.panel
    # Only a registered canary target ever receives the panel; the registry
    # keeps a forecaster from ever being handed its own outcomes.
    model = build_model(candidate.model, canary_panel=panel, **candidate.kwargs)

    if split.name == "test":
        if touch_budget is None:
            raise RuntimeError("scoring against test requires a touch budget")
        touch_budget.check(model.name, model.version)

    progress(f"  fit {model.name}@{model.version} on {contract.splits.train}")
    card_scorecard, report = scoring.screen(model, panel, contract, split)
    verdict = contract_mod.evaluate(card_scorecard, contract)

    if split.name == "test" and touch_budget is not None:
        spent = touch_budget.spend(model.name, model.version)
        progress(f"  test touch {spent}/{contract.test_touch_budget} spent")

    if report.rejected:
        outcome = "REJECTED by leakage canary; contract verdict not honoured"
    elif verdict.passed:
        outcome = "passed the contract"
    else:
        failed = [c.name for c in verdict.checks if not c.passed]
        outcome = f"failed the contract on: {', '.join(failed)}"

    card = ExperimentCard(
        experiment_id=ledger.next_id(),
        timestamp=utc_now(),
        model=model.name,
        version=model.version,
        split=split.name,
        changed=candidate.changed,
        hypothesis=candidate.hypothesis,
        outcome=outcome,
        scorecard=card_scorecard.to_dict(),
        verdict=verdict.to_dict(),
        canary=report.to_dict(),
        data_snapshot=dataset.provenance(),
        contract_digest=contract.digest(),
    )
    return ledger.append(card)


def run_local(
    contract: Contract,
    *,
    split_name: str = "validate",
    queue: Sequence[Candidate] = BASELINE_QUEUE,
    include_canary: bool = True,
    snapshot_dir: pathlib.Path = data_mod.SNAPSHOT_DIR,
    experiments_dir: pathlib.Path | None = None,
    dataset: data_mod.Dataset | None = None,
    progress: Progress = print,
) -> LoopResult:
    """Run the loop for one contract with no language model in the control flow."""
    split = get_split(contract, split_name)
    where = data_mod.paths(contract, experiments_dir=experiments_dir)

    progress("gather context")
    progress(f"  contract: {contract.name} (sha256:{contract.digest()})")
    if dataset is None:
        dataset = data_mod.build(
            contract, snapshot_dir=snapshot_dir, progress=lambda m: progress("  " + m)
        )
    progress(f"  panel: {dataset.panel.summary()}")
    progress(f"  data version: sha256:{dataset.data_version}")

    ledger = Ledger(where.ledger)
    budget = TouchBudget(where.touch_budget, contract.test_touch_budget)

    work = list(queue)
    if include_canary:
        work.append(CANARY_CANDIDATE)

    cards: list[ExperimentCard] = []
    # Tallied by the card's own status, so the loop's summary cannot disagree
    # with the ledger's summary or the dashboard about what a card was.
    tallies: dict[str, list[str]] = {"PASS": [], "FAIL": [], "REJECTED": []}

    for candidate in work:
        progress(f"take action  [{candidate.model}]")
        card = run_experiment(
            candidate, dataset, split, ledger, touch_budget=budget, progress=progress
        )
        cards.append(card)

        progress("verify work")
        sc = card.scorecard
        progress(
            f"  BSS {sc['brier_skill_score']:+.4f}   AUC {sc['auc']:.4f}   "
            f"brier {sc['brier_score']:.6f}"
        )
        progress(f"  -> {card.outcome}")

        tallies[card.status].append(card.model)

    progress("repeat  (queue exhausted)")
    return LoopResult(
        contract, cards, dataset, tallies["PASS"], tallies["FAIL"], tallies["REJECTED"]
    )


# ---------------------------------------------------------------------------
# Claude Agent SDK backend
# ---------------------------------------------------------------------------

#: What the agent is told it may not edit. The first entries are what the
#: guard hashes; the rest are the data and (from Phase 1) feature channels,
#: which decide what the models see rather than how they are judged.
FORBIDDEN_EDITS: tuple[str, ...] = (
    "readiness/harness/",
    "readiness/contracts.py",
    "readiness/config.py",
    "readiness/verify.py",
    "readiness/agent/guard.py",
    "readiness/connectors/",
    "contracts/",
    "any module named `features` (the feature channel, Phase 1)",
)


def system_prompt(contract: Contract) -> str:
    """The orchestrator's standing brief, built from the contract itself.

    The vocabulary (hazard, geography, period, sources) comes from the
    contract's own `describe()` rather than being written here, so the same
    prompt serves a non-US contract the day one is registered.
    """
    c = contract
    forbidden = "\n".join(f"    {path}" for path in FORBIDDEN_EDITS)
    return f"""\
You are the orchestrator of an open-source disaster-risk forecasting loop.

Your cycle is: gather context -> take action -> verify work -> repeat.

You are running against the registered contract `{c.name}`
(sha256:{c.digest()}). Its forecast unit is: at least one damaging {c.hazard}
event in a given region during a given {c.period}, in {c.scope_label}.
The contract, as the harness reads it:

{indent(c.describe(), "    ")}

Read it in full with `readiness contract -c {c.name}` before doing anything.

RULES YOU CANNOT NEGOTIATE:
  * You may propose any model in readiness.engine. You may not modify anything
    under
{forbidden}
    Those files define how you are judged and where your data comes from;
    editing them is not iteration, it is cheating. An integrity guard hashes
    the harness, the contracts and the guard itself before and after the run;
    any change fails the run and discards its cards.
  * You never see holdout labels. Ask for them and the answer is no.
  * The validate split ({c.validate_years[0]}-{c.validate_years[-1]})
    is yours to iterate against. The test split
    ({c.test_years[0]}-{c.test_years[-1]}) may be touched
    {c.test_touch_budget} time(s) per model version, ever.
  * Every experiment gets a card: what you changed, why you expected it to help,
    and what happened — including when it did not help.

Stop when a candidate passes the contract on validate, or when you have run out
of ideas that are worth a card. Say which of the two it was.
"""


def run_claude(
    contract: Contract,
    *,
    split_name: str = "validate",
    max_turns: int = 24,
    progress: Progress = print,
) -> None:
    """Drive the same loop with a Claude Agent SDK orchestrator.

    Requires `pip install 'readiness-loop[agent]'` and an ANTHROPIC_API_KEY.
    The harness is unchanged: the agent's tools call the very same functions the
    local backend calls. Because the agent holds Bash, `readiness.agent.guard`
    hashes the guarded paths before the run and again after; any change raises
    `HarnessTampered` and the run's cards are to be discarded.
    """
    try:
        from claude_agent_sdk import ClaudeAgentOptions, query  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SystemExit(
            "the claude backend needs the Claude Agent SDK:\n"
            "    pip install claude-agent-sdk\n"
            "and an ANTHROPIC_API_KEY in the environment.\n"
            "Phase 0's exit criteria are checked against the deterministic "
            "'local' backend, so you do not need this to verify the build:\n"
            f"    readiness loop -c {contract.name} --backend local\n"
            f"({exc})"
        ) from exc

    from readiness.agent.subagents import subagents_for  # local import: optional path

    options = ClaudeAgentOptions(
        system_prompt=system_prompt(contract),
        max_turns=max_turns,
        allowed_tools=["Read", "Bash", "Glob", "Grep"],
        agents=subagents_for(contract),
    )
    prompt = (
        f"Run the experimental loop for contract '{contract.name}' against the "
        f"'{split_name}' split. Start by reading skills/verification-protocol.md "
        "and skills/experiment-card.md, then use the `readiness` CLI (always with "
        f"`-c {contract.name}`) to inspect the panel and score candidates. Report "
        "the ledger at the end."
    )

    async def _go() -> None:
        async for message in query(prompt=prompt, options=options):
            progress(str(message))

    _run_guarded(lambda: asyncio.run(_go()))


def _run_guarded(run: Callable[[], None], root: pathlib.Path = guard.REPO_ROOT) -> None:
    """Run the agent between two integrity snapshots of the guarded paths.

    The check also runs when the agent raises: a run that crashed halfway can
    still have edited the harness first, and that is the finding that matters
    most, so `HarnessTampered` takes precedence over the agent's own error.
    """
    before = guard.snapshot(root)
    try:
        run()
    finally:
        guard.check(before, root)
