# The gap report

**Status: Phase 3.** This page documents the command surface exactly as
scoped in [`plan.md`](plan.md) §4 and [`plan-design-annex.md`](plan-design-annex.md)
§3. Some of it is still being integrated in this tree; see
[`tests/test_docs.py`](../tests/test_docs.py) for which assertions are
guarded pending that integration.

A gap report is the second human-facing output of the loop, and the first
that reasons about a single building rather than a county. It stress-tests
one facility's emergency plan against a hazard-agnostic scenario and the
county's validated risk layer, answers every question the scenario asks with
evidence, and is written only when `readiness.cite.validate` finds nothing
wrong with it. Report §6 sets the terms:

> Case studies become regression tests: any plan the system blesses must
> survive the scenarios that have killed people before.

And report §7, the same rule the county brief obeys: "Numbers come from the
harness; prose comes from the model — and prose can hallucinate." Every
sentence of a gap report cites a fact from the facility's own record, a
computed number, or a named guidance document, or it does not ship.

The code is `readiness/plans/` (facility, scenarios, rules, the risk layer,
the report and its blinding, reviews, case studies), `readiness/agent/planner.py`
(the optional claude drafter) and the reused `readiness/cite.py`. The
scenario library lives beside its markdown in [`plans/`](../plans/README.md).

```bash
readiness scenarios list                              # the scenario library
readiness scenarios check [--case-studies DIR]         # run every case study, compare to its expected findings

readiness gap-report --facility PATH --period YYYY-Qn [--scenario ID] [--out DIR] [--drafter local|claude]

readiness review record --report PATH.blind.html --rating {not useful,somewhat useful,useful,very useful} \
    --role "practising emergency manager" --org-type hospital|county|state|ngo|other --years N \
    [--comments TEXT] [--reviews DIR]

readiness verify --phase 3 [--reports DIR] [--reviews DIR]     # no -c: reads the registry, not one contract
```

`make scenarios-check` and `make gap-report FACILITY=<path> PERIOD=<label>`
wrap the first two; `make verify PHASE=3` wraps the check.

## The facility record

A facility is a JSON file the planner supplies, read by `readiness.plans.facility`.
Unknown keys are refused, and every field that is populated must name an
entry in `evidence` — a fact with nowhere it came from does not belong in a
document that cites everything.

```
slug, occupancy_type ∈ {hospital, nursing_home, shelter, school, other}
county_fips, census, staff_on_shift
power        { generator, fuel_hours, switchgear_elevation_ft,
               transfer_switch_elevation_ft, load_test_interval_days }
water        { on_site_storage_hours }
design_intensity { flood_elevation_ft, flood_elevation_source, design_wind_mph }
evacuation   { trigger_written, trigger_text, authority, transport_lead_hours,
               priority_order_written, priority_decided_on }
transfer_agreements [ { name, county_fips, signed, same_floodplain, same_grid_feeder } ]
co_located_operators [ { name, occupants } ]
evidence     { <field path>: { text, source_doc, page } }
```

**No address or coordinate field exists.** There is nothing in this schema
from which a point on a map could be reconstructed — no address, no
latitude, no longitude, no tract, block or parcel. `design_intensity` is
supplied directly by the planner, read off the building's own elevation
certificate or FIRM panel and cited as a facility document
(`fema-elevation-certificate` in [`plans/guidance.json`](../plans/guidance.json)),
never looked up from an address. `readiness verify --phase 3` refuses any
committed JSON under `plans/` that carries an `address`, `lat`, `lon`,
`tract`, `block` or `parcel` key, so the guard is mechanical, not a
convention. Real facility files never enter git; `plans/facilities/example-rural-hospital.json`
(fictional) is the only one committed.

## The scenario and its rules

The scenario is JSON beside its markdown —
[`plans/scenarios/96h-isolation-acute-care.json`](../plans/scenarios/96h-isolation-acute-care.json)
next to [`96h-isolation-acute-care.md`](../plans/scenarios/96h-isolation-acute-care.md) — and a
test keeps the markdown the specification: the JSON's question ids and text
must match the prose. Each question is answered by one rule in
`readiness.plans.rules` against the facility record and the issued risk
layer, and every rule returns a **finding** with one of four statuses:

| status | meaning |
|---|---|
| `answered` | the record and the risk layer together answer the question, with a citation for every sentence |
| `unanswered` | the record does not say — an unanswered question is a finding, not a gap in the test |
| `failed` | the record answers, and the answer fails the scenario's pass condition (a switchgear below the design elevation, a priority order decided under duress, a signed partner in the same floodplain) |
| `cannot_run` | a rule cannot be evaluated at all because a fact it depends on is missing from the record |

**Fail-closed, by construction.** The rule that reads `design_intensity`
never substitutes a county-level number when the facility's own elevation
certificate is missing: a missing `flood_elevation_ft` or `flood_elevation_source`
produces one `cannot_run` finding naming the `fema-elevation-certificate`
guidance entry, and no other rule can stand in for it. A region-level answer
to a switchgear question is worse than no answer, because it looks like one
— the scenario markdown says so, and there is no code path that does it.

The risk layer a rule reads (`readiness.plans.risk.RiskLayer`) comes from
`issued/` and its backing test cards only, never a re-fit; when a county has
no issued file for the period, the absence is itself a cited claim, not a
silent zero.

## What a gap report is

`readiness gap-report` builds one document per facility and period from the
scenario's findings, the facility's own evidence, and the risk layer, and
writes it only when `readiness.cite.validate` returns no violation — the
same five rules the county brief passes (`UNCITED`, `UNKNOWN_CLAIM`,
`UNRESOLVED`, `NUMBER_WITHOUT_CLAIM`, `FORBIDDEN_PHRASE`; see
[`docs/brief.md`](brief.md#the-citation-rules-it-passes)), over a claim set
that additionally resolves `facility` field paths and `scenario` question
ids and constants. `--drafter local` fills each section from a fixed
template citing the finding's evidence and guidance ids; `--drafter claude`
lets `readiness/agent/planner.py` rewrite the prose, but every sentence it
produces must still carry `[c:ID]` citation markers into the same claim set,
`cite.validate` runs on its output exactly as it would on the local
drafter's, and any sentence that fails validation is dropped and counted on
the report's provenance line rather than shown. Numbers never come from the
model; only wording can.

Two renders are written together, plus the machine-readable claims:

- `<out>/<slug>/<period>.html` and `.json` — the plain report, for the
  facility's own use.
- `<out>/<slug>/<period>.blind.html` — the same document with the facility's
  slug, name and every named partner replaced by `FACILITY-<hash6>` and
  `PARTNER-n`, for review by someone who is not told which building they are
  reading. This is the file a review record cites.

`--out` defaults to `plans/reports/`. Exit 1, listing every violation, when
the document does not validate; exit 2 when the facility file cannot be
read (missing, invalid JSON, an unknown key, or a populated field with no
evidence entry). Nothing is written on either failure. **Real facility
files and real gap reports never enter git** — `plans/reports/` and
`plans/facilities/` carry only their README and the one fictional example.

## Reviews: an attestation bound to one report

A review record is not a survey response; it is an attestation that a
specific blinded document was shown to a specific kind of reader.
`readiness review record` computes the sha256 of the file named by
`--report` and refuses unless it is a real blinded render (a `.blind.html`
file this command itself could have produced); the record is written to
`plans/reviews/<sha256>.json` (or under `--reviews DIR`), and that filename
*is* the binding — a review naming a sha with no matching report is inert,
and a report that changes by one byte orphans every review of the version
before it. The rating vocabulary is closed to four values: `not useful`,
`somewhat useful`, `useful`, `very useful`. `--role` and `--org-type` record
who is attesting; nothing here checks that the string is true, which is
exactly the limit `verify --phase 3` states rather than papers over.

## Case studies: the mechanism, not the evidence

A case study (`plans/case-studies/<slug>.json`) maps a real, published event
onto a scenario's injects and answers its questions from the record, with a
source cited for every fact — see
[`plans/case-studies/README.md`](../plans/case-studies/README.md) for the
exact JSON shape. `readiness scenarios check` runs every committed case
study's facts through the same rules a live facility would go through and
compares the result to the file's own `expected_findings`. **None ships by
default.** The mechanism is proven against a synthetic case study built for
the tests (`tests/fixtures_plans.py::synthetic_case_study()`); the library is
empty in this repository on purpose, and stays that way until a real,
citable, published case study is added deliberately.

## `readiness verify --phase 3`

Takes no `-c`; it reads the registry of committed reviews, reports and case
studies, not one contract. Four checks, in this order:

1. **Reviews.** At least three reviews, each naming a distinct blinded
   `facility_hash`, `blinded: true`, a `reviewer_role` containing "emergency
   manager", and a rating of `useful` or `very useful`.
2. **Reports.** For each of those reviews, its `report_sha256` matches the
   sha256 of a blinded render found in `plans/reports/` (or `--reports DIR`
   — real reports are never committed, so a real run points this at the
   reviewer's copies), and that render validates under `readiness.cite`
   with zero violations. A report that has since changed invalidates the
   review that pointed at the old one.
3. **Case studies.** Every case study committed under `plans/case-studies/`
   reproduces its own `expected_findings` exactly.
4. **No coordinates.** No JSON file committed under `plans/` carries an
   `address`, `lat`, `lon`, `tract`, `block` or `parcel` key, anywhere in its
   structure.

Exit 0 only when all four hold. **Not mechanisable:** that the facilities
are real and the reviewers are practising emergency managers. A review
record is an attestation bound to a specific blinded report's hash, not
proof of who wrote it or what building it describes — check 1 above is the
honest extent of what `verify --phase 3` can check about that, and it says
so rather than implying more.

## What is never in a gap report

Everything [`docs/brief.md`](brief.md#what-is-never-in-a-brief) rules out —
warning language outside the fixed disclaimer, predictions of specific
events, a number typed in rather than cited, prose with no citation at all
— plus two rules specific to a facility document:

- **No address or coordinate, anywhere.** Not in the facility record (no
  such field exists), not in the report (blinding removes names and partner
  names; the underlying facts never included a point on a map to begin
  with).
- **No substituted intensity.** A missing elevation certificate is a
  `cannot_run` finding citing the guidance document that would resolve it,
  never a county-level or regional number standing in for the building's
  own.
