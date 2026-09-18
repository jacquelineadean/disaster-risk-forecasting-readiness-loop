# The gap report

**Status: Phase 3, built.** This page documents the command surface exactly as
scoped in [`plan.md`](plan.md) §4 and [`plan-design-annex.md`](plan-design-annex.md)
§3, and [`tests/test_docs.py`](../tests/test_docs.py) asserts every command and
flag it names against the parser.

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
slug, blind_id, occupancy_type ∈ {hospital, nursing_home, shelter, school, other}
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
from which a point on a map could be reconstructed. `design_intensity` is
supplied directly by the planner, read off the building's own elevation
certificate or FIRM panel and cited as a facility document
(`fema-elevation-certificate` in [`plans/guidance.json`](../plans/guidance.json)),
never looked up from an address.

Two scans make that mechanical rather than conventional, and both run in the
facility loader, in `gap_report.check` over a rendered document and in
`readiness verify --phase 3` over every committed JSON under `plans/`:

- **Keys.** Any of the keys `facility.FORBIDDEN_KEYS` lists — `address`,
  `address_line1`, `address_line2`, `apn`, `block`, `block_group`,
  `coordinates`, `easting`, `geocode`, `geohash`, `geometry`, `gps`, `lat`,
  `lat_lon`, `latitude`, `latlon`, `lon`, `longitude`, `northing`, `parcel`,
  `plus_code`, `postal_code`, `street`, `tract`, `zip`, `zipcode` — anywhere in
  the tree, matched case-insensitively.
- **Values.** A key scan alone is a fail-open, because a free-text note has
  whatever key its author chose. Any string value holding a street address
  (`412 Riverside Drive`), a ZIP+4 (`27834-1234`), a decimal-degree pair
  (`35.6127, -77.3664`) or the token `ZIP` refuses the file and names the path
  it was found at. A **bare five-digit number is not flagged**: that is a
  county FIPS, the one geography report §7 publishes.

`blind_id` is 32 lowercase hex characters the planner generates once,
`python3 -c "import secrets; print(secrets.token_hex(16))"`, and keeps. The
label a blinded report carries is its first twelve characters — not a digest
of the slug, which a dictionary search would invert in milliseconds. Numbers
that are hours, elevations, occupant counts or lead times may not be negative,
and every string is whitespace-normalised at load, so one name has one
spelling and the blinding cannot be defeated by a double space.

Real facility files never enter git; `plans/facilities/example-rural-hospital.json`
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

**No rule writes a name.** A partner, a co-tenant, a county or a document is
referred to positionally — `PARTNER-1`, `PARTNER-2`, `COUNTY-A`, `DOCUMENT-3`
— with the label fixed by the record (transfer agreements in order, then
co-located operators). The name itself lives in the claim the sentence cites,
which is what the blinded render drops. Two things follow: blinding is
structural rather than a search for names in prose a drafter may have
reworded, and a partner called "Regional Medical Center 2" or "Alert Bay
Hospital" no longer makes the report unwritable, because neither the digit nor
the word ever enters validated prose. Those labels are the document's own
identifiers, passed to `readiness.cite.validate` the way a brief passes the
county FIPS it is about; every *other* number in a sentence is a quantity and
must be the rendered value of a claim it cites.

**Fail-closed, by construction.** The rule that reads `design_intensity`
never substitutes a county-level number when the facility's own elevation
certificate is missing: a missing `flood_elevation_ft` or `flood_elevation_source`
produces one `cannot_run` finding naming the `fema-elevation-certificate`
guidance entry, and no other rule can stand in for it. A region-level answer
to a switchgear question is worse than no answer, because it looks like one
— the scenario markdown says so, and there is no code path that does it. The
guard is the **union** of the scenario's `fail_closed_on` and the rule's own
inputs (`rules.REQUIRED_PATHS`), so a scenario that under-declares its list
still refuses rather than raising.

Equipment sitting **exactly at** the design flood elevation is in the water:
inject 3 is standing water *reaching* that elevation, so the comparison is
`<=` and the finding reads "at or below". The analogous boundary in
`first_break` is the other way and deliberate — a reserve that gives out at
exactly hour 96 does not give out *inside* a 96-hour isolation — and both are
now tested at the boundary.

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
drafter's, and any sentence that fails validation is **refused** rather than
shown. A refusal is a refusal to reword: the local sentence stays exactly where
it was, so the report has the local report's sentences whichever drafter ran,
some of them reworded, and the provenance line reads "N of M drafted sentences
were refused and kept their local wording". A model cannot delete a finding —
not a `failed` one, not the fail-closed `cannot_run` statement — by returning
rubbish. Numbers never come from the model; only wording can.

A candidate is refused unless it carries **exactly** the markers the original
carried (no fewer, so a citation cannot be shed; no more, so the sentence
cannot re-attribute itself), holds no URL, and introduces no digit run that was
not already in the original or in a value it cites. That digit scan runs with
`readiness.cite`'s identifier exemptions **off**: to a validator reading a
document a five-digit number is a county FIPS and `2031` is a year, and both
are exactly what a model must not be able to introduce. The rewriter is never
shown a name in the first place, because no rule writes one.

Three files are written, in two trees:

- `<out>/<slug>/<period>.html` and `.json` — the plain report, for the
  facility's own use. The page carries a **legend** saying which building
  `PARTNER-1` is and which county `COUNTY-A` is.
- `<out>/blinded/<label>/<period>.blind.html` — the same document with no
  legend, no slug, no names, no county FIPS and **no timestamp**, for review by
  someone who is not told which building they are reading. This is the file a
  review record cites. It is in its own directory because a path is as good as
  a name: nothing under `blinded/` carries the slug in a directory name, a file
  name, the title or the meta tag.

Blinding is structural. The title and the footer are rebuilt from the labels,
each claim's text is cut where it stops describing a field and starts quoting
the record's name for it, and the claim ids that carried a county FIPS are
re-keyed along with the markers that cite them. `render_blinded` then checks
its own output: a page still holding the slug, a partner or co-tenant name, an
evidence document or a county FIPS raises `GapReportError` and **nothing is
written**. (A slug that is an ordinary English word cannot be removed from
prose by any mechanism, so such a record is refused rather than shipped
half-blinded; choose a distinctive slug.)

Because the blinded page carries no `generated_at`, the same record and period
render to the same bytes and the same sha every time, so re-running
`readiness gap-report` does not orphan the reviews of a report that has not
changed. The JSON and the plain HTML keep their timestamp.

`--period` is checked against `^\d{4}(-(Q[1-4]|M(0[1-9]|1[0-2])))?$` before
anything is read or written — it is a path component and it is markup — and
`readiness.issue.parse_period` agrees with it, which a test asserts.

The three files are written **atomically**: every string is rendered first,
each goes to a `.tmp` sibling, and the temporaries are swapped in only once all
of them are on disk. A failure part-way through leaves the tree exactly as it
was rather than a `.html` and a `.json` with no blinded page — which is the
state `verify --phase 3` would otherwise read.

`--out` defaults to `plans/reports/`. Exit 1, listing every violation, when
the document does not validate; exit 2 when the facility file cannot be
read (missing, invalid JSON, an unknown key, a place in a key or a value, or a
populated field with no evidence entry), when the period is not a period label,
or when a rule cannot be applied at all. Nothing is written on either failure.
**Real facility files and real gap reports never enter git** — `plans/reports/`
and `plans/reviews/` carry only their README, and `plans/facilities/` its
README and the one fictional example.

## Reviews: an attestation bound to one report

A review record is not a survey response; it is an attestation that a
specific blinded document was shown to a specific kind of reader.
`readiness review record` computes the sha256 of the file named by
`--report` and refuses unless it is a real blinded render (a `.blind.html`
file this command itself could have produced); the record is written to
`plans/reviews/<report sha256[:16]>-<attestation digest[:12]>.json` (or under
`--reviews DIR`), and the sha in that name *is* the binding — a review naming
a sha with no matching report is inert, and a report whose *content* changes
orphans every review of the version before it. The second half is a digest of
the record with its timestamp removed, so two practising emergency managers
reviewing one report — the normal case — get two files rather than the second
destroying the first, and recording the same attestation twice is idempotent.
`facility_label` is the `FACILITY-<12 hex>` the blinded page carries and its
shape is validated like the sha's. The rating vocabulary is closed to four values: `not useful`,
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
   `facility_label`, `blinded: true`, a `reviewer_role` containing "emergency
   manager", and a rating of `useful` or `very useful`.
2. **Reports.** Every `<period>.json` under `plans/reports/` (or `--reports
   DIR` — real reports are never committed, so a real run points this at the
   planner's own tree) is re-rendered with `render_blinded` and keyed by the
   sha of that render. A review counts only when its `report_sha256` is one of
   those shas **and** the render's `readiness-blind` tag says exactly
   `(facility_label, period, gap-report)`. Finding the document by content
   rather than by file name is what closes document → page → sha; by name, the
   machine-readable record behind a counted review could be replaced wholesale
   and this check would still have said "validates with zero violations". The
   document is then re-validated: the check prints the label, the first sixteen
   hex of the sha and the sentence and claim counts, and **never a path under
   the reports tree** — a line pairing a blinded label with
   `plans/reports/<slug>/…` is a de-blinding table, and this output gets pasted
   into pull requests.
3. **Case studies.** Every case study committed under `plans/case-studies/`
   reproduces its own `expected_findings` exactly. A study whose top level is
   not an object, or which carries an unknown key at any level, is a refusal
   that fails the check rather than a traceback.
4. **No coordinates.** No JSON file committed under `plans/` carries any of the
   keys `facility.FORBIDDEN_KEYS` lists (`address`, `lat`, `lon`, `zip`,
   `geometry`, `parcel` and the rest, quoted in full above), **or any string
   value** that reads as a street address, a ZIP+4 or a decimal-degree pair,
   anywhere in its structure.

What check 2's re-validation is, and is not: the facility record and the
scenario a real report was built from are the planner's, not this
repository's, and neither is committed — so nothing here can re-resolve
`power.fuel_hours` to a building. What it establishes is that the document is
internally sound: every sentence cites, every citation exists in the document,
every number is the rendered value of a cited claim, no forbidden phrasing, no
place named by a key or a value, and the kind is a gap report. A document that
passed these when it was written and fails them now has been edited since.

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

- **No address or coordinate, anywhere.** Not in the facility record (no such
  field exists, and both the key and the value scan say so out loud), not in
  the report, and not in the blinded render, which also carries no county FIPS
  and no timestamp.
- **No substituted intensity.** A missing elevation certificate is a
  `cannot_run` finding citing the guidance document that would resolve it,
  never a county-level or regional number standing in for the building's
  own.
