# plans/reviews/

One JSON per review, named by the sha256 of the blinded report it rates:

```json
{
  "report_sha256": "<sha256 of the .blind.html file>",
  "facility_hash":  "FACILITY-<6 hex>",
  "period":         "2026-Q4",
  "reviewer_role":  "practising emergency manager",
  "organisation_type": "hospital",
  "years_in_role":  12,
  "rating":         "useful",
  "blinded":        true,
  "comments":       "",
  "recorded_at":    "2026-09-18T00:00:00+00:00"
}
```

Written by:

```
readiness review record --report <path>.blind.html --rating useful \
  --role "practising emergency manager" --org-type hospital --years 12
```

`record` reads the file, refuses anything that is not a blinded rendering of a
gap report, recomputes its sha256 and names the record after it. The rating
vocabulary is closed — `not useful`, `somewhat useful`, `useful`,
`very useful` — because a free-text rating could not be counted and a scale
without a bottom rung would not be a review.

## What this can and cannot establish

Plan §4 says it plainly: nothing here can establish that a facility is real or
that a reviewer is a practising emergency manager. A review record is an
**attestation**, and `verify --phase 3` prints that line every time it runs.

What it does establish is that a rating cannot float free of what it rates.
The sha is recomputed from the file when the review is recorded, and
`verify --phase 3` re-finds a blinded render with exactly that sha, and
re-validates the report beside it, before it counts the review. Change one
number in a report and every review of it stops counting.

**Review records are not committed.** `.gitignore` keeps `*.json` here out of
git: a record carries a named person's judgement about a named institution's
plan. `verify --phase 3 --reviews DIR` reads them from wherever the planner
keeps them.
