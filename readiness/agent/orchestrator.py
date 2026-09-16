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
import json
import pathlib
import time
from dataclasses import dataclass, field
from textwrap import indent
from typing import Callable, Sequence

from readiness import data as data_mod
from readiness.agent import guard
from readiness.contracts import Contract, Split
from readiness.engine import REGISTRY, build_model
from readiness.engine.features import FEATURE_SETS
from readiness.harness import contract as contract_mod
from readiness.harness import scoring
from readiness.harness.ledger import GENESIS, ExperimentCard, Ledger, utc_now
from readiness.harness.splits import TouchBudget, get_split, split_panel

Progress = Callable[[str], None]


@dataclass
class Candidate:
    """One thing the loop intends to try, and why."""

    model: str
    changed: str
    hypothesis: str
    kwargs: dict = field(default_factory=dict)

    @property
    def requires(self) -> tuple[str, ...]:
        """The feature sets this candidate asks the harness for.

        Read from its own `feature_sets` argument when it gives one, else
        from the registry's default for a model that needs features, so the
        loop can tell before fitting whether the loaded sources can serve it.
        """
        if "feature_sets" in self.kwargs:
            return tuple(self.kwargs["feature_sets"])
        spec = REGISTRY.get(self.model)
        if spec is None or not spec.needs_features:
            return ()
        return tuple(spec.params["feature_sets"]["default"])

    @property
    def label(self) -> str:
        """`model` alone, or `model[set+set]` when the candidate names its sets.

        Three `logistic` entries in one queue would otherwise be told apart
        only by reading their cards.
        """
        if "feature_sets" not in self.kwargs:
            return self.model
        sets = "+".join(self.requires) or "history-only"
        return f"{self.model}[{sets}]"


def sources_for(feature_sets: Sequence[str]) -> tuple[str, ...]:
    """The source names the named feature sets draw on, from the engine's catalogue."""
    names = {spec.source for name in feature_sets for spec in FEATURE_SETS[name]}
    return tuple(sorted(names))


def missing_sources(candidate: Candidate, dataset: data_mod.Dataset) -> tuple[str, ...]:
    """Sources the candidate's feature sets need that the dataset did not load."""
    return tuple(s for s in sources_for(candidate.requires) if s not in dataset.sources)


def json_kwargs(kwargs: dict) -> dict:
    """Constructor arguments as they are written on a card: JSON, tuples as lists."""
    return json.loads(json.dumps(kwargs, sort_keys=True))


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


#: The Phase 1 queue, run after the baselines. Each step changes one thing
#: against the step before it, so a card's outcome can be read as evidence
#: about that one change. Feature sets are named explicitly on every card so
#: the same queue is legible in the ledger without the registry's defaults.
PHASE1_QUEUE: tuple[Candidate, ...] = (
    Candidate(
        model="logistic",
        kwargs={"feature_sets": []},
        changed=(
            "Replaced the seasonal climatology's lookup with a logistic regression "
            "on one column: the leave-one-year-out shrunk seasonal-rate logit "
            "computed inside fit() from training labels. No harness features yet."
        ),
        hypothesis=(
            "A fitted slope and intercept on the history logit should reproduce the "
            "seasonal climatology's ranking (AUC unchanged) and, because the "
            "training rows see a leave-one-year-out rate, temper its over-confidence "
            "in sparse cells: reliability no worse, BSS no lower. Falsified if the "
            "history-only model scores below climatology-seasonal on BSS, which "
            "would mean the LOYO shrinkage is throwing away signal, not noise."
        ),
    ),
    Candidate(
        model="logistic",
        kwargs={"feature_sets": ["era5-antecedent"]},
        changed=(
            "Added the ERA5 antecedent set: trailing 1-, 3- and 12-month "
            "precipitation, the ten-year same-period mean and the trailing "
            "three-month temperature, every column cut one month before the period "
            "by the harness and audited under poisoning."
        ),
        hypothesis=(
            "Antecedent wetness conditions the hazard: a saturated catchment or an "
            "anomalously wet season should raise the odds above the seasonal rate. "
            "Expected to add resolution (BSS up, AUC up) over the history-only "
            "model. Falsified if AUC does not move, which would say a one-month lag "
            "leaves nothing of the antecedent signal for this period length."
        ),
    ),
    Candidate(
        model="logistic",
        kwargs={"feature_sets": ["era5-antecedent", "terrain"]},
        changed=(
            "Added the terrain set on top of ERA5: elevation, water share and "
            "latitude from the timeless geometry sources, the two static layers "
            "the firewall admits without a vintage year."
        ),
        hypothesis=(
            "Low, wet, coastal counties flood more often than high, dry inland ones, "
            "and the history logit only knows that for cells with enough events. "
            "Terrain should lift resolution where history is thin. Falsified if the "
            "static columns add nothing over the antecedent model, which would mean "
            "the history feature already carries the geography."
        ),
    ),
    Candidate(
        model="logistic+iso",
        kwargs={"feature_sets": ["era5-antecedent", "terrain"]},
        changed=(
            "Wrapped the ERA5+terrain logistic in an isotonic map fitted on its own "
            "forecasts for the last three training years (early years fit the "
            "model, late years fit the map, then a refit on all of training)."
        ),
        hypothesis=(
            "The uncalibrated logistic is expected to fail the reliability clause "
            "while clearing AUC: a discriminating model is usually over-confident. "
            "A monotone map fitted train-only should pull the populated bins inside "
            "the tolerance band without changing the ranking, so AUC is unchanged "
            "and the reliability check flips. Falsified if the map over-fits the "
            "three late years and reliability on validate gets worse."
        ),
    ),
    Candidate(
        model="gbm",
        kwargs={"feature_sets": ["era5-antecedent", "terrain"]},
        changed=(
            "Swapped the linear learner for histogram gradient boosting on the same "
            "columns: depth-2 trees, 150 rounds, quantile bins, deterministic splits."
        ),
        hypothesis=(
            "If wetness matters only above a threshold, or only in low-lying "
            "counties, a linear model cannot say so and stumps can. Expected to "
            "beat the logistic on AUC and to be less reliable, as boosted margins "
            "usually are. Falsified if the trees do not beat the linear model on "
            "AUC, which would mean the signal is additive and the extra capacity "
            "buys nothing but variance."
        ),
    ),
    Candidate(
        model="gbm+iso",
        kwargs={"feature_sets": ["era5-antecedent", "terrain"]},
        changed=(
            "Wrapped the boosted model in the same train-only isotonic map as the "
            "logistic, so the two calibrated candidates differ only in the learner."
        ),
        hypothesis=(
            "Whatever the boosted model gains in resolution it should keep under a "
            "monotone recalibration, so this is expected to be the best-calibrated "
            "high-AUC candidate. Falsified if it fails where logistic+iso passes, "
            "which would say the boosted forecasts are too coarse for a map fitted "
            "on three years to smooth."
        ),
    ),
)

#: Feature sets every Phase 2 candidate is built on: the Phase 1 winner's
#: columns, so a national card differs from a state card in scope only.
PHASE2_SETS = ["era5-antecedent", "terrain"]

#: The boosting budget a national panel gets. Plan annex §2.1: a national
#: quarterly panel is about 380k units and a monthly one about 1.1M, so the
#: registry's 150 rounds over 32 bins would run for hours per card; 60 rounds
#: over 16 bins keeps a capped GBM to tens of minutes and still lets the trees
#: say something a line cannot.
PHASE2_GBM = {"rounds": 60, "bins": 16}

#: The Phase 2 queue, run after the baselines on every national contract.
#: Four candidates, not six: Phase 1 already showed what each ERA5 and terrain
#: set adds on a state; at national scale a card costs minutes, so the fleet
#: spends them on the four learners the Phase 1 record says are worth a card.
PHASE2_QUEUE: tuple[Candidate, ...] = (
    Candidate(
        model="logistic",
        kwargs={"feature_sets": list(PHASE2_SETS)},
        changed=(
            "Ran the Phase 1 ERA5+terrain logistic unchanged on the national panel: "
            "every county in the country, the same antecedent-precipitation and "
            "terrain columns, the same leave-one-year-out history logit. Only the "
            "scope of the contract changed."
        ),
        hypothesis=(
            "A relationship between antecedent wetness, terrain and damaging events "
            "that held in one state should hold across the country, and thousands "
            "of counties give every populated reliability bin far more rows, so the "
            "linear model is expected to clear BSS > 0 and AUC with less bin noise "
            "than on a state. Falsified if AUC falls to the history-only level, "
            "which would say the state result was a regional fit, not a hazard one."
        ),
    ),
    Candidate(
        model="logistic+iso",
        kwargs={"feature_sets": list(PHASE2_SETS)},
        changed=(
            "Wrapped the national logistic in the train-only isotonic map: the "
            "early training years fit the model, the last three fit the map, then "
            "a refit on all of training. Nothing else changed."
        ),
        hypothesis=(
            "National scale makes the map's job easier, not harder: three years of "
            "every county give the isotonic fit hundreds of thousands of forecasts "
            "to sort, so every populated bin should land inside the tolerance band "
            "with AUC unchanged. Falsified if reliability on validate is worse than "
            "the uncalibrated model's, which would mean the late training years are "
            "not representative of the validate years at national scale."
        ),
    ),
    Candidate(
        model="gbm",
        kwargs={"feature_sets": list(PHASE2_SETS), **PHASE2_GBM},
        changed=(
            "Swapped the linear learner for histogram gradient boosting on the same "
            "columns, capped for the national panel: 60 rounds over 16 quantile bins "
            "instead of the registry's 150 over 32, depth-2 trees, deterministic "
            "splits, no subsampling."
        ),
        hypothesis=(
            "With hundreds of thousands of training rows the trees can afford to "
            "find thresholds and interactions a line cannot, so the capped GBM is "
            "expected to beat the logistic on AUC even at a third of the rounds, "
            "and to be less reliable, as boosted margins usually are. Falsified if "
            "it does not beat the linear model on AUC, which would say the signal "
            "is additive and 60 rounds buy variance, not resolution."
        ),
    ),
    Candidate(
        model="gbm+iso",
        kwargs={"feature_sets": list(PHASE2_SETS), **PHASE2_GBM},
        changed=(
            "Wrapped the capped national GBM in the same train-only isotonic map as "
            "the logistic, so the two calibrated candidates differ only in the "
            "learner and the boosting budget is the one the card before it used."
        ),
        hypothesis=(
            "Whatever resolution the capped trees find should survive a monotone "
            "recalibration, so this is expected to be the best-calibrated high-AUC "
            "candidate on the national panel and the one the fleet promotes. "
            "Falsified if it fails where logistic+iso passes, which would say the "
            "coarse 16-bin forecasts are too lumpy for a map fitted on three years "
            "to smooth."
        ),
    ),
)

#: The queues `readiness loop --queue` and `readiness fleet --queue` can name.
#: Every queue starts with the baselines: the reference and the persistence
#: rejection cost seconds and make a national ledger readable the same way
#: as a state one.
QUEUES: dict[str, tuple[Candidate, ...]] = {
    "baseline": BASELINE_QUEUE,
    "phase1": BASELINE_QUEUE + PHASE1_QUEUE,
    "phase2": BASELINE_QUEUE + PHASE2_QUEUE,
}


class PromotionRefused(RuntimeError):
    """A test touch that would have no validate record behind it, or a second one.

    `result` is the loop that ran before the refusal, when there was one, so a
    caller that refuses to promote can still report the experiments it did
    run instead of losing them to the traceback.
    """

    result: "LoopResult | None" = None


@dataclass
class LoopResult:
    contract: Contract
    cards: list[ExperimentCard]
    dataset: data_mod.Dataset
    passed: list[str]
    failed: list[str]
    rejected: list[str]
    #: Candidates not run because the dataset lacked a source they need.
    skipped: list[str] = field(default_factory=list)
    #: The test card written by `promote=True`, when a candidate earned one.
    promoted: ExperimentCard | None = None

    def format(self) -> str:
        lines = [
            f"ran {len(self.cards)} experiment(s) against {self.contract.name}",
            f"  passed contract   {self.passed or '-'}",
            f"  failed contract   {self.failed or '-'}",
            f"  rejected (canary) {self.rejected or '-'}",
        ]
        if self.skipped:
            lines.append(f"  skipped (sources) {self.skipped}")
        if self.promoted is not None:
            sc = self.promoted.scorecard
            lines.append(
                f"  promoted to test  {self.promoted.model}@{self.promoted.version} "
                f"-> {self.promoted.status} (BSS {sc['brier_skill_score']:+.4f}, "
                f"AUC {sc['auc']:.4f}; card {self.promoted.experiment_id})"
            )
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
        # Build and audit the feature frame *before* paying. It is the same
        # call `scoring` makes on the way to a fit, it takes units and never
        # labels, and it is the one step that can still refuse: a missing
        # source or an unclean audit must not burn the one touch. Everything
        # after this point pays first and scores second, so a run interrupted
        # after scoring cannot be re-run for free.
        scoring._features(
            model,
            contract,
            split_panel(panel, contract.splits.train),
            split_panel(panel, split),
            dataset.sources,
        )
        spent = touch_budget.spend(model.name, model.version)
        progress(f"  test touch {spent}/{contract.test_touch_budget} spent")

    progress(f"  fit {model.name}@{model.version} on {contract.splits.train}")
    started = time.perf_counter()
    card_scorecard, report = scoring.screen(
        model, panel, contract, split, sources=dataset.sources
    )
    # Provenance, not a criterion: the annex budgets a national card in
    # minutes, and the only way to know what a queue costs is to write down
    # what each card cost. Nothing hashes or judges it.
    wall_clock_s = round(time.perf_counter() - started, 3)
    verdict = contract_mod.evaluate(card_scorecard, contract)

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
        # The guarded code as it stood at scoring time, so a guarded agent run
        # can check every card it produced against the harness it began with;
        # and the constructor arguments, so a replay can rebuild the model.
        data_snapshot=dataset.provenance()
        | {
            "harness_digest": guard.tree_digest(),
            "model_kwargs": json_kwargs(candidate.kwargs),
            "wall_clock_s": wall_clock_s,
        },
        contract_digest=contract.digest(),
    )
    return ledger.append(card)


def _same_data(built, validated: ExperimentCard, dataset: data_mod.Dataset) -> None:
    """Refuse a touch earned on one version of the data and spent on another.

    The validate pass is the evidence the touch is bought with; if the panel
    or the feature extracts have been re-pulled since, the evidence is about
    other numbers. Both versions are named so the difference can be looked up
    in the manifest rather than guessed at.
    """
    snapshot = validated.data_snapshot or {}
    stored = snapshot.get("data_version")
    if stored != dataset.data_version:
        raise PromotionRefused(
            f"refusing to promote {built.name}@{built.version}: validate card "
            f"{validated.experiment_id} was scored on data version sha256:{stored}, and "
            f"this dataset is sha256:{dataset.data_version}. Re-score it on validate "
            "against the data in hand; a pass earned on other bytes does not buy the "
            "touch."
        )
    card_features = snapshot.get("feature_version")
    if card_features is None and not dataset.feature_version:
        return
    if (card_features or "") != (dataset.feature_version or ""):
        raise PromotionRefused(
            f"refusing to promote {built.name}@{built.version}: validate card "
            f"{validated.experiment_id} was scored on feature version "
            f"sha256:{card_features or 'none'}, and this dataset is "
            f"sha256:{dataset.feature_version or 'none'}. The features a model is "
            "judged on are part of what earned the pass."
        )


def run_local(
    contract: Contract,
    *,
    split_name: str = "validate",
    queue: Sequence[Candidate] = BASELINE_QUEUE,
    include_canary: bool = True,
    snapshot_dir: pathlib.Path = data_mod.SNAPSHOT_DIR,
    experiments_dir: pathlib.Path | None = None,
    dataset: data_mod.Dataset | None = None,
    features: Sequence[str] = (),
    promote: bool = False,
    progress: Progress = print,
) -> LoopResult:
    """Run the loop for one contract with no language model in the control flow.

    `features` names the feature connectors to load when the dataset is built
    here. A candidate whose feature sets need a source that was not loaded is
    skipped with a progress line rather than a card: a card records an
    experiment, and "could not be run" is not one. With `promote`, the first
    candidate in queue order whose card passed the contract with a clear
    canary is then promoted to the test split, once.
    """
    split = get_split(contract, split_name)
    where = data_mod.paths(contract, experiments_dir=experiments_dir)

    progress("gather context")
    progress(f"  contract: {contract.name} (sha256:{contract.digest()})")
    if dataset is None:
        dataset = data_mod.build(
            contract,
            snapshot_dir=snapshot_dir,
            features=features,
            progress=lambda m: progress("  " + m),
        )
    progress(f"  panel: {dataset.panel.summary()}")
    progress(f"  data version: sha256:{dataset.data_version}")
    if dataset.sources:
        progress(f"  feature sources: {', '.join(sorted(dataset.sources))}")

    ledger = Ledger(where.ledger)
    budget = TouchBudget(where.touch_budget, contract.test_touch_budget)

    work = list(queue)
    if include_canary:
        work.append(CANARY_CANDIDATE)

    cards: list[ExperimentCard] = []
    ran: list[tuple[Candidate, ExperimentCard]] = []
    skipped: list[str] = []
    # Tallied by the card's own status, so the loop's summary cannot disagree
    # with the ledger's summary or the dashboard about what a card was.
    tallies: dict[str, list[str]] = {"PASS": [], "FAIL": [], "REJECTED": []}

    for candidate in work:
        missing = missing_sources(candidate, dataset)
        if missing:
            progress(
                f"skip         [{candidate.label}] needs source(s) {list(missing)} "
                f"for feature set(s) {list(candidate.requires)}; not loaded"
            )
            skipped.append(candidate.label)
            continue
        progress(f"take action  [{candidate.label}]")
        card = run_experiment(
            candidate, dataset, split, ledger, touch_budget=budget, progress=progress
        )
        cards.append(card)
        ran.append((candidate, card))

        progress("verify work")
        sc = card.scorecard
        progress(
            f"  BSS {sc['brier_skill_score']:+.4f}   AUC {sc['auc']:.4f}   "
            f"brier {sc['brier_score']:.6f}"
        )
        progress(f"  -> {card.outcome}")

        tallies[card.status].append(candidate.label)

    progress("repeat  (queue exhausted)")
    result = LoopResult(
        contract, cards, dataset, tallies["PASS"], tallies["FAIL"], tallies["REJECTED"],
        skipped=skipped,
    )
    if promote:
        try:
            result.promoted = _promote_first_pass(
                ran, dataset, experiments_dir=experiments_dir, progress=progress
            )
        except PromotionRefused as exc:
            # The queue ran and its cards are written; only the promotion was
            # refused. Carry the result on the refusal so the caller can still
            # report what the loop found before it says why nothing moved to
            # the test split.
            exc.result = result
            raise
    return result


def _promote_first_pass(
    ran: Sequence[tuple[Candidate, ExperimentCard]],
    dataset: data_mod.Dataset,
    *,
    experiments_dir: pathlib.Path | None,
    progress: Progress,
) -> ExperimentCard | None:
    """Promote the first candidate whose validate card is a PASS, or nobody."""
    for candidate, card in ran:
        if card.status == "PASS" and card.split == "validate":
            progress(
                f"promote      [{candidate.label}] first validate pass in queue order"
            )
            return promote(
                dataset.contract, candidate.model, candidate.kwargs, dataset,
                experiments_dir=experiments_dir, progress=progress,
            )
    progress("promote      nothing to promote: no candidate passed on validate")
    return None


def _validated(
    ledger: Ledger, name: str, version: str, kwargs: dict, digest: str
) -> ExperimentCard | None:
    """The latest validate PASS card for exactly this model, version and arguments."""
    found = None
    for card in ledger.read():
        if (
            card.split == "validate"
            and card.model == name
            and card.version == version
            and card.contract_digest == digest
            and card.data_snapshot.get("model_kwargs") == kwargs
            and card.status == "PASS"
        ):
            found = card
    return found


def _latest_validated(
    ledger: Ledger, name: str, version: str, digest: str
) -> ExperimentCard | None:
    """The latest validate PASS card for this model and version, whatever its arguments."""
    found = None
    for card in ledger.read():
        if (
            card.split == "validate"
            and card.model == name
            and card.version == version
            and card.contract_digest == digest
            and card.status == "PASS"
        ):
            found = card
    return found


def promote(
    contract: Contract,
    model: str,
    kwargs: dict | None,
    dataset: data_mod.Dataset,
    *,
    experiments_dir: pathlib.Path | None = None,
    progress: Progress = lambda _m: None,
) -> ExperimentCard:
    """The one atomic test touch: spend the budget and write the test card together.

    `kwargs` None means "the arguments that earned the pass": the latest
    validate PASS card for this model and version under the current digest is
    read and its own constructor arguments are adopted and printed, so
    `readiness promote logistic` promotes what was actually validated rather
    than the registry's defaults. Given explicitly, they must still match a
    validate card exactly.

    Refused, in order, when: the dataset has not loaded a source the candidate
    needs (a touch spent on a run that cannot build its features is a touch
    spent on nothing); the ledger already holds *any* test card; no validate
    card for exactly this model, version and arguments passed with a clear
    canary; or the data the pass was earned on is not the data in hand.
    Everything else is `run_experiment` as usual, with the budget it needs for
    the test split.
    """
    where = data_mod.paths(contract, experiments_dir=experiments_dir)
    ledger = Ledger(where.ledger)
    digest = contract.digest()
    identity = build_model(model)
    name, version = identity.name, identity.version

    if kwargs is None:
        adopted = _latest_validated(ledger, name, version, digest)
        if adopted is None:
            raise PromotionRefused(
                f"refusing to promote {name}@{version}: no validate card for it passed "
                f"the contract (sha256:{digest}) with a clear canary, so there are no "
                "arguments to adopt. Score it on validate first, or name the arguments "
                "with --param."
            )
        kwargs = adopted.data_snapshot.get("model_kwargs") or {}
        progress(
            f"  arguments        {json.dumps(kwargs, sort_keys=True)} "
            f"(adopted from validate card {adopted.experiment_id})"
        )
    built = build_model(model, **kwargs)
    wanted = json_kwargs(kwargs)

    # What the candidate would ask the harness for, before anything is spent.
    probe = Candidate(model=model, changed="", hypothesis="", kwargs=dict(kwargs))
    missing = missing_sources(probe, dataset)
    if missing:
        raise PromotionRefused(
            f"refusing to promote {built.name}@{built.version}: it needs source(s) "
            f"{list(missing)} for feature set(s) {list(probe.requires)}; load them with "
            "--features and promote again. The touch pays for a scored model, and a run "
            "that cannot build its features scores nothing."
        )

    prior = [c for c in ledger.read() if c.split == "test"]
    if prior:
        first = prior[0]
        under = (
            "this contract"
            if first.contract_digest == digest
            else f"contract sha256:{first.contract_digest}"
        )
        raise PromotionRefused(
            f"refusing to promote {built.name}@{built.version}: the ledger already "
            f"holds a test card ({first.experiment_id}, {first.model}@{first.version}, "
            f"{first.status}, under {under}). The test years are spent once per ledger, "
            "not once per contract version — re-registering the criteria does not make "
            "them untouched again. A new contract name, not a second touch, is the "
            "sanctioned next move."
        )
    validated = _validated(ledger, built.name, built.version, wanted, digest)
    if validated is None:
        raise PromotionRefused(
            f"refusing to promote {built.name}@{built.version} with arguments "
            f"{json.dumps(wanted, sort_keys=True)}: no validate card for exactly this "
            f"model, version and arguments passed the contract (sha256:{digest}) "
            "with a clear canary. Score it on validate first; the test touch is "
            "spent only on a result that was earned there."
        )
    _same_data(built, validated, dataset)

    candidate = Candidate(
        model=model,
        kwargs=dict(kwargs),
        changed=(
            f"Promoted to the test split: the same model, version and arguments as "
            f"validate card {validated.experiment_id}, which passed the contract with "
            "a clear canary. Nothing about the model changed."
        ),
        hypothesis=(
            "The validate result generalises to the untouched test years: BSS above "
            "the contract's minimum, every populated bin within tolerance, AUC at or "
            "above the floor. This is the one touch; whatever it says is the result, "
            "and a failure here is a published card, not a reason to try again."
        ),
    )
    budget = TouchBudget(where.touch_budget, contract.test_touch_budget)
    return run_experiment(
        candidate, dataset, contract.splits.test, ledger,
        touch_budget=budget, progress=progress,
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
    "readiness/data.py",
    "readiness/agent/guard.py",
    "readiness/connectors/",
    "contracts/",
    "snapshots/ (the data is pinned before the run; do not pull or edit it)",
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
    those paths before and after the run, and every card you write is stamped
    with the digest of the harness it was scored under; any change, even one
    restored before the run ends, fails the run and discards its cards.
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

    ledger = Ledger(data_mod.paths(contract).ledger)
    _run_guarded(lambda: asyncio.run(_go()), ledger=ledger)


def run_claude_fleet(
    contracts: Sequence[Contract],
    *,
    split_name: str = "validate",
    max_turns: int = 24,
    progress: Progress = print,
) -> None:
    """`run_claude` for each contract in turn, each with its own subagents.

    Report §4 calls risk work "embarrassingly parallel: one hazard-analyst
    subagent per peril"; plan §3 makes the fleet a loop over registered
    contracts rather than a redesign. Sequential on purpose: every contract
    has its own ledger and touch budget, and the snapshot manifest the data
    plane saves is not safe to write from two runs at once. Not required by
    any exit criterion — the deterministic `readiness.fleet.run_fleet` is —
    and the SDK is imported only when `run_claude` runs.
    """
    for contract in contracts:
        progress(f"fleet        [{contract.name}] claude backend")
        run_claude(
            contract, split_name=split_name, max_turns=max_turns, progress=progress
        )


def _run_guarded(
    run: Callable[[], None],
    root: pathlib.Path = guard.REPO_ROOT,
    ledger: Ledger | None = None,
) -> None:
    """Run the agent between two integrity snapshots of the guarded paths.

    The check also runs when the agent raises: a run that crashed halfway can
    still have edited the harness first, and that is the finding that matters
    most, so `HarnessTampered` takes precedence over the agent's own error.

    With a `ledger`, every card appended during the run must carry the digest
    of the guarded code taken before it started; a card scored under an edited
    and then restored harness carries a different one and fails the run.
    """
    before = guard.snapshot(root)
    expected = guard.tree_digest(root)
    n_before = len(ledger) if ledger is not None else 0
    head_before = ledger.head() if ledger is not None else None
    try:
        run()
    finally:
        guard.check(before, root)
        if ledger is not None:
            cards = list(ledger.read())
            # The cards that existed before the run must still be there,
            # unchanged and in place: a run that emptied the ledger and wrote
            # a fresh one would otherwise slip past the positional slice.
            intact = (
                len(cards) >= n_before
                and (cards[n_before - 1].card_hash if n_before else GENESIS) == head_before
                and ledger.verify().valid
            )
            if not intact:
                raise guard.HarnessTampered(
                    ["ledger: the cards that existed before the run were rewritten, "
                     "reordered or removed"]
                )
            guard.check_cards(cards[n_before:], expected)
