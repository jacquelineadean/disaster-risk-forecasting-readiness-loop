# plans/reports/

What `readiness gap-report` writes, in two trees:

```
<slug>/<period>.json           the cite.Document the pages were rendered from
<slug>/<period>.html           the page the planner reads, with the legend
blinded/<label>/<period>.blind.html    the page a reviewer reads
```

* **`.json`** is the `cite.Document` the pages were rendered from, so anyone can
  re-run `readiness.cite.validate` over the file and get the answer the command
  got before it wrote anything. A report with a violation is never written.
* **`.html`** is the page the planner reads: every sentence with its citation
  markers, a **legend** saying which building `PARTNER-1` is and which county
  `COUNTY-A` is, then the sources those markers point at.
* **`.blind.html`** is the same document with no legend, no slug, no partner or
  co-tenant name, no county FIPS and no timestamp. It sits under
  `blinded/<label>/` rather than beside the plain pair, because a directory
  named after the slug holding the blinded page next to the named one
  de-blinds the report without a hash being computed. Nothing under `blinded/`
  carries the slug in a directory name, a file name, the title or the meta tag.
  That is the file a reviewer reads, and its sha256 is what `readiness review
  record` binds a rating to.

**Nothing here is committed.** `.gitignore` keeps the whole tree out of git,
at any depth and whatever the extension, but this page. A gap report is a list
of the ways one named building fails, and `verify --phase 3` therefore takes
`--reports DIR` so the three blinded reports the exit criterion needs can live
in the planner's own tree and still be checked:

```
readiness verify --phase 3 --reports <dir with the reports> --reviews <dir>
```

That check re-renders every `<period>.json` under the tree and finds a review's
report by the sha of the render, never by the file's path: a JSON found by
name could have been replaced after the review with a document saying the
opposite of the page the reviewer rated. It prints the blinded label, the first
sixteen hex of the sha and the sentence and claim counts, and never a path
under this tree.

## What the blinding is, and what it is not

Blinding is **structural**. No rule writes a name: a sentence says `PARTNER-2`,
`COUNTY-A` or `DOCUMENT-3`, the name lives in the claim that sentence cites,
and the blinded render drops the naming tail of each claim, rebuilds the title
and footer from the labels, and re-keys the claim ids that carried a county
FIPS. `render_blinded` then checks its own output and refuses — writing
nothing — if the slug, a name, an evidence document or a county FIPS survived.

It is still not an anonymisation. It removes the names, the county and the
timestamp from the page; it does not pretend that a reader who knows the
occupancy type, the census and the reserves could not guess. It exists so that
a reviewer rates the analysis rather than the institution, and so that a rating
is bound to one exact rendering — change what the report *says* and the sha
moves, and every review of the old rendering stops counting.

Because the blinded page carries no `generated_at`, re-running the command on
an unchanged record produces the same bytes and the same sha, so a report that
has not changed does not orphan its reviews.
