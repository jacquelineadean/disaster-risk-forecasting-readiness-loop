# plans/

The Phase 3 seed: the planning thought-partner's scenario library.

```
plans/scenarios/       hazard-agnostic stress tests a blessed plan must survive
plans/case-studies/    worked examples from published investigations (none by default)
```

Scenarios are written as specifications now, at Phase 0, so that when the
planning layer exists it is tested against something that was decided before
it was built. Each scenario lists its injects, the questions a plan must answer
with evidence, its pass condition, and a fail-closed rule for when the data
needed to run it is missing.
