"""The local drafter: the plan section each finding implies, and nothing else.

Report §6 asks for "draft plan sections grounded in a library of published case
studies"; report §7 requires every sentence to cite a computed number, a
dataset row or a named guidance document. Those two together rule out free
prose, so the local drafter is a template per finding: one sentence saying what
to write into the plan, citing the guidance document that says why and the
facility field the recommendation turns on.

It is deterministic — the same record and the same findings produce the same
bytes — which is what makes the case-study regression tests possible. The
optional `claude` drafter in `readiness/agent/planner.py` may rewrite these
sentences; whatever it returns still has to carry markers from this document's
own claim set and still has to pass `readiness.cite.validate`, and whatever
does not is dropped and counted on the provenance line.

The spelling each template uses for its document ("NFPA 110", "42 CFR 482.15")
is an alias registered in `plans/guidance.json`, so the citation validator
reads its digits as a name rather than as a quantity that needs a claim.
"""

from __future__ import annotations

from typing import Sequence

from readiness import cite
from readiness.plans.facility import Facility
from readiness.plans.rules import Finding, guidance_claim
from readiness.plans.scenarios import Scenario

#: rule -> finding status -> (what to write, guidance id, how prose spells it).
#: The "" status is the fallback when a rule has nothing status-specific to say.
SECTIONS: dict[str, dict[str, tuple[str, str, str]]] = {
    "switchgear_vs_intensity": {
        "cannot_run": (
            "Obtain the building's elevations and its base flood elevation from an "
            "Elevation Certificate or the FIRM panel and record them under "
            "design_intensity before this section can be drafted, following {doc}.",
            "fema-elevation-certificate", "the Elevation Certificate",
        ),
        "failed": (
            "Write the essential electrical system into the plan as a flood-exposed "
            "asset: the elevations, what they put at risk and the interim measure "
            "that holds until the equipment is raised or relocated, following {doc}.",
            "nfpa-99", "NFPA 99",
        ),
        "": (
            "Keep the elevation evidence and the load-test schedule for the essential "
            "electrical system together in the plan's power annex, following {doc}.",
            "nfpa-110", "NFPA 110",
        ),
    },
    "written_trigger": {
        "unanswered": (
            "Write the evacuation trigger down: the condition, the person who holds "
            "the authority to pull it, and the notice transport needs, following {doc}.",
            "aspr-tracie-evac", "the ASPR TRACIE Hospital Evacuation Toolkit",
        ),
        "failed": (
            "Move the evacuation trigger earlier than the transport lead time it "
            "depends on, and name the forecast condition that starts the clock, "
            "following {doc}.",
            "aspr-tracie-evac", "the ASPR TRACIE Hospital Evacuation Toolkit",
        ),
        "": (
            "Keep the written trigger, its authority and its transport lead time in "
            "one section of the plan, and rehearse pulling it, following {doc}.",
            "cms-482-15", "42 CFR 482.15",
        ),
    },
    "priority_order_in_advance": {
        "failed": (
            "Decide and write the occupant priority order now, in daylight, with the "
            "clinical rationale beside it, following {doc}.",
            "aspr-tracie-evac", "the ASPR TRACIE Hospital Evacuation Toolkit",
        ),
        "unanswered": (
            "Record who approved the occupant priority order and when, so the plan "
            "shows the decision was taken in advance, following {doc}.",
            "cms-482-15", "42 CFR 482.15",
        ),
        "": (
            "Re-approve the occupant priority order on the plan's review cycle so it "
            "stays a decision taken in advance, following {doc}.",
            "cms-482-15", "42 CFR 482.15",
        ),
    },
    "partner_correlated_failure": {
        "failed": (
            "Add at least one receiving facility outside this county, this floodplain "
            "and this grid feeder, and record the same three flags for every partner, "
            "following {doc}.",
            "cms-482-15", "42 CFR 482.15",
        ),
        "unanswered": (
            "Sign a transfer agreement with a receiving facility and record its "
            "county, floodplain and grid feeder, following {doc}.",
            "cms-482-15", "42 CFR 482.15",
        ),
        "": (
            "Re-check every partner's county, floodplain and grid feeder whenever the "
            "plan is reviewed, following {doc}.",
            "fema-cpg-101", "CPG 101",
        ),
    },
    "campus_seam": {
        "unanswered": (
            "Write one joint annex with the other operator on this campus naming who "
            "moves whose occupants, who calls it and who pays, following {doc}.",
            "fema-cpg-101", "CPG 101",
        ),
        "": (
            "State in the plan that no other operator shares this campus, so a reader "
            "can see the seam was considered, following {doc}.",
            "fema-cpg-101", "CPG 101",
        ),
    },
    "first_break": {
        "failed": (
            "Write the fallback for the hour the first reserve gives out: what load is "
            "shed, what is brought in and who is called, following {doc}.",
            "nfpa-110", "NFPA 110",
        ),
        "": (
            "Record the reserves and the date they were last measured, so the break "
            "hour in this report can be recomputed, following {doc}.",
            "cms-482-15", "42 CFR 482.15",
        ),
    },
}

#: The one sentence in the document allowed to say "alerts": the fixed
#: disclaimer `readiness.cite` exempts, with the guidance entry it must cite.
DISCLAIMER_GUIDANCE = cite.NOT_A_WARNING_GUIDANCE


def _evidence_claim(finding: Finding) -> cite.Claim | None:
    """The first facility field the finding leaned on, so the advice cites it too."""
    for claim in finding.claims:
        if claim.source.kind == "facility":
            return claim
    return None


def section_for(
    finding: Finding, rule: str
) -> tuple[cite.Sentence | None, list[cite.Claim]]:
    """The drafted sentence for one finding, and the claims it cites."""
    templates = SECTIONS.get(rule)
    if not templates:
        return None, []
    entry = templates.get(finding.status) or templates.get("")
    if not entry:
        return None, []
    template, guidance_id, spelling = entry
    guidance = guidance_claim(guidance_id)
    claims = [guidance]
    markers = f"[c:{guidance.id}]"
    evidence = _evidence_claim(finding)
    if evidence is not None:
        claims.append(evidence)
        markers += f"[c:{evidence.id}]"
    text = " ".join(template.format(doc=spelling).split())
    return cite.Sentence.from_text(f"{text} {markers}"), claims


def sections(
    facility: Facility, scenario: Scenario, findings: Sequence[Finding]
) -> dict[str, tuple[list[cite.Sentence], list[cite.Claim]]]:
    """Per question id, the drafted prose and the claims it cites.

    `facility` is not read for the wording — the recommendation follows from
    the finding and its guidance — but it is taken so the signature does not
    have to change when a later scenario's section turns on the occupancy type.
    """
    out: dict[str, tuple[list[cite.Sentence], list[cite.Claim]]] = {}
    by_id = {q.id: q for q in scenario.questions}
    for finding in findings:
        question = by_id.get(finding.question_id)
        if question is None:
            continue
        sentence, claims = section_for(finding, question.rule)
        if sentence is None:
            continue
        out[finding.question_id] = ([sentence], claims)
    return out


def disclaimer() -> tuple[cite.Sentence, cite.Claim]:
    """The fixed not-a-warning line every surface of this product carries."""
    claim = guidance_claim(DISCLAIMER_GUIDANCE)
    claim = cite.Claim(
        id=claim.id,
        text="official alerting: NWS watches and warnings, and IPAWS",
        value=None,
        source=claim.source,
    )
    return (
        cite.Sentence.from_text(f"{cite.NOT_A_WARNING_SENTENCE} [c:{claim.id}]."),
        claim,
    )


def guidance_ids() -> tuple[str, ...]:
    """Every guidance id the local drafter can cite, for a test to check."""
    found = {entry[1] for rule in SECTIONS.values() for entry in rule.values()}
    found.add(DISCLAIMER_GUIDANCE)
    return tuple(sorted(found))


__all__ = [
    "DISCLAIMER_GUIDANCE",
    "SECTIONS",
    "disclaimer",
    "guidance_ids",
    "section_for",
    "sections",
]
