# Features and the temporal firewall

Phase 0 models saw nothing but units: a region, a year, a period. Phase 1
models see covariates — antecedent rainfall, terrain, a prior risk layer —
and every covariate is a new way for the future to leak into a forecast. A
feature computed over the wrong months, or from a layer that was published
after the years it is asked to forecast, produces a backtest that looks
excellent and means nothing.

So the harness owns the feature channel
([`readiness/harness/features.py`](../readiness/harness/features.py)), and
this page is the firewall in the reader's terms: what a source may hand over,
who computes the cutoff, which transforms exist, what is refused and why,
what the audit checks, and what the harness proves as opposed to trusts.

## Who does what

**A source hands over raw material only.** A connector gives the harness a
monthly series per region (`era5`: precipitation and mean temperature) or a
static table per region (`gazetteer`: latitude, water share; `elevation`;
`nri`; `climada`). A source never sees a unit and never decides a cutoff. It
cannot, for instance, hand over "the rainfall before this quarter": it hands
over the whole series and the harness cuts it.

**The harness computes every cutoff itself.** For a unit `(region, year,
period)` the cutoff is *the first month of the period minus the spec's lag*.
Only months strictly before the cutoff reach a transform. With the minimum
lag of one month, a forecast for the quarter beginning in July is built from
data through May: June is withheld, because the month just before a period
is never complete by the time the period starts — reanalysis and reporting
both run late. Lag zero is refused at spec construction for that reason, not
because it would admit the period's own months (those are always out).

**A model declares, it does not construct.** A Phase 1 model names feature
*sets* from the engine's catalogue
([`readiness/engine/features.py`](../readiness/engine/features.py)) and reads
the matrix the harness hands back. It never touches a series and never
writes a transform. The agent, likewise, proposes sets by name; it never
authors a source or a transform (see
[`skills/verification-protocol.md`](../skills/verification-protocol.md)).

## The closed vocabulary of transforms

A transform is the only code that ever touches a series, and there are
exactly these. Each receives the series already cut at the cutoff, and the
audit (below) checks that being handed more would have made no difference.

| transform | meaning |
|---|---|
| `trailing_sum` | the sum of the `window_months` months ending just before the cutoff; missing if any of them is missing |
| `trailing_mean` | the mean of those months, under the same completeness rule |
| `trailing_max` | the largest of those months |
| `same_period_mean` | the mean, over the `window_months / 12` prior years, of the series summed over this period's own months in each of those years — "how wet is this quarter, usually"; a prior year's period that is not entirely before the cutoff is missing, not truncated |
| `static` | one value per region from a static table; no window, no lag (there is no time axis to lag along) |

A missing value is NaN, never zero: how to treat a gap is the model's
decision, not the matrix builder's.

## The catalogue

Column names are unique across sets, so any combination concatenates into one
frame. All series columns lag one month.

| set | columns | source |
|---|---|---|
| `era5-antecedent` | `precip_1m`, `precip_3m`, `precip_12m` (trailing sums), `precip_same_10y` (same-period mean over ten prior years), `tmean_3m` (trailing mean) | ERA5 monthly extract via Open-Meteo |
| `terrain` | `elevation_m`, `water_share`, `lat` | Open-Meteo elevation; Census Gazetteer |
| `nri` | `nri_eal`, `nri_risk` | FEMA National Risk Index — **refused under every current contract**, on purpose (below) |
| `climada-prior` | `climada_rp10`, `climada_rp50`, `climada_rp100` | a pinned CLIMADA layer, admissible only when its event set ends before the first validate year |

`readiness features -c NAME` prints, for the sources that are loaded, the
admission verdict of each, the columns of every set they can build, and the
audit over the contract's validate units.

## Admission: what is refused before anything is built

`admit(source, contract)` applies three rules, in order.

**1. Nothing built from the ground truth.** A source whose pinned inputs
include a label-origin key (`noaa/storm_events/`, `records/`, `emdat/`,
`desinventar/`) is refused outright. A feature derived from the labels is the
leak the harness exists to stop. History features that a model wants from
its own training labels — the shrunk seasonal rate — are computed inside
`fit()` from the `TrainingView`, leave-one-year-out for training rows, where
the split already protects them.

**2. A static layer must predate the holdout.** A static source declares
`derived_through`, the last calendar year of data it encodes. If that year is
not before the contract's first validate year, the layer is refused. The
example that ships: FEMA's National Risk Index v1.20 encodes data through
2023, and every current contract starts validating in 2016. A risk index
published in 2024 knows about the 2016–2023 floods it would be asked to
forecast; scoring it on those years would be a backtest of hindsight. So
`readiness features --features nri` prints the refusal as a **finding** and
exits 0 — the refusal is the firewall working on real data, which is exactly
what the demonstration is for. A layer that would be admissible is one
built from inputs ending in 2015 or earlier (the CLIMADA recipe says how:
[`skills/climada-recipe.md`](../skills/climada-recipe.md)).

**3. "Timeless" is an allow-list, not a claim.** A static source may declare
no year only if it encodes physical geometry that no event changes — where a
county is, how high it sits, how much of it is water. The allow-list is
harness-owned (`gazetteer`, `elevation`); anything else that declares no
year is refused rather than trusted.

## The poisoned-cutoff audit

Admission checks the sources. The audit checks the transforms, mechanically,
on every scoring call and before any fit:

1. **admission** — every source the specs use, re-checked and reported.
2. **timestamp bound** — every value in the frame is rebuilt from the *uncut*
   series in which every month at or after the unit's cutoff has been
   replaced by a poison value (`1e15`). A transform that reads any month past
   the cutoff produces a visibly absurd number and the rows differ; one that
   does not is bit-identical. This guards against a future transform that
   ignores the slice it was handed — the negative test injects exactly such
   a transform and watches the audit catch it.
3. **coverage** — the share of missing values per column, for the reader. A
   mostly-missing column is a finding about the data, not a refusal.

An unclean audit raises before `fit()`; the model never sees the frame. The
audit's findings are recorded on the card's scorecard (`scorecard.feature_audit`,
alongside `scorecard.feature_columns` and `scorecard.feature_digest`; the
constructor arguments and, when sources were loaded, the feature manifest
digest and keys live separately under `data_snapshot`), and the canary's
fifth check compares the feature digest the model declares against the
digest of the frame the harness actually handed it.

## What the harness proves, and what it trusts

*Proves:* temporal precedence, from cutoffs it computed itself; label origin,
from manifest keys it reads itself; that no transform reads past its cutoff,
by the poisoning above; that the frame a model was fitted on is the frame it
claims (canary check 5).

*Trusts:* the `derived_through` year a static connector declares. It is a
reviewed constant in the connector, pinned into the manifest record's notes,
and a wrong year is a wrong review — not something the harness can detect
from the bytes. That is the honest extent of "the harness can check it", and
it is why the allow-list of timeless sources is short and hand-written.

## Lag is the operational lead

`lag_months >= 1` is enforced when a spec is constructed, so the backtest
uses exactly the information that would be available before a period
starts. The same rule an issuance would have to live by — forecast the
quarter from what is complete before it begins — is the rule the backtest
is scored under. A feature that would improve the backtest by shrinking the
lag is not a better feature; it is a forecast made after the fact.
