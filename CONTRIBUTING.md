# Contributing

The Readiness Loop is a pure standard-library Python package with a test
suite that needs no network. This page is the short version of how changes
land. The design and the phase plan are in `docs/`, and `CLAUDE.md` holds the
same rules in the form a Claude Code session reads first.

## Set up

```bash
make install   # pip install -e ., no runtime dependencies
make test      # the whole suite, no network
ruff check .   # the linter configured in pyproject.toml
```

CI (`.github/workflows/test.yml`) runs the suite on Python 3.10 and 3.12 and
builds the site without its data sandbox, on every push and pull request.

## What a change may not do

- Move a committed artefact. The ledgers in `experiments/`, their anchors, the
  blessed fingerprints in `harness_expected/` and the contract digests are the
  ground truth; `tests/test_repro_guard.py` re-checks them on every run.
- Give a model a holdout label, or let the harness call a language model.
  `tests/test_boundaries.py` reads the import graph.
- Spend a test touch outside `readiness promote`, or spend one on a baseline.
- Commit a partner's bytes, a real facility record, a gap report or a review.

## Pull requests

One concern per pull request, opened as a draft, with a body that has:

- **Summary**: what changed and why, by commit where that helps.
- **Verification**: the commands run and what they showed.
- **Stack**: see below, whenever the pull request depends on another.

Commits: one coherent change each, a subject that names the change, a body
that says why. The repository's history is written this way throughout.

### Stacked pull requests

A change that builds on an unmerged pull request does not wait for it. Stack
only when the work depends on that pull request; otherwise branch from
`main`.

1. Branch from the head of the pull request you depend on.
2. Open the new pull request with its base set to that branch, so the diff
   shows only the new work.
3. Name the stack and the merge order in every body.
4. A fix to a lower pull request goes on its branch; rebase the branches
   above it onto the new head (`git rebase --onto`) and push them with
   `--force-with-lease`. Rebase only branches you own.
5. Merge from the bottom, one at a time. After each merge, delete the merged
   branch so GitHub retargets the next pull request to `main`, or retarget it
   by hand; confirm the base reads `main`; then merge the next. Never merge
   two pull requests of a stack within the same minute: docs/plan.md §6.1
   records what happened when #12, #13 and #14 were.

## Real-data runs

`.github/workflows/real-data.yml` is the only place real data is pulled and
scored. It rehearses by default and spends a contract's one test touch only
with `promote` on. The rules, the per-contract loop and the register of every
dispatch are in docs/plan.md §7. Read it before dispatching anything.

## Licence

Apache-2.0 for the code (`LICENSE`); the data layers carry their own terms
(`DATA-LICENSES.md`). Cite the tagged release you used (`CITATION.cff`).
