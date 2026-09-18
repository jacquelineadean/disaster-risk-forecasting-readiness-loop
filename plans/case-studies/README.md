# Case studies

Worked examples of the scenario library's tests, one JSON file per real
event, checked by `readiness scenarios check`. None ships by default: a case
study is an *example*, added deliberately, and every fact in it must cite a
published investigation, an after-action report or a court or regulatory
record — a fact with no source fails the schema and the file is refused, not
silently accepted.

## The JSON shape

```jsonc
{
  "event": "a short name for the event, with its date",
  "hazard": "the hazard, matching a value from readiness hazards",
  "facility_type": "hospital | nursing_home | shelter | school | other",
  "scenario": "the scenario id this case study is mapped onto, e.g. 96h-isolation-acute-care",
  "sources": [
    {"title": "...", "publisher": "...", "year": 2021, "url": "...", "note": "what this source establishes"}
  ],
  "facility_as_recorded": {
    "...": "a partial facility record — only the fields the public record actually supports,\
            each one naming an entry under evidence, the same schema readiness/plans/facility.py reads"
  },
  "expected_findings": {
    "<question id, from the scenario>": "answered | unanswered | failed | cannot_run"
  }
}
```

Every fact under `facility_as_recorded` must have a matching `evidence` entry
whose `source_doc` names one of the entries in `sources` — the same rule a
real facility record follows, so a case study is tested through the *same*
rules a live facility goes through, not a special-cased shortcut. Loading a
case study with a fact that names no source, or naming a source that is not
listed under `sources`, is refused rather than silently skipped.

`readiness scenarios check` runs `readiness/plans/rules.py` against
`facility_as_recorded` and the scenario named by `scenario`, and compares
the resulting finding for every question to `expected_findings`. A mismatch
is reported by question id, with both statuses, so a case study that
regresses is a specific, readable failure — this is report §6's promise that
"case studies become regression tests" made literal.

A case study file should, in its sources and its mapping onto the scenario,
not only its JSON structure:

1. name the event, the facility type and the hazard, with dates;
2. map what happened onto the injects of a scenario in
   [`../scenarios/`](../scenarios/), hour by hour where the record allows;
3. answer, from the record, each question the scenario asks — and say plainly
   where the record is silent (an unanswered question is `unanswered`, not
   omitted);
4. list its sources in full, with enough detail that another reader could
   find the same passage.

Case studies are used here as the engineering standard they deserve to be,
and any product surface that renders one should say the same.
