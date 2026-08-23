# Scenario: 96-hour isolation of an acute-care hospital

**Status: Phase 3 seed.** Nothing executes this yet. It is written now, at
Phase 0, because report §6 makes it a *regression test* rather than a case
study:

> Case studies become regression tests: any plan the system blesses must survive
> the scenario that killed people at Memorial.

A regression test written after the feature is a test of the feature. Written
before, it is a specification.

---

## What happened

Memorial Medical Center, Uptown New Orleans, August 2005. Katrina had been
tracked for days. The hospital lost power and running water; interior
temperatures passed 100°F; staff and patients were marooned for four days.
Mortuary workers carried 45 bodies out — more than from any comparable-size
hospital in the flooded city. Tenet reported 34 patient deaths on campus, 24 of
them in the long-term acute-care unit run by LifeCare. Statewide, investigators
reviewed roughly 215 deaths at hospitals and nursing homes.

Sheri Fink's Pulitzer-winning investigation documented exhausted clinicians
improvising triage under conditions the institution's emergency plan had never
rehearsed — including decisions about which patients would be moved last, and
allegations that some were given lethal doses of sedatives. A physician and two
nurses were arrested in 2006; a grand jury declined to indict in 2007.

**The deaths were not caused by a missing forecast.** They were caused by a plan
that assumed short outages, working infrastructure, and prompt evacuation —
assumptions nobody had stress-tested against the region's known flood risk. The
failure was analytical, not meteorological, which is precisely why it is
tractable.

Sources: [8] Fink, *The Deadly Choices at Memorial* (ProPublica / NYT Magazine,
2009); [9] AMA Journal of Ethics (2010); AP/NBC (Oct 2005). Full citations in
[the report](../../report/index.html#sources).

---

## The scenario, as a test

**Preconditions.** A facility with: an address, an occupancy type, a patient or
occupant census, on-site generation, and a written emergency plan.

**Injects, applied in order and not individually:**

1. Grid power lost at T+0. No restoration for 96 hours.
2. Municipal water lost at T+6.
3. Standing water reaches the design flood elevation for this address by T+12,
   and does not recede before T+96.
4. No road access for resupply or evacuation between T+12 and T+72.
5. External ambient temperature at the 90th percentile for the season.
6. Mobile networks degraded but not absent.

The combination is the test. A plan that survives each inject alone and fails
their conjunction is the Memorial failure mode exactly.

**The plan must answer, with evidence:**

- Where is the electrical switchgear relative to the flood elevation in inject 3
  — and what fails when it goes under? *(Memorial's transfer switches were below
  flood level. The generators were fine. It did not matter.)*
- What is the **written** trigger for evacuating *before* landfall, who holds
  the authority to pull it, and by when must it be pulled for transport to still
  be available?
- What is the patient priority order, written down in advance — sickest first or
  sickest last? Who decided, and when? *(At Memorial this was decided under
  duress, on day four, by exhausted people. That is the finding.)*
- Which receiving facilities have signed transfer agreements, and **do they
  flood in the same scenarios you do?** *(Correlated failure is the trap: the
  nearest partner is usually in the same floodplain.)*
- What is the plan for occupants under a different operator on the same campus?
  *(24 of the 34 deaths were LifeCare's patients in Memorial's building. The
  seam between two operators in one building is where the plan was thinnest.)*
- At what hour does the plan first break, and what is the fallback?

**Pass condition.** Every question answered from the facility's own records plus
the computed risk layer, with a citation for each answer. An unanswered question
is a finding, not a gap in the test.

**Fail-closed rule.** If the system cannot obtain the flood elevation for this
address, it reports that it cannot run the scenario. It does not substitute a
county-level number and proceed — a county-level answer to a switchgear question
is worse than no answer, because it looks like one.

---

## What this requires from earlier phases

| need | phase |
|---|---|
| Calibrated flood probability for the county | 1 |
| Flood *depth* at an address, not just occurrence | 2 |
| Exposure join: this building, its occupancy type, its neighbours | 2 |
| Case-study corpus with after-action reports | 3 |

Depth is the hard dependency, and it is worth being explicit that Phase 0's
forecast unit — *at least one damaging event in a county-quarter* — cannot
answer inject 3. Occurrence and depth are different quantities. Phase 2 must
introduce depth deliberately rather than hoping the occurrence model generalises.

## Boundaries

Every recommendation traces to a computed number, a dataset row, or a named
guidance document. Prose that cannot cite one of those does not ship (report §7:
"Numbers come from the harness; prose comes from the model — and prose can
hallucinate").

A practising emergency manager reviews everything. The agent's job is to make
the analysis cheap enough that a rural hospital can afford it — not to be in
charge of it.

This scenario is built from a published investigation of a real institution. It
is used here as the engineering standard it deserves to be, and any product
surface that renders it should say the same.
