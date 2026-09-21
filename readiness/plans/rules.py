"""The rules that answer a scenario's questions from the record and the layer.

One rule per question. Each returns a `Finding`: a status, the sentences that
say what was found, and the claims those sentences cite. Every sentence cites
at least one claim and every number in a sentence is the rendered value of a
claim, because `readiness.cite` checks exactly that before a gap report is
written — these rules are simply written so that it passes.

Where the numbers come from, and nowhere else:

* the facility record, cited by field path (`power.fuel_hours`), which resolves
  back to the evidence entry naming the document it was read from;
* the scenario, cited as `<scenario id>/<constant>` — so "96" in prose is the
  scenario's own `isolation_hours`, not a number someone typed;
* the issued risk layer, cited as the issued file and its period;
* a named guidance document from `plans/guidance.json`;
* a `computed` claim derived from the claims above, named in its source.

**No rule ever writes a name.** A partner, a co-tenant or a document is
referred to by a positional label the record fixes — `PARTNER-1`, `PARTNER-2`,
`COUNTY-A` — and the name itself lives in the claim that sentence cites, after
`NAME_MARK`. Two things follow: blinding is structural (the blinded render
drops the tail of a claim's text; it does not search prose for names a drafter
may have reworded), and a partner called "Regional Medical Center 2" or "Alert
Bay Hospital" no longer makes the report unwritable, because the digit and the
word never enter validated prose.

The one rule that matters most is the one that refuses to answer.
`switchgear_vs_intensity` never reads the risk layer at all: a county
probability is a statement about occurrence somewhere in a county over a
period, and using it to answer "is the switchgear above the water" would
produce something that looks like an answer and is not one. Missing design
intensity is a single `cannot_run` finding naming the Elevation Certificate
that would supply it.
"""

from __future__ import annotations

import dataclasses
from typing import Callable, Mapping, Sequence

from readiness import cite
from readiness.plans.facility import SCHEMA, Facility, populated_paths
from readiness.plans.risk import RiskLayer
from readiness.plans.scenarios import Scenario

#: The statuses a finding may carry. "unanswered" is a finding, not a gap in
#: the test (the scenario's own pass condition says so).
STATUSES: tuple[str, ...] = ("answered", "unanswered", "failed", "cannot_run")

#: How a number read from the facility record reads in prose: plainly, with no
#: thousands separators and never in scientific notation, so 32.5 reads
#: "32.5", 12.0 reads "12", 0.031 reads "0.031" and 12345678 reads "12345678"
#: rather than "1.23457e+07". `"{:g}"` would do the first three and not the
#: fourth, and a report that spells a record's own number differently from the
#: record is both unreadable and a different number. Fifteen significant
#: digits is every magnitude a facility field can honestly carry.
NUMBER_FMT = "{:.15g}"

#: What each field path means, so a claim's text is a property of the field
#: rather than of whichever rule happened to cite it first.
FIELD_TEXT: dict[str, str] = {
    "slug": "the facility's identifier in this planning record",
    "occupancy_type": "the facility's occupancy type as recorded",
    "county_fips": "the county the facility sits in",
    "census": "occupant census the record carries",
    "staff_on_shift": "staff on shift the record carries",
    "power.generator": "whether the record says a generator is installed on site",
    "power.fuel_hours": "hours of generator fuel held on site, from the record",
    "power.switchgear_elevation_ft": "elevation of the electrical switchgear, in feet",
    "power.transfer_switch_elevation_ft": "elevation of the transfer switch, in feet",
    "power.load_test_interval_days": "days between generator load tests, from the record",
    "water.on_site_storage_hours": "hours of water held on site, from the record",
    "design_intensity.flood_elevation_ft":
        "design flood elevation for this building, in feet, from the record's "
        "elevation certificate",
    "design_intensity.flood_elevation_source":
        "the document the design flood elevation was read from",
    "design_intensity.design_wind_mph": "design wind speed for this building, in mph",
    "evacuation.trigger_written":
        "whether the record says the evacuation trigger is written down",
    "evacuation.trigger_text": "the written evacuation trigger, as the record quotes it",
    "evacuation.authority": "who holds the authority to order an evacuation",
    "evacuation.transport_lead_hours":
        "hours of notice transport needs before an evacuation, from the record",
    "evacuation.priority_order_written":
        "whether the occupant priority order is written down in advance",
    "evacuation.priority_decided_on": "when the occupant priority order was decided",
    "transfer_agreements": "the receiving facilities the record lists",
    "co_located_operators": "the other operators sharing this campus",
}

#: Where a claim's text stops describing and starts naming. Everything after
#: it is the record's own wording of a name, and the blinded render cuts there.
NAME_MARK = " — recorded as: "

#: What each rule reads from the record, whatever the scenario declares. The
#: fail-closed guard is the union of the two, so a scenario that under-declares
#: `fail_closed_on` produces a `cannot_run` finding naming the missing field
#: rather than a `RuleError` out of the middle of the rule.
REQUIRED_PATHS: dict[str, tuple[str, ...]] = {
    "switchgear_vs_intensity": (
        "design_intensity.flood_elevation_ft",
        "power.switchgear_elevation_ft",
        "power.transfer_switch_elevation_ft",
    ),
}

#: The guidance a finding points a planner at, by rule.
GUIDANCE: dict[str, tuple[str, ...]] = {
    "switchgear_vs_intensity": ("fema-elevation-certificate", "nfpa-110", "nfpa-99"),
    "written_trigger": ("aspr-tracie-evac", "cms-482-15"),
    "priority_order_in_advance": ("aspr-tracie-evac", "cms-482-15"),
    "partner_correlated_failure": ("cms-482-15", "fema-cpg-101"),
    "campus_seam": ("fema-cpg-101",),
    "first_break": ("nfpa-110", "cms-482-15"),
}

GUIDANCE_TEXT: dict[str, str] = {
    "fema-elevation-certificate":
        "the FEMA Elevation Certificate and Flood Insurance Rate Map, which carry a "
        "building's elevations and its base flood elevation",
    "nfpa-110":
        "NFPA 110, the standard for emergency and standby power systems, on where "
        "the equipment sits and how it is tested",
    "nfpa-99": "NFPA 99, the Health Care Facilities Code, on essential electrical systems",
    "aspr-tracie-evac":
        "the ASPR TRACIE Hospital Evacuation Toolkit, on evacuation triggers, "
        "patient prioritisation and transport lead times",
    "cms-482-15":
        "the CMS emergency preparedness Condition of Participation, on the emergency "
        "plan, subsistence needs, evacuation and standby power",
    "fema-cpg-101":
        "FEMA's Comprehensive Preparedness Guide on developing and maintaining "
        "emergency operations plans",
}


class RuleError(ValueError):
    """A rule that cannot be applied at all — a scenario and a record that disagree."""


@dataclasses.dataclass(frozen=True)
class Finding:
    """What one question came to: a status, the prose, and what the prose cites."""

    question_id: str
    status: str
    sentences: tuple[cite.Sentence, ...] = ()
    claims: tuple[cite.Claim, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise RuleError(
                f"finding for {self.question_id}: unknown status {self.status!r}; "
                f"expected one of {list(STATUSES)}"
            )
        object.__setattr__(self, "sentences", tuple(self.sentences))
        object.__setattr__(self, "claims", tuple(self.claims))

    @property
    def is_gap(self) -> bool:
        """Anything but a clean answer is a finding a planner has to act on."""
        return self.status != "answered"

    def text(self) -> str:
        return " ".join(cite.strip_markers(s.text) for s in self.sentences).strip()


# --------------------------------------------------------------------------- #
# Claim builders
# --------------------------------------------------------------------------- #


def facility_refs(facility: Facility) -> dict[str, object]:
    """Every `facility` citation this record licenses, flat, by field path.

    Flat rather than nested because a field path may index into a list
    (`transfer_agreements.0.signed`), and because a path that exists in the
    schema but is null is still a path a sentence may cite — saying a field is
    empty is exactly what a fail-closed finding does.
    """
    out: dict[str, object] = {}
    for key, spec in SCHEMA.items():
        out[key] = facility.get(key)
        if isinstance(spec, Mapping):
            for leaf in spec:
                out[f"{key}.{leaf}"] = facility.get(f"{key}.{leaf}")
    for path in populated_paths(facility.raw):
        out[path] = facility.get(path)
    return out


def number_claim(facility: Facility, path: str, *, fmt: str = NUMBER_FMT) -> cite.Claim:
    """A number the record carries, cited by its field path."""
    value = facility.get(path)
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuleError(f"facility field {path!r} is not a number: {value!r}")
    return cite.Claim(
        id=f"f-{path}",
        text=FIELD_TEXT.get(path, path),
        value=value,
        source=cite.Source("facility", path),
        fmt=fmt,
    )


def fact_claim(path: str, text: str | None = None) -> cite.Claim:
    """A fact the record states that is not a number: a flag, a name, a date.

    The value is deliberately None. A boolean is not a quantity, and a name or
    a date printed into prose would have to be matched as a number by the
    validator; the claim's text carries the fact, and the field path carries
    where it was read.
    """
    return cite.Claim(
        id=f"f-{path}",
        text=text or FIELD_TEXT.get(path, path),
        value=None,
        source=cite.Source("facility", path),
    )


def name_claim(path: str, label: str, name: str, what: str) -> cite.Claim:
    """The one place a facility, partner or co-tenant name is written down.

    Prose carries `label`; this claim carries the name, and `blinded_document`
    drops everything from `NAME_MARK` onwards so the blinded page keeps the
    label and loses the name. That is why blinding is structural here rather
    than a search-and-replace over prose a model may have reworded.
    """
    return cite.Claim(
        id=f"f-{path}",
        text=f"{label}, {what}{NAME_MARK}{name}",
        value=None,
        source=cite.Source("facility", path),
    )


def scenario_claim(scenario: Scenario, name: str, text: str) -> cite.Claim:
    return cite.Claim(
        id=f"s-{name}",
        text=text,
        value=scenario.constant(name),
        source=cite.Source("scenario", scenario.ref(name)),
        fmt=NUMBER_FMT,
    )


def guidance_claim(guidance_id: str) -> cite.Claim:
    return cite.Claim(
        id=f"g-{guidance_id}",
        text=GUIDANCE_TEXT.get(guidance_id, guidance_id),
        value=None,
        source=cite.Source("guidance", guidance_id),
    )


def _sentence(text: str) -> cite.Sentence:
    return cite.Sentence.from_text(" ".join(text.split()))


def _paths(names: Sequence[str]) -> str:
    return ", ".join(names)


def _and_list(parts: Sequence[str]) -> str:
    """"a", "a and b", "a, b and c" — prose, not a comma-separated dump."""
    parts = list(parts)
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return ", ".join(parts[:-1]) + " and " + parts[-1]


# --------------------------------------------------------------------------- #
# The rules
# --------------------------------------------------------------------------- #


def switchgear_vs_intensity(
    facility: Facility, risk: RiskLayer, scenario: Scenario
) -> Finding:
    """Where the electrical equipment sits relative to the design intensity.

    Fail-closed and deliberately blind to `risk`: this rule does not take the
    risk layer's probabilities into account at all, because there is no
    arithmetic that turns "18% chance of at least one damaging event in this
    county this quarter" into a water depth at a switchgear.
    """
    question = scenario.question_for_rule("switchgear_vs_intensity")
    wanted = dict.fromkeys(
        (*scenario.fail_closed_on, *REQUIRED_PATHS["switchgear_vs_intensity"])
    )
    missing = [path for path in wanted if facility.get(path) is None]
    certificate = guidance_claim("fema-elevation-certificate")
    if missing:
        return Finding(
            question.id, "cannot_run",
            [
                _sentence(
                    f"This scenario cannot be run for the electrical equipment: the "
                    f"record leaves {_paths(missing)} empty, and those elevations come "
                    f"from the facility's own Elevation Certificate or FIRM panel "
                    f"[c:{certificate.id}]."
                ),
                _sentence(
                    "No region-level number is put in their place: a county probability "
                    "says that a damaging event may occur somewhere in the county, not "
                    "how deep the water stands at this switchgear, and an answer that "
                    f"looks like one is worse than none [c:{certificate.id}]."
                ),
            ],
            [certificate],
        )

    design = number_claim(facility, "design_intensity.flood_elevation_ft")
    switchgear = number_claim(facility, "power.switchgear_elevation_ft")
    transfer = number_claim(facility, "power.transfer_switch_elevation_ft")
    hour = scenario_claim(
        scenario, "design_intensity_hour",
        "the hour at which the scenario's design-basis intensity is reached",
    )
    source = fact_claim("design_intensity.flood_elevation_source")
    nfpa = guidance_claim("nfpa-110")
    claims = [design, switchgear, transfer, hour, source, nfpa, certificate]

    # At the design flood elevation is *in* the water: inject 3 is standing
    # water reaching that elevation, and equipment sitting exactly there is
    # wet. The comparison is therefore `<=`, and the prose says "at or below".
    below = [
        label
        for label, claim in (("switchgear", switchgear), ("transfer switch", transfer))
        if float(claim.value) <= float(design.value)
    ]
    stated = _sentence(
        f"The design flood elevation for this building is "
        f"{cite.render_value(design)} feet, from the document the record names "
        f"[c:{design.id}][c:{source.id}]."
    )
    measured = _sentence(
        f"The switchgear sits at {cite.render_value(switchgear)} feet and the transfer "
        f"switch at {cite.render_value(transfer)} feet "
        f"[c:{switchgear.id}][c:{transfer.id}]."
    )
    if below:
        verdict = _sentence(
            f"The {_and_list(below)} therefore "
            f"{'sit' if len(below) > 1 else 'sits'} at or below the design flood "
            f"elevation, so from hour {cite.render_value(hour)} of the scenario the "
            f"essential electrical system is inside the water, whether or not the "
            f"generator itself keeps running [c:{hour.id}][c:{nfpa.id}]."
        )
        return Finding(question.id, "failed", [stated, measured, verdict], claims)
    verdict = _sentence(
        f"Both sit clear above the design flood elevation, so the scenario's "
        f"intensity at hour {cite.render_value(hour)} does not by itself take the "
        f"essential "
        f"electrical system out [c:{hour.id}][c:{nfpa.id}]."
    )
    return Finding(question.id, "answered", [stated, measured, verdict], claims)


def written_trigger(facility: Facility, risk: RiskLayer, scenario: Scenario) -> Finding:
    """The written trigger, who pulls it, and whether transport can still come."""
    question = scenario.question_for_rule("written_trigger")
    evacuation = facility.evacuation
    road = scenario_claim(
        scenario, "road_access_lost_hour",
        "the hour at which the scenario closes the roads for resupply or evacuation",
    )
    tracie = guidance_claim("aspr-tracie-evac")
    claims: list[cite.Claim] = [road, tracie]

    missing = []
    if not evacuation.trigger_written:
        missing.append("evacuation.trigger_written")
    if not evacuation.authority:
        missing.append("evacuation.authority")
    if evacuation.transport_lead_hours is None:
        missing.append("evacuation.transport_lead_hours")
    if missing:
        claims += [fact_claim(path) for path in missing]
        return Finding(
            question.id, "unanswered",
            [_sentence(
                f"The record cannot answer this question: it leaves {_paths(missing)} "
                f"empty, and a trigger without a named authority and a transport lead "
                f"time is not a trigger anyone can pull [c:{tracie.id}]"
                + "".join(f"[c:f-{path}]" for path in missing) + "."
            )],
            claims,
        )

    lead = number_claim(facility, "evacuation.transport_lead_hours")
    written = fact_claim("evacuation.trigger_written")
    authority = fact_claim("evacuation.authority")
    claims += [lead, written, authority]
    stated = _sentence(
        f"The evacuation trigger is written down, the record names the authority who "
        f"pulls it, and transport needs {cite.render_value(lead)} hours of notice "
        f"[c:{written.id}][c:{authority.id}][c:{lead.id}]."
    )
    if float(lead.value) > float(road.value):
        verdict = _sentence(
            f"The scenario closes the roads at hour {cite.render_value(road)}, which is "
            f"less notice than transport needs, so a trigger pulled when the scenario "
            f"begins is already too late [c:{road.id}][c:{tracie.id}]."
        )
        return Finding(question.id, "failed", [stated, verdict], claims)
    verdict = _sentence(
        f"The scenario closes the roads at hour {cite.render_value(road)}, so the "
        f"trigger still has time to be pulled once the scenario begins — which is the "
        f"latest it could be, not the intent [c:{road.id}][c:{tracie.id}]."
    )
    return Finding(question.id, "answered", [stated, verdict], claims)


def priority_order_in_advance(
    facility: Facility, risk: RiskLayer, scenario: Scenario
) -> Finding:
    """Whether the occupant priority order was decided before the scenario began."""
    question = scenario.question_for_rule("priority_order_in_advance")
    tracie = guidance_claim("aspr-tracie-evac")
    cms = guidance_claim("cms-482-15")
    written = fact_claim("evacuation.priority_order_written")
    claims = [written, tracie, cms]

    if not facility.evacuation.priority_order_written:
        return Finding(
            question.id, "failed",
            [_sentence(
                f"The occupant priority order is not written down in advance, so it "
                f"would be decided during the scenario by whoever is on shift — which "
                f"is the finding this question is looking for, not an answer to it "
                f"[c:{written.id}][c:{tracie.id}]."
            )],
            claims,
        )
    if not facility.evacuation.priority_decided_on:
        return Finding(
            question.id, "unanswered",
            [_sentence(
                f"The record states that the occupant priority order is written down "
                f"but does not say when it was decided or by whom, so the question is "
                f"unanswered from the record alone [c:{written.id}][c:{cms.id}]."
            )],
            claims,
        )
    decided = fact_claim("evacuation.priority_decided_on")
    claims.append(decided)
    return Finding(
        question.id, "answered",
        [_sentence(
            f"The occupant priority order is written down in advance and the record "
            f"names when it was decided, so it is not a decision taken under duress "
            f"on day three [c:{written.id}][c:{decided.id}]."
        )],
        claims,
    )


def partner_correlated_failure(
    facility: Facility, risk: RiskLayer, scenario: Scenario
) -> Finding:
    """Whether the signed receiving facilities fail in the same scenario we do."""
    question = scenario.question_for_rule("partner_correlated_failure")
    cms = guidance_claim("cms-482-15")
    agreements = fact_claim("transfer_agreements")
    county = fact_claim("county_fips")
    claims: list[cite.Claim] = [agreements, cms, county]

    signed = [
        (i, a) for i, a in enumerate(facility.transfer_agreements) if a.signed
    ]
    if not signed:
        return Finding(
            question.id, "unanswered",
            [_sentence(
                f"The record lists no signed transfer agreement, so there is no "
                f"receiving facility to test for correlated failure "
                f"[c:{agreements.id}][c:{cms.id}]."
            )],
            claims,
        )

    labels = facility.partner_labels()
    sentences: list[cite.Sentence] = []
    correlated: list[str] = []
    for index, agreement in signed:
        label = labels.get(agreement.name, f"PARTNER-{index + 1}")
        # (reason, the field path the reason turns on) — one claim per path, so
        # a reader following the citation lands on the flag, not on the county.
        reasons: list[tuple[str, str]] = []
        if agreement.county_fips == facility.county_fips:
            reasons.append((
                "sits in the same county as this facility",
                f"transfer_agreements.{index}.county_fips",
            ))
        if agreement.same_floodplain:
            reasons.append((
                "is recorded as being in the same floodplain",
                f"transfer_agreements.{index}.same_floodplain",
            ))
        if agreement.same_grid_feeder:
            reasons.append((
                "is recorded as being on the same grid feeder",
                f"transfer_agreements.{index}.same_grid_feeder",
            ))
        named = name_claim(
            f"transfer_agreements.{index}.name", label,
            "the receiving facility this transfer agreement names", agreement.name,
        )
        signed_claim = fact_claim(
            f"transfer_agreements.{index}.signed",
            f"the signed transfer agreement with {label}",
        )
        claims += [named, signed_claim]
        if not reasons:
            continue
        correlated.append(label)
        reason_claims = [
            fact_claim(path, f"the shared-failure fact recorded for {label} at {path}")
            for _reason, path in reasons
        ]
        claims.extend(reason_claims)
        markers = "".join(f"[c:{c.id}]" for c in (signed_claim, named, *reason_claims))
        sentences.append(_sentence(
            f"{label} holds a signed transfer agreement and "
            f"{_and_list([reason for reason, _path in reasons])}, so it is likely "
            f"to be inside the same event this facility is inside {markers}."
        ))

    context, context_claims = _risk_context(
        facility, risk, [a for _, a in signed], derived_from=county.id
    )
    claims.extend(context_claims)
    if correlated:
        sentences.append(_sentence(
            f"A receiving facility that fails when this one does is not a receiving "
            f"facility, and the plan needs at least one partner outside the shared "
            f"failure [c:{cms.id}]."
        ))
        return Finding(question.id, "failed", sentences + context, claims)
    ok = _sentence(
        f"Every signed transfer agreement is with a facility in another county that "
        f"the record does not place in this floodplain or on this grid feeder "
        f"[c:{agreements.id}][c:{cms.id}]."
    )
    return Finding(question.id, "answered", [ok] + context, claims)


def _risk_context(
    facility: Facility,
    risk: RiskLayer,
    partners: Sequence[object],
    *,
    derived_from: str,
) -> tuple[list[cite.Sentence], list[cite.Claim]]:
    """The issued probabilities for both counties, as context and never as the test.

    Correlated failure is decided by the record's own flags above. These
    sentences say what the validated risk layer holds for the two counties, or
    that it holds nothing — an absence is stated, never filled in.
    """
    sentences: list[cite.Sentence] = []
    claims: list[cite.Claim] = []
    partner_labels = facility.partner_labels()
    #: FIPS -> every partner label in that county, so two partners in one
    #: county are both named rather than the second overwriting the first.
    shared: dict[str, list[str]] = {facility.county_fips: []}
    for partner in partners:
        fips = getattr(partner, "county_fips", None)
        if not fips or fips == facility.county_fips:
            continue
        name = getattr(partner, "name", "")
        shared.setdefault(fips, []).append(partner_labels.get(name, name or fips))
    for fips, names in shared.items():
        label = (
            "this facility's county" if fips == facility.county_fips
            else f"the county {_and_list(names)} sits in"
        )
        found = [risk.probability(fips, name) for name in risk.covering(fips)]
        found = [claim for claim in found if claim is not None]
        if not found:
            absent = risk.absence(fips, derived_from=derived_from)
            claims.append(absent)
            sentences.append(_sentence(
                f"For context, no validated issuance covers {label} for this period, "
                f"so no probability is reported for it rather than one being borrowed "
                f"from somewhere else [c:{absent.id}]."
            ))
            continue
        claims.extend(found)
        rendered = " and ".join(
            f"{cite.render_value(c)} [c:{c.id}]" for c in found
        )
        sentences.append(_sentence(
            f"For context, the validated risk layer puts the chance of at least one "
            f"damaging event in {label} during this period at {rendered}."
        ))
    return sentences, claims


def campus_seam(facility: Facility, risk: RiskLayer, scenario: Scenario) -> Finding:
    """The seam between two operators in one building, where plans are thinnest."""
    question = scenario.question_for_rule("campus_seam")
    operators = facility.co_located_operators
    listed = fact_claim("co_located_operators")
    cpg = guidance_claim("fema-cpg-101")
    claims: list[cite.Claim] = [listed, cpg]
    if not operators:
        return Finding(
            question.id, "answered",
            [_sentence(
                f"The record names no other operator on this campus, so there is no "
                f"seam between operators for this scenario to run across "
                f"[c:{listed.id}]."
            )],
            claims,
        )
    labels = facility.partner_labels()
    sentences = []
    for index, operator in enumerate(operators):
        label = labels.get(operator.name, f"PARTNER-{index + 1}")
        occupants = number_claim(facility, f"co_located_operators.{index}.occupants")
        occupants = dataclasses.replace(
            occupants, text=f"occupants {label} holds on this campus"
        )
        named = name_claim(
            f"co_located_operators.{index}.name", label,
            "the other operator on this campus", operator.name,
        )
        claims += [occupants, named]
        sentences.append(_sentence(
            f"{label} occupies part of this campus with "
            f"{cite.render_value(occupants)} occupants "
            f"[c:{occupants.id}][c:{named.id}]."
        ))
    sentences.append(_sentence(
        f"This record has no field for a joint plan across that seam, so the question "
        f"is unanswered here and belongs to the two operators together rather than to "
        f"either alone [c:{listed.id}][c:{cpg.id}]."
    ))
    return Finding(question.id, "unanswered", sentences, claims)


def first_break(facility: Facility, risk: RiskLayer, scenario: Scenario) -> Finding:
    """The hour at which the first recorded reserve runs out, and what follows."""
    question = scenario.question_for_rule("first_break")
    isolation = scenario_claim(
        scenario, "isolation_hours", "the length of the scenario's isolation, in hours"
    )
    closed = scenario_claim(
        scenario, "road_access_lost_hour", "the hour road access is lost"
    )
    reopened = scenario_claim(
        scenario, "road_access_restored_hour", "the hour road access returns"
    )
    nfpa = guidance_claim("nfpa-110")
    claims: list[cite.Claim] = [isolation, closed, reopened, nfpa]
    sentences: list[cite.Sentence] = []

    candidates: list[tuple[str, float, list[str]]] = []
    generator = fact_claim("power.generator")
    claims.append(generator)
    if facility.power.generator:
        fuel = number_claim(facility, "power.fuel_hours")
        claims.append(fuel)
        candidates.append(("generator fuel runs out", float(fuel.value), [fuel.id]))
    else:
        candidates.append(("there is no generator at all", 0.0, [generator.id]))

    storage = facility.water.on_site_storage_hours
    if storage is None:
        empty = fact_claim("water.on_site_storage_hours")
        claims.append(empty)
        sentences.append(_sentence(
            f"The record does not say how many hours of water are held on site, so "
            f"that limb of this answer is missing rather than assumed "
            f"[c:{empty.id}][c:{nfpa.id}]."
        ))
    else:
        water_hour = scenario_claim(
            scenario, "water_loss_hour", "the hour the scenario cuts municipal water"
        )
        stored = number_claim(facility, "water.on_site_storage_hours")
        claims += [water_hour, stored]
        candidates.append((
            "stored water runs out",
            float(stored.value) + float(water_hour.value),
            [stored.id, water_hour.id],
        ))

    label, hour, parts = min(candidates, key=lambda c: (c[1], c[0]))
    break_claim = cite.Claim(
        id="first-break-hour",
        text=(
            "the earliest hour at which a reserve the record carries runs out: the "
            "minimum over generator fuel and stored water, counted from the start of "
            "the scenario"
        ),
        value=hour,
        source=cite.Source("computed", "+".join(parts)),
        fmt=NUMBER_FMT,
    )
    claims.append(break_claim)
    sentences.append(_sentence(
        f"The first recorded reserve to give out is where {label}, at hour "
        f"{cite.render_value(break_claim)} of the scenario [c:{break_claim.id}]."
    ))
    if hour < float(isolation.value):
        sentences.append(_sentence(
            f"The scenario runs {cite.render_value(isolation)} hours and closes the "
            f"roads between hour {cite.render_value(closed)} and hour "
            f"{cite.render_value(reopened)}, so the plan breaks before the isolation "
            f"ends and, for much of that window, before anything can reach the site "
            f"[c:{isolation.id}][c:{closed.id}][c:{reopened.id}]."
        ))
        status = "failed"
    else:
        sentences.append(_sentence(
            f"That is at or beyond the {cite.render_value(isolation)} hours the "
            f"scenario runs, so no recorded reserve gives out inside it "
            f"[c:{isolation.id}]."
        ))
        status = "answered"
    sentences.append(_sentence(
        f"The record carries no field for what happens after those reserves, so the "
        f"fallback is the planner's to write and this report does not invent one "
        f"[c:{nfpa.id}]."
    ))
    return Finding(question.id, status, sentences, claims)


#: Rule name -> the function that answers its question. `scenarios.check_shape`
#: refuses a scenario naming a rule that is not here.
RULES: dict[str, Callable[[Facility, RiskLayer, Scenario], Finding]] = {
    "switchgear_vs_intensity": switchgear_vs_intensity,
    "written_trigger": written_trigger,
    "priority_order_in_advance": priority_order_in_advance,
    "partner_correlated_failure": partner_correlated_failure,
    "campus_seam": campus_seam,
    "first_break": first_break,
}


def run(facility: Facility, risk: RiskLayer, scenario: Scenario) -> list[Finding]:
    """Every question of the scenario, in the scenario's order."""
    findings = []
    for question in scenario.questions:
        rule = RULES.get(question.rule)
        if rule is None:
            raise RuleError(
                f"scenario {scenario.id}: question {question.id} names rule "
                f"{question.rule!r}, which is not in RULES"
            )
        findings.append(rule(facility, risk, scenario))
    return findings


def statuses(findings: Sequence[Finding]) -> dict[str, str]:
    """Question id -> status, the shape a case study's `expected_findings` takes."""
    return {f.question_id: f.status for f in findings}


__all__ = [
    "FIELD_TEXT",
    "NAME_MARK",
    "REQUIRED_PATHS",
    "GUIDANCE",
    "GUIDANCE_TEXT",
    "NUMBER_FMT",
    "RULES",
    "STATUSES",
    "Finding",
    "RuleError",
    "campus_seam",
    "facility_refs",
    "fact_claim",
    "first_break",
    "guidance_claim",
    "name_claim",
    "number_claim",
    "partner_correlated_failure",
    "priority_order_in_advance",
    "run",
    "scenario_claim",
    "statuses",
    "switchgear_vs_intensity",
    "written_trigger",
]
