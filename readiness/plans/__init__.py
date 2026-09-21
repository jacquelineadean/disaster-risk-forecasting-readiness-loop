"""`readiness.plans` — the planning thought-partner (report §6, Phase 3).

A facility record, a library of scenarios, the rules that answer each
scenario's questions from that record and the validated risk layer, the cited
gap report those answers become, the blinded rendering a reviewer reads, and
the review records and case studies that make the whole thing checkable.

What is deliberately absent, and why:

* **No address, coordinate, tract, block or parcel field.** Not in the
  facility schema, not in a report, not in a review record — and the guard is
  a scan of keys *and* of string values, because a key list cannot cover a
  free-text note. The design intensity inject 3 needs comes from the
  planner's own Elevation Certificate and is cited as a facility document.
* **No name in prose.** A rule writes `PARTNER-1`, `COUNTY-A` or
  `DOCUMENT-3`; the name lives in the claim the sentence cites, which is what
  the blinded render drops. The blinded page carries no slug, no name, no
  county and no timestamp, and the render refuses to return one that does.
* **No LLM.** Nothing in this package calls a model. The optional `claude`
  drafter lives in `readiness/agent/planner.py`, behind a lazy import, and
  every sentence it returns goes through `readiness.cite.validate` before it
  can reach a page.
* **No labels and no scoring.** The risk layer is read from `issued/` files
  Phase 2 already wrote and validated; nothing here fits, refits or scores,
  and the three fields kept from a ledger card are the ones a citation to it
  needs — no scorecard, no split, no label count crosses into this package.
* **No substitute numbers.** A missing design intensity is a `cannot_run`
  finding naming the document that would supply it, and a county with no
  validated issuance gets an explicit statement of absence. Neither is ever
  filled in from a region-level number.

Standard library only, so the package runs in the browser sandbox alongside
the rest of `readiness`.
"""

from readiness.plans.facility import Facility, FacilityError
from readiness.plans.risk import RiskLayer
from readiness.plans.rules import RULES, Finding
from readiness.plans.scenarios import Scenario, ScenarioError

__all__ = [
    "RULES",
    "Facility",
    "FacilityError",
    "Finding",
    "RiskLayer",
    "Scenario",
    "ScenarioError",
]
