"""Subagent definitions.

Report §4:

    Subagents are the scaling mechanism. Risk work is embarrassingly parallel:
    one hazard-analyst subagent per peril [...] each with its own context
    window, returning only fitted parameters and scores to the orchestrator. A
    separate calibration-critic subagent reviews experiment cards and proposes
    the next iteration; a data-steward subagent watches for schema drift and
    stale sources.

A loop runs against one contract, so it instantiates one hazard analyst — for
that contract's hazard. Phase 2 ("parallelize with subagents: hurricane wind,
wildfire, tornado, heat") is a loop over registered contracts, not a redesign.

Note the tool grants. No subagent has Write or Edit. The agent plane reads the
harness and never writes to it.
"""

from __future__ import annotations

from readiness.config import HAZARDS
from readiness.contracts import Contract

READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "Bash"]


def hazard_analyst(hazard: str, contract_name: str | None = None) -> dict:
    """One peril, one context window, returns fitted parameters and scores only."""
    which = f" (contract `{contract_name}`)" if contract_name else ""
    return {
        "description": (
            f"Fits and scores candidate {hazard} risk models against the locked "
            "harness. Returns only model parameters and scorecard numbers."
        ),
        "prompt": (
            f"You are the {hazard} hazard analyst{which}.\n\n"
            f"Propose candidate models for P(at least one damaging {hazard} event "
            "| region, period), fit them through the harness, and report back.\n\n"
            "Return ONLY: the model name, its parameters, and the scorecard "
            "numbers (Brier, BSS, AUC, reliability bins). Do not return prose "
            "about your process — the orchestrator has a limited context window "
            "and yours is isolated precisely so it can stay that way.\n\n"
            "You may not read or modify anything under readiness/harness/ except "
            "to call it, and you may not edit any contract. You will never be "
            "given holdout labels; do not ask."
        ),
        "tools": READ_ONLY_TOOLS,
    }


CALIBRATION_CRITIC = {
    "description": (
        "Reviews the experiment ledger and proposes the next iteration. Does not "
        "fit models."
    ),
    "prompt": (
        "You are the calibration critic.\n\n"
        "Read the contract's ledger (experiments/<contract>/ledger.jsonl) and the "
        "most recent score reports. Your job is to answer one question: given "
        "what has been tried, what is the single most informative next "
        "experiment?\n\n"
        "Look specifically for:\n"
        "  * reliability failures that are concentrated in particular bins "
        "(systematic over- or under-forecasting, not noise)\n"
        "  * models that gained AUC while losing calibration — usually a sign of "
        "a sharp feature that needs shrinking, not discarding\n"
        "  * experiments whose stated hypothesis was not actually tested by the "
        "change that was made\n"
        "  * any run whose BSS improved suspiciously fast; say so plainly, and "
        "check whether the canary findings were merely 'clear' or actually "
        "verified the training digest\n\n"
        "Propose one experiment. State what to change, why you expect it to help, "
        "and what result would falsify your expectation. If the honest answer is "
        "'nothing left worth trying', say that instead of inventing work."
    ),
    "tools": ["Read", "Grep", "Glob"],
}

DATA_STEWARD = {
    "description": (
        "Watches for schema drift, stale sources, and upstream data that has "
        "moved or been retired."
    ),
    "prompt": (
        "You are the data steward.\n\n"
        "Check snapshots/manifest.json against the live sources. Report:\n"
        "  * any pinned file whose upstream sha256 has changed (the manifest "
        "records this as a note; experiments run before the change are not "
        "comparable to those run after)\n"
        "  * any source that has stopped updating, moved, or been retired\n"
        "  * any Storm Events column this project reads that has disappeared or "
        "changed meaning\n\n"
        "Report §7 is the standing brief here: NOAA retired the Billion-Dollar "
        "Disasters product in May 2025 with no updates beyond CY2024. Core "
        "series can stop. Your job is that we find out from you rather than from "
        "a silently wrong number.\n\n"
        "Do not modify snapshots. Report and stop."
    ),
    "tools": ["Read", "Grep", "Glob", "WebFetch"],
}


def subagents_for(contract: Contract) -> dict[str, dict]:
    """The subagents a loop against one contract gets."""
    key = f"hazard-analyst-{contract.hazard.replace('_', '-')}"
    return {
        key: hazard_analyst(contract.hazard, contract.name),
        "calibration-critic": CALIBRATION_CRITIC,
        "data-steward": DATA_STEWARD,
    }


def all_hazard_analysts() -> dict[str, dict]:
    """Phase 2: one analyst per catalogued peril, run in parallel against one harness."""
    return {f"hazard-analyst-{h.replace('_', '-')}": hazard_analyst(h) for h in HAZARDS}
