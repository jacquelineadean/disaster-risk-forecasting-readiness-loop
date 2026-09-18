# Assessor counts for the exposure spot-check

Plan §3, Phase 2 exit: "exposure joins spot-validated against county assessor
counts in ten sampled counties". This directory holds the person-collected
half of that check. `assessor_counts.csv` **ships header-only**: no count is
committed until a person has collected it and can say where it came from.
`readiness exposure spot-check` reads it, divides our pinned USA Structures
total for each county by the assessor's number, prints every ratio, and
passes only when at least **ten counties from at least three states** fall
inside the band declared in `readiness/config.py` (`EXPOSURE_SPOTCHECK_RATIO`,
`0.67`–`1.5`). A row outside the band is printed and does **not** count toward
the ten; it is never edited to fit.

## How to collect a count

Pick counties you have no reason to prefer: different states, a mix of urban
and rural, and ideally counties already covered by a pinned extract
(`snapshots/usa_structures/<st>_counts.jsonl`). For each one:

1. Find the county assessor's (or appraiser's, or property-tax office's)
   open-data page. Most publish a parcel or property table; some publish a
   building or improvement table. Prefer an official page over a third-party
   aggregator.
2. Record the number and **what it counts**. `count_definition` is one of:
   - `structures`: a count of buildings or improvements (each building counted
     once).
   - `improved_parcels`: a count of parcels with at least one improvement.
     This is what most assessors publish, and it is *not* a structure count:
     one parcel can carry a house, a garage and a barn, and one apartment
     parcel can carry a dozen buildings. That difference is why the band is
     wide.
3. Record the exact URL you read the number from (`source_url`) and the
   retrieval date (`retrieved_on`, `YYYY-MM-DD`). Assessor pages move and
   numbers change; without the date and the URL the row cannot be checked.
4. Put anything a reader needs in `notes` (which table, which filter, "as of"
   date the assessor states, whether mobile homes or exempt property are
   included). `notes` is informational; nothing reads it.

Then add the row:

```
fips,assessor_count,count_definition,source_url,retrieved_on,notes
22071,146712,improved_parcels,https://example.invalid/assessor/summary,2026-09-16,"Orleans Parish, improved parcels excluding exempt"
```

`fips` is the five-digit county FIPS (state + county). One row per county;
a duplicate FIPS, a non-numeric count, a `count_definition` outside the two
values above, a non-http URL or a malformed date is refused by the loader.

## What the check does and does not establish

It establishes that the join is real: that the counts we pinned per county
are the same order of magnitude as an independent count, in enough places
and states that one convention cannot carry the result. It does not
establish that either number is exact, and the ratios are printed so a
reader can see the spread rather than a verdict. Nothing finer than the
county appears here or anywhere in the exposure layer (report §7: publish
county aggregates only).
