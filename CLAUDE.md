# CLAUDE.md

Project memory for Claude Code sessions in this repository: the rules a
session follows before it reads anything else. The long form of each rule
lives where the rule points. Keep this file short.

## Stacked pull requests

Work that builds on a pull request that has not merged does not wait for the
merge. It is stacked:

1. Branch from the head of the pull request you depend on, never from `main`.
2. Open the new pull request as a draft with its **base set to that pull
   request's branch**, so its diff shows only the new work. One concern per
   pull request; each must be reviewable on its own.
3. Put the stack in every body under a `## Stack` heading: the merge order,
   with the pull request numbers, and which one this is.
4. A fix to a lower pull request goes on that pull request's branch as a new
   commit. Then rebase every branch above it onto the new head
   (`git rebase --onto <new head> <old head> <branch>`) and push each with
   `--force-with-lease`. A branch this session created is its own to rebase;
   a branch someone else is working on is never rewritten.
5. Merge from the bottom, one at a time. After a merge, delete the merged
   branch so GitHub retargets the next pull request to `main`, or retarget it
   by hand; confirm the base reads `main`; only then merge the next. Never
   merge two pull requests of one stack within the same minute: #12, #13 and
   #14 were merged forty seconds apart and #13 and #14 landed sideways on
   their base branches (docs/plan.md §6.1).
6. Stack only when the new work depends on an open pull request. Work that
   does not depend on one branches from `main` and targets `main`.
7. Every run of `real-data.yml`, rehearsal or promote, is dispatched from
   `main`: a cache is scoped to the branch that saved it and is evicted after
   seven days unused, so a pull made from a stack branch cannot resume from
   `main` (docs/plan.md §7.1 and §7.3).

## Working rules

- Before opening a pull request, fetch and read the open pull requests and
  the `claude/*` branches: another session may already hold the change (#20
  and #21 carried the same five commits on the same day). Open the pull
  request from the branch that has the work rather than duplicating it.
- `make test` runs the suite with no network and no dependencies; it and
  `ruff check .` are clean before every push. CI runs the suite on Python
  3.10 and 3.12 and builds the site.
- The committed artefacts are ground truth and never move: every
  `experiments/*/ledger.jsonl` and its anchor, every `harness_expected/*.json`,
  every contract digest. A new per-card field goes inside `data_snapshot`; a
  new contract field is elided from the digest at its default
  (docs/plan.md §0). A scratch run points `READINESS_EXPERIMENTS_DIR`
  elsewhere rather than appending to a committed ledger.
- Nothing in `readiness/harness/` calls a language model; the engine never
  imports the judge or the canary; `tests/test_boundaries.py` enforces the
  import rules by reading the AST.
- Each contract has one test touch, spent only by `readiness promote`, never
  on a baseline, never twice. A real-data run is rehearsed first (`promote`
  off), its artifact is committed whatever the verdict, and every dispatch
  gets a register row in docs/plan.md §7.4, failures included.
- Never commit a partner's or EM-DAT's bytes under `snapshots/records/`
  (only `??_emdat_regions.csv`), nor a real facility record, gap report or
  review under `plans/`. The `.gitignore` and the tests guard both; do not
  weaken either.
- Commit style: one coherent change per commit; a subject that names the
  change; a body that says why. A pull request body carries a Summary, a
  Verification section with the commands run, and the Stack section above.
