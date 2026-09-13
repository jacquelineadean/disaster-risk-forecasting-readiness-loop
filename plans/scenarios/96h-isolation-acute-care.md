# Scenario: 96-hour isolation of an acute-care facility

**Status: Phase 3 seed.** Nothing executes this yet. It is written now, at
Phase 0, because report §6 makes it a *regression test* rather than a case
study:

> Case studies become regression tests: any plan the system blesses must survive
> the scenarios that have killed people before.

A regression test written after the feature is a test of the feature. Written
before, it is a specification.

This scenario is hazard-agnostic on purpose. The injects below describe what
the facility *experiences* — loss of power, water, access — not which hazard
caused it. A hurricane, a riverine flood, an ice storm and a wildfire produce
the same first four injects; only the flood-elevation inject is specific to
water, and it is marked as such.

---

## The pattern this encodes

The failure it tests for recurs across hazards and decades, and it has the
same shape every time: the hazard was forecast, often days ahead; the plan
assumed short outages, working infrastructure and prompt evacuation; the
compound event — no power, then no water, then no road access, for longer
than anyone had rehearsed — arrived anyway; and decisions that should have
been written down in advance were made under duress, on day three or four, by
exhausted people.

**The deaths in such events are not caused by a missing forecast.** They are
caused by a plan that was never stress-tested against the region's known risk.
The failure is analytical, not meteorological, which is precisely why it is
tractable.

---

## The scenario, as a test

**Preconditions.** A facility with: an address, an occupancy type, a patient or
occupant census, on-site generation, and a written emergency plan.

**Injects, applied in order and not individually:**

1. Grid power lost at T+0. No restoration for 96 hours.
2. Municipal water lost at T+6.
3. *(water hazards)* Standing water reaches the design flood elevation for this
   address by T+12, and does not recede before T+96. *(Other hazards: the
   equivalent design-basis intensity for the address — wind, ice load, fire
   perimeter — is reached by T+12.)*
4. No road access for resupply or evacuation between T+12 and T+72.
5. External ambient temperature at the 90th percentile for the season.
6. Mobile networks degraded but not absent.

The combination is the test. A plan that survives each inject alone and fails
their conjunction is the failure mode exactly.

**The plan must answer, with evidence:**

- Where is the electrical switchgear relative to the design intensity in inject
  3 — and what fails when it is reached? *(Generators that survive are
  irrelevant if the transfer switches that connect them do not.)*
- What is the **written** trigger for evacuating *before* the hazard arrives,
  who holds the authority to pull it, and by when must it be pulled for
  transport to still be available?
- What is the occupant priority order, written down in advance — sickest first
  or sickest last? Who decided, and when? *(A priority order decided under
  duress on day four is the finding, not the answer.)*
- Which receiving facilities have signed transfer agreements, and **do they
  fail in the same scenarios you do?** *(Correlated failure is the trap: the
  nearest partner is usually in the same floodplain, on the same grid, behind
  the same roads.)*
- What is the plan for occupants under a different operator on the same campus?
  *(The seam between two operators in one building is where plans are
  thinnest.)*
- At what hour does the plan first break, and what is the fallback?

**Pass condition.** Every question answered from the facility's own records plus
the computed risk layer, with a citation for each answer. An unanswered question
is a finding, not a gap in the test.

**Fail-closed rule.** If the system cannot obtain the design intensity for this
address — the flood elevation, the design wind speed — it reports that it cannot
run the scenario. It does not substitute a region-level number and proceed: a
region-level answer to a switchgear question is worse than no answer, because it
looks like one.

---

## What this requires from earlier phases

| need | phase |
|---|---|
| Calibrated occurrence probability for the region, for the contract's hazard | 1 |
| Hazard *intensity* at an address (depth, wind, fire perimeter), not just occurrence | 2 |
| Exposure join: this building, its occupancy type, its neighbours | 2 |
| Case-study corpus with after-action reports | 3 |

Intensity is the hard dependency, and it is worth being explicit that Phase 0's
forecast unit — *at least one damaging event in a region-period* — cannot
answer inject 3. Occurrence and intensity are different quantities. Phase 2
must introduce intensity deliberately rather than hoping the occurrence model
generalises.

## Boundaries

Every recommendation traces to a computed number, a dataset row, or a named
guidance document. Prose that cannot cite one of those does not ship (report §7:
"Numbers come from the harness; prose comes from the model — and prose can
hallucinate").

A practising emergency manager reviews everything. The agent's job is to make
the analysis cheap enough that a rural hospital can afford it — not to be in
charge of it.

## Case studies

This template names no institution. Worked case studies — published
investigations of real events, with full citations — belong in
[`../case-studies/`](../case-studies/) and are added as examples, one per
event, each mapped onto the injects and questions above. A case study that
cannot cite a published investigation for every fact it asserts does not
belong in the library.
