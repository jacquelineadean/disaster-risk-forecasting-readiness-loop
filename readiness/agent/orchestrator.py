"""The experimental loop: gather context -> take action -> verify work -> repeat.

Report §4 maps the Claude Agent SDK's loop onto this problem one-to-one, and
this module is that map made executable.

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

import pathlib
from dataclasses import dataclass, field
from typing import Callable, Sequence

from readiness import data as data_mod
from readiness.config import CONTRACT
from readiness.engine import build_model
from readiness.harness import canary as canary_mod
from readiness.harness import contract as contract_mod
from readiness.harness import scoring
from readiness.harness.labels import Panel
from readiness.harness.ledger import ExperimentCard, Ledger, utc_now
from readiness.harness.splits import (
    TRAIN,
    Split,
    TouchBudget,
    TrainingView,
    get_split,
    split_panel,
)

Progress = Callable[[str], None]


@dataclass
class Candidate:
    """One thing the loop intends to try, and why."""

    model: str
    changed: str
    hypothesis: str
    kwargs: dict = field(default_factory=dict)


#: Phase 0's queue. Deliberately short and deliberately unambitious: the point
#: of Phase 0 is that the plumbing works, not that the forecast is good.
PHASE0_QUEUE: tuple[Candidate, ...] = (
    Candidate(
        model="climatology-global",
        changed="Initial run: the contract's reference forecast, scored against itself.",
        hypothesis=(
            "Scored against itself the reference must produce BSS exactly 0.0 and "
            "AUC exactly 0.5. Any other result means the harness is wrong, not the "
            "model."
        ),
    ),
    Candidate(
        model="climatology-county-quarter",
        changed=(
            "Replaced the single global rate with a per-county, per-quarter "
            "empirical frequency, shrunk toward state-quarter and global rates."
        ),
        hypothesis=(
            "Flood risk in this state is strongly seasonal and strongly spatial, "
            "so conditioning on county and quarter should add resolution without "
            "hurting reliability — BSS > 0 against global climatology."
        ),
    ),
    Candidate(
        model="persistence-last-year",
        changed=(
            "Swapped in a sharp, deliberately uncalibrated forecast: fixed high "
            "probability if the same county-quarter had an event last year."
        ),
        hypothesis=(
            "Expected to FAIL the reliability clause while looking acceptable on "
            "AUC. Included so the contract is demonstrated rejecting something, "
            "not only accepting things."
        ),
    ),
)


@dataclass
class LoopResult:
    cards: list[ExperimentCard]
    dataset: data_mod.Dataset
    passed: list[str]
    failed: list[str]
    rejected: list[str]

    def format(self) -> str:
        lines = [
            f"ran {len(self.cards)} experiment(s)",
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
    panel = dataset.panel
    needs_panel = candidate.model == "leaky-oracle"
    model = build_model(
        candidate.model,
        panel=panel if needs_panel else None,
        **candidate.kwargs,
    )

    if split.name == "test":
        if touch_budget is None:
            raise RuntimeError("scoring against test requires a touch budget")
        touch_budget.check(model.name, model.version)

    progress(f"  fit {model.name}@{model.version} on {TRAIN}")
    card_scorecard = scoring.score(model, panel, split)

    # Re-run the fit/predict path to obtain the raw arrays the canary needs.
    # Same inputs, same code path, so this cannot disagree with the scorecard.
    _units, probs, outcomes = scoring.predictions_for(model, panel, split)
    view = TrainingView(split_panel(panel, TRAIN), TRAIN)
    report = canary_mod.run(
        probs=probs,
        outcomes=outcomes,
        brier_skill_score=card_scorecard.brier_skill_score,
        auc=card_scorecard.auc,
        view=view,
        declared_train_digest=getattr(model, "training_digest", None),
    )

    verdict = contract_mod.evaluate(card_scorecard)

    if split.name == "test" and touch_budget is not None:
        spent = touch_budget.spend(model.name, model.version)
        progress(f"  test touch {spent}/{CONTRACT.test_touch_budget} spent")

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
        contract_digest=CONTRACT.digest(),
    )
    return ledger.append(card)


def run_local(
    *,
    split_name: str = "validate",
    queue: Sequence[Candidate] = PHASE0_QUEUE,
    include_canary: bool = True,
    snapshot_dir: pathlib.Path = data_mod.SNAPSHOT_DIR,
    ledger_path: pathlib.Path = data_mod.LEDGER_PATH,
    progress: Progress = print,
) -> LoopResult:
    """Run the Phase 0 loop with no language model in the control flow."""
    split = get_split(split_name)

    progress("gather context")
    dataset = data_mod.build(snapshot_dir=snapshot_dir, progress=lambda m: progress("  " + m))
    progress(f"  panel: {dataset.panel.summary()}")
    progress(f"  data version: sha256:{dataset.data_version}")

    ledger = Ledger(ledger_path)
    budget = TouchBudget(data_mod.TOUCH_BUDGET_PATH)

    work = list(queue)
    if include_canary:
        work.append(
            Candidate(
                model="leaky-oracle",
                changed=(
                    "Canary run: a model constructed with direct access to the "
                    "outcomes it is scored on."
                ),
                hypothesis=(
                    "Phase 0 exit criterion. The harness must REJECT this, "
                    "regardless of how good its scores look."
                ),
            )
        )

    cards: list[ExperimentCard] = []
    passed: list[str] = []
    failed: list[str] = []
    rejected: list[str] = []

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

        if card.canary and card.canary["rejected"]:
            rejected.append(card.model)
        elif card.verdict["passed"]:
            passed.append(card.model)
        else:
            failed.append(card.model)

    progress("repeat  (queue exhausted)")
    return LoopResult(cards, dataset, passed, failed, rejected)


# ---------------------------------------------------------------------------
# Claude Agent SDK backend
# ---------------------------------------------------------------------------

AGENT_SYSTEM_PROMPT = f"""\
You are the orchestrator of an open-source disaster-risk forecasting loop.

Your cycle is: gather context -> take action -> verify work -> repeat.

The forecast unit is: at least one damaging {CONTRACT.hazard} event in a given
county during a given quarter, in {CONTRACT.state}.

RULES YOU CANNOT NEGOTIATE:
  * You may propose any model in readiness.engine. You may not modify anything
    under readiness/harness/ or readiness/config.py. Those files define how you
    are judged; editing them is not iteration, it is cheating.
  * You never see holdout labels. Ask for them and the answer is no.
  * The validate split ({CONTRACT.validate_years[0]}-{CONTRACT.validate_years[-1]})
    is yours to iterate against. The test split
    ({CONTRACT.test_years[0]}-{CONTRACT.test_years[-1]}) may be touched
    {CONTRACT.test_touch_budget} time per model version, ever.
  * Every experiment gets a card: what you changed, why you expected it to help,
    and what happened — including when it did not help.

Stop when a candidate passes the contract on validate, or when you have run out
of ideas that are worth a card. Say which of the two it was.
"""


def run_claude(
    *,
    split_name: str = "validate",
    max_turns: int = 24,
    progress: Progress = print,
) -> None:
    """Drive the same loop with a Claude Agent SDK orchestrator.

    Requires `pip install 'readiness-loop[agent]'` and an ANTHROPIC_API_KEY.
    The harness is unchanged: the agent's tools call the very same functions the
    local backend calls.
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
            "    readiness loop --backend local\n"
            f"({exc})"
        ) from exc

    from readiness.agent.subagents import SUBAGENTS  # local import: optional path

    options = ClaudeAgentOptions(
        system_prompt=AGENT_SYSTEM_PROMPT,
        max_turns=max_turns,
        allowed_tools=["Read", "Bash", "Glob", "Grep"],
        agents=SUBAGENTS,
    )
    prompt = (
        f"Run the experimental loop against the '{split_name}' split. "
        "Start by reading skills/verification-protocol.md and "
        "skills/experiment-card.md, then use the `readiness` CLI to inspect the "
        "panel and score candidates. Report the ledger at the end."
    )

    import asyncio

    async def _go() -> None:
        async for message in query(prompt=prompt, options=options):
            progress(str(message))

    asyncio.run(_go())
