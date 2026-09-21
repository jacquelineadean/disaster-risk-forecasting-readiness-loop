# plans/

The Phase 3 seed: the planning thought-partner's scenario library.

```
plans/scenarios/       hazard-agnostic stress tests a blessed plan must survive
plans/case-studies/    worked examples from published investigations (none by default)
plans/guidance.json    the named guidance documents a sentence may cite
```

Scenarios are written as specifications now, at Phase 0, so that when the
planning layer exists it is tested against something that was decided before
it was built. Each scenario lists its injects, the questions a plan must answer
with evidence, its pass condition, and a fail-closed rule for when the data
needed to run it is missing.

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
