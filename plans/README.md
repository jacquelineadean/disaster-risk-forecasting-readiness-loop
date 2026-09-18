# plans/

The planning thought-partner's committed data: the scenario library, worked
case studies and the guidance registry, plus the real, gitignored inputs and
outputs a live run produces. The code that reads all of this is
`readiness/plans/` (a different directory — see
[docs/plans.md](../docs/plans.md)); this one is what a reader or a reviewer
can look at directly.

```
plans/scenarios/       hazard-agnostic stress tests a blessed plan must survive: one markdown
                       file plus its JSON per scenario, the markdown the specification a test
                       keeps the JSON in step with
plans/case-studies/    worked examples from published investigations, one JSON file per event,
                       a source cited for every fact (none by default)
plans/guidance.json    the named guidance documents a sentence may cite
plans/facilities/      real facility records a planner supplies; gitignored except this directory's
                       README and one fictional example, because a real one names a real building
plans/reports/         the gap reports `readiness gap-report` writes — plain under <slug>/,
                       blinded under blinded/<label>/; gitignored except this directory's README
plans/reviews/         review records, each an attestation bound to one blinded report's
                       sha256; gitignored except this directory's README
```

Only `facilities/` carries a committed example. `reports/` and `reviews/`
carry a README and nothing else, and `tests/test_facility.py` and
`tests/test_reviews.py` assert that against `git ls-files` rather than a
filesystem glob — a glob misses a record in a subdirectory or one named
`.JSON`, which are exactly the files that would be committed by accident.

Scenarios are written as specifications now, at Phase 0, so that when the
planning layer exists it is tested against something that was decided before
it was built. Each scenario lists its injects, the questions a plan must answer
with evidence, its pass condition, and a fail-closed rule for when the data
needed to run it is missing. `readiness/plans/rules.py` answers each question
with a finding of status `answered`, `unanswered`, `failed` or `cannot_run` —
never a silent substitution when a fact (most importantly, the facility's own
design flood elevation or wind speed) is missing.

`facilities/`, `reports/` and `reviews/` hold the outputs and inputs of a
real run and are gitignored the way `issued/` and `briefs/` are — recursively,
at any depth and whatever the extension, because a facility record, a gap
report and a review all name or describe a real building or a real person's
attestation, and none of that belongs in a public repository by accident. Each
tree carries its own README; `facilities/` additionally carries one fictional,
clearly-marked example. A real run's files are committed deliberately when a
person chooses to, the same way a real brief or a real issued file is.

`guidance.json` is the registry of published guidance a brief or a gap report
may cite by id (FEMA CPG 101, 42 CFR 482.15, the ASPR TRACIE evacuation
toolkit, NFPA 110 and 99, the FEMA Elevation Certificate / FIRM, and NWS /
IPAWS as the only warning channel). Report §7 requires every sentence of
model prose to cite a computed number, a dataset row or a named guidance
document; `readiness/cite.py` resolves a `guidance` citation against this
file and nothing else, so prose cannot invent a standard, and the digits in
each entry's `aliases` ("CPG 101", "42 CFR 482.15") are treated as names
rather than as numbers that need a claim. The URLs are the publishers' pages
as best known; none is verified offline, and a citation is to the id, never
to the URL.
