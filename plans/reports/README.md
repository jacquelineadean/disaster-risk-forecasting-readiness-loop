# plans/reports/

What `readiness gap-report` writes: `<slug>/<period>.html`, `.json` and
`.blind.html`.

* **`.json`** is the `cite.Document` the page was rendered from, so anyone can
  re-run `readiness.cite.validate` over the file and get the answer the command
  got before it wrote anything. A report with a violation is never written.
* **`.html`** is the page the planner reads: every sentence with its citation
  markers, then the sources those markers point at.
* **`.blind.html`** is the same page with the facility's slug replaced by
  `FACILITY-<6 hex of its sha256>` and every partner and co-tenant by
  `PARTNER-n`. That is the file a reviewer reads, and its sha256 is what
  `readiness review record` binds a rating to.

**Nothing here is committed.** `.gitignore` keeps the whole tree out of git but
this page. A gap report is a list of the ways one named building fails, and
`verify --phase 3` therefore takes `--reports DIR` so the three blinded reports
the exit criterion needs can live in the planner's own tree and still be
checked:

```
readiness verify --phase 3 --reports <dir with the blinded reports> --reviews <dir>
```

The blinding is a rename, not an anonymisation. It removes the names from the
page; it does not pretend that a reader who knows the county, the occupancy
type and the census could not guess. It exists so that a reviewer rates the
analysis rather than the institution, and so that a rating is bound to one
exact rendering — change a number and the sha moves, and every review of the
old rendering stops counting.
