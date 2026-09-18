# plans/reviews/

One JSON per review, named
`<report sha256[:16]>-<attestation digest[:12]>.json`:

```json
{
  "report_sha256":  "<sha256 of the .blind.html file>",
  "facility_label": "FACILITY-<12 hex>",
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

The first half of the file name is the binding. The second is a digest of the
record with `recorded_at` removed, so **two reviews of one report coexist** —
two practising emergency managers reviewing one report is the normal case, and
naming the file after the report alone made the second destroy the first — and
recording the same attestation twice is idempotent rather than a second file.

`facility_label` is the `FACILITY-<12 hex>` the blinded page carries, and its
shape is validated the way `report_sha256`'s is. It is the first twelve
characters of the record's own random `blind_id`, not a digest of the slug: a
six-hex digest of a human-chosen slug is a dictionary search away from the
building's name, and this is the identifier the blinded artefact carries.

Written by:

```
readiness review record --report <path>.blind.html --rating useful \
  --role "practising emergency manager" --org-type hospital --years 12
```

`record` reads the file, refuses anything that is not a blinded rendering of a
gap report (a directory or an unreadable file included, with the path in the
message), recomputes its sha256 and names the record after it. The rating
vocabulary is closed — `not useful`, `somewhat useful`, `useful`,
`very useful` — because a free-text rating could not be counted and a scale
without a bottom rung would not be a review.

## What this can and cannot establish

Plan §4 says it plainly: nothing here can establish that a facility is real or
that a reviewer is a practising emergency manager. A review record is an
**attestation**, and `verify --phase 3` prints that line every time it runs.

What it does establish is that a rating cannot float free of what it rates.
The sha is recomputed from the file when the review is recorded, and
`verify --phase 3` re-renders every gap-report document under the reports tree,
finds the one whose render has exactly that sha, checks that the render's own
`readiness-blind` tag names this review's `facility_label` and `period`, and
re-validates it — before it counts the review. Change what a report *says* and
every review of it stops counting; re-run the command on an unchanged record
and nothing moves, because the blinded page carries no timestamp.

**Review records are not committed.** `.gitignore` keeps this whole tree out of
git but this page, at any depth and whatever the extension: a record carries a
named person's judgement about a named institution's plan.
`verify --phase 3 --reviews DIR` reads them from wherever the planner keeps
them.
