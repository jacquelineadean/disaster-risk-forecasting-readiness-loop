# Global pilots

**Status: Phase 4.** This page documents the command surface exactly as
scoped in [`plan.md`](plan.md) §5. The code is `readiness/contracts.py` (the
two source fields, `_validate_sources`, `pilot_sources`), `readiness/config.py`
(`GROUND_TRUTH_SOURCES`, `HAZARD_CATEGORIES`, `normalise_hazard_value`),
[`readiness/connectors/geoboundaries.py`](../readiness/connectors/geoboundaries.py),
[`readiness/connectors/national_records.py`](../readiness/connectors/national_records.py),
[`readiness/connectors/emdat.py`](../readiness/connectors/emdat.py),
`readiness/harness/labels.py` (`RecordEvent`), `readiness/data.py` (the
pilot half of `build`) and `readiness/verify.py` (`phase4`).

Plan §5's claim is architectural, not a feature list: *the architecture does
not change, the connectors do.* Below the two inputs a contract names, the
Phase 1 queue, the temporal firewall, the leakage canary, the ledger, the
one atomic test touch and `readiness verify --phase 1` are the same code
running on the same objects whether the contract is `tornado-ok` or a
fictional flood contract in `ZZ`. [`tests/test_global.py`](../tests/test_global.py)
is written to prove exactly that claim, on a synthetic panel — nothing here
reaches the network, and nothing reads a real pilot's ground truth.

```bash
readiness register NAME --hazard H --country CC \
    --ground-truth national_records|emdat --records PATH \
    [--record-start-year YYYY] [--admin-level ADM1|ADM2] [--regions-release RELEASE]

readiness verify --phase 4     # no -c: reads the registry, not one contract
```

## What a pilot is

A **pilot** is any registered contract whose `country` is not `US`
(`Contract.is_pilot`). Nothing about it is a different code path in the
harness: it is a contract whose two source sections name a different ground
truth and a different region universe, and the same `readiness loop`,
`readiness promote` and `readiness verify --phase 1` run against it
unmodified. What is different is what a pilot is *not*: it carries no US
state in `scope.states` — outside the US the sub-national scope is
`regions.admin_level`, ADM1 or ADM2, not a state list — and it is excluded
from Phase 2's count of national contracts on purpose:
`readiness.fleet.national()` excludes `is_pilot` contracts explicitly,
because a pilot also has an empty `states` list and would otherwise satisfy
"national" by accident. A pilot is counted by `verify --phase 4`, never by
`verify --phase 2`.

## The contract's two source blocks

Every contract carries `ground_truth` and `regions`, each `{"source": ...}`
at minimum. Every contract registered before Phase 4 meant
`{"source": "storm_events"}` and `{"source": "census"}` implicitly, and
`Contract.criteria()` drops a source field from the hashed criteria when its
value equals that documented default (`SCHEMA_V1_DEFAULTS`) — so the schema
change moves no committed digest: a spec that spells the US defaults out
explicitly hashes identically to one that leaves them implicit, and every
contract registered before this phase (nine of them: three examples, six
national) still hashes to exactly what it did.

**In the US**, `_validate_sources` allows only `{"source": ...}` in each
section; naming anything else is refused ("in the US the record is a public
archive the connector pins for itself").

**Outside the US**, the same method requires, in full:

| field | rule |
|---|---|
| `ground_truth.source` | `national_records` or `emdat` — `storm_events` is refused ("Storm Events is a US record") |
| `ground_truth.file` | the records file's *basename* only (`BASENAME_RE`); a committed contract must never record one operator's filesystem |
| `ground_truth.sha256` | 64 lowercase hex digits — the criterion that pins the record's exact bytes |
| `ground_truth.record_start_year` | required unless the source carries a fixed floor (below) |
| `regions.source` | must be `geoboundaries` |
| `regions.admin_level` | `ADM1` or `ADM2` |
| `regions.release` | a non-empty string, e.g. `"gbOpen 6.0.0"` — boundary ids are renumbered between releases, so it is a hashed criterion like everything else |
| `scope.states` | must be empty |
| `zone_policy` | must be `drop` (there is no NWS crosswalk outside the US) |
| `hazard` | must be in `config.HAZARD_CATEGORIES` — `dust_storm` and `lightning` have no entry and cannot be registered outside the US at all: EM-DAT does not record either at a level that supports a region x period panel |

`readiness.contracts.pilot_sources(...)` is the one place that builds the
`(ground_truth, regions)` pair, so the CLI, a test fixture and anything
written later cannot disagree about which keys a pilot carries.
`Contract.record_start_year` reads `config.GROUND_TRUTH_SOURCES`: Storm
Events floors at 1996, EM-DAT at 2000 (CRED's own guidance is that pre-2000
entries are sparse and inconsistently geocoded), and a partner's national
record has no fixed floor at all — `None` there means only the contract can
say, because a partner's archive starts where their archive starts. No split
may begin before whichever year applies.

A pilot's `describe()` reads the same as a US contract, with two lines
where a US one has none:

```
contract        flood-zz 1.0.0  (sha256:36fd50ac6d56df77)
hazard          inland_flood  ['inland_flood', 'flood', 'flash_flood', 'riverine_flood', 'river_flood']
geography       ZZ, every region
ground truth    partner national records zz_records.csv (sha256:aaaaaaaaaaaaaaaa), from 2005
regions         geoBoundaries ADM1 for ZZ (gbOpen 6.0.0)
forecast unit   region x year
...
```

(a fictional worked example — no such contract is registered in this
repository).

## The three connectors

### geoBoundaries: the region universe

[`geoboundaries.py`](../readiness/connectors/geoboundaries.py) fetches gbOpen
(CC BY 4.0, William & Mary geoLab) GeoJSON from
`https://raw.githubusercontent.com/wmgeolab/geoBoundaries/{ref}/releaseData/gbOpen/{country}/{level}/geoBoundaries-{country}-{level}.geojson`
(`URL_TEMPLATE`), where `{ref}` is the release tag pulled out of
`regions.release` (`"gbOpen 6.0.0"` → `"6.0.0"`). `READINESS_GEOBOUNDARIES_URL`
overrides the whole template — for a mirror or a local file server — and the
substitution is still pinned by hash into the manifest, so it is visible
there rather than hidden in an environment variable. Every feature must carry
`shapeID` and `shapeName`; a release that stops doing so is schema drift and
an error, not a silently empty universe. `centroid_lat`/`centroid_lon` are
the arithmetic mean of the outer ring's vertices — **a lookup point for one
ERA5 series per region, not a legal centre or an administrative seat**, and
nothing publishes it as a location. [`tests/data/geoboundaries_sample.geojson`](../tests/data/geoboundaries_sample.geojson)
is the shape a real release has: a `FeatureCollection` whose features carry
`shapeName`, `shapeISO`, `shapeID`, `shapeGroup`, `shapeType` and a
`Polygon`/`MultiPolygon` geometry.

### Partner national records: the record that never leaves the partner's hands

[`national_records.py`](../readiness/connectors/national_records.py) reads a
CSV with exactly these columns:

```
event_id,start_date,region_id,hazard,deaths,injured,damage_usd,source
```

`start_date` is `YYYY-MM-DD`; `region_id` is a geoBoundaries `shapeID` at the
contract's admin level; `hazard` is normalised (`config.normalise_hazard_value`
— lowercased, spaces/hyphens/slashes folded to underscores) and matched
against `config.HAZARD_CATEGORIES[hazard]["national_records"]` — a row whose
value is not there is counted (`n_skipped_hazard`, printed by `readiness
panel`) and skipped, never guessed at. An optional first comment line,

```
# record_start_year: 2005
```

records where the archive itself begins; `readiness register` reads it so no
split can start before the record does. [`tests/data/national_records_sample.csv`](../tests/data/national_records_sample.csv)
is the agreed schema with fictional places and figures.

### EM-DAT: the record of last resort

[`emdat.py`](../readiness/connectors/emdat.py) reads a registered EM-DAT
export (CRED / UCLouvain, free for research, redistribution not permitted)
as an `.xlsx` — read with `zipfile` and `xml.etree` from the standard
library only, because the project has to keep running in Pyodide. Columns,
by header name (a reordering upstream is harmless; a renaming is loud):

```
DisNo., Disaster Type, Disaster Subtype, Start Year, Start Month, Country,
Admin Units, Total Deaths, No. Injured, Total Damage ('000 US$)
```

Damage is reported in thousands of US dollars (`DAMAGE_SCALE = 1_000.0`).
`Admin Units` is a JSON-ish list of objects —
`[{"adm1_code":101,"adm1_name":"Northern Province"}]`, sometimes with single
quotes, which `ast.literal_eval` reads where `json.loads` will not — and
naming the admin units in prose is exactly why a mapping to geoBoundaries ids
has to be a **committed crosswalk a person wrote**, never a fuzzy match at
run time: `snapshots/records/<cc>_emdat_regions.csv`, columns
`emdat_name,shape_id`
([`tests/data/zz_emdat_regions.csv`](../tests/data/zz_emdat_regions.csv) is
the shape). An admin name the crosswalk does not list is counted
(`n_unmapped_units`) and dropped, never spread across the whole country — a
national row marked everywhere would manufacture positives in districts that
saw nothing. [`tests/data/emdat_sample.xlsx`](../tests/data/emdat_sample.xlsx)
is a synthetic export in the real column shape.

## The records directory and the never-committed rule

A pilot's ground-truth file lives at `snapshots/records/<basename>`
(`data.RECORDS_DIRNAME = "records"`, `data.records_path`) — the contract
names the basename only, so the directory is policy and `records_path` is
where that policy lives. Three things enforce that the bytes stay there and
nowhere else:

1. **`.gitignore`** ignores everything under `snapshots/records/` except the
   directory itself and any `*_emdat_regions.csv` crosswalk — the one
   exception is ours to publish, because it is a person's own judgement about
   which geoBoundaries region an EM-DAT name means, and a panel cannot be
   reproduced without it.
2. **The site packer refuses by path, not by memory.** `tools/build_site.py`
   defines `PRIVATE_SNAPSHOT_DIR = "snapshots/records"`; `is_private()`
   raises `PrivateDataError` on anything physically under it — including the
   committed crosswalk file, since the guard is a path check, not a
   whitelist of what it remembered to exclude. `PRIVATE_KEY_PREFIXES =
   ("records/", "emdat/")` additionally strips every manifest record filed
   under those prefixes (and the partner filename inside it) out of the
   public manifest the sandbox archive ships, via `is_private_key()`.
3. **`verify --phase 4`'s `ground truth pinned` check** confirms each
   pilot's pinned hash still matches the sha256 its contract names — from
   `snapshots/manifest.json` alone, never by reading the bytes, because the
   check has to be able to run in a clone that has never seen them.

Registering a pilot computes the sha256 once, at registration time, from
whatever file `--records` names; nothing about the bytes is read again until
a real `readiness panel` or `readiness loop` run, and even then the file is
read in place and pinned, never copied.

## The label path: `RecordEvent`, and why the US panels do not move

`readiness/harness/labels.py` adds `RecordEvent` beside the existing
`StormEvent`, and `Event = StormEvent | RecordEvent`. A `RecordEvent` differs
in two ways that make the panel walk *simpler*, not special: the connector
already matched its hazard to the contract's hazard through
`config.HAZARD_CATEGORIES` (so `_hazard_matches` treats it as in by
construction — the filter is applied once, where the vocabulary lives), and
it names its own `region_ids` directly (geoBoundaries `shapeID`s — an EM-DAT
row can list several admin units for one flood), so nothing is zone-coded
and `zone_policy` never applies to it (the validator already requires `drop`
outside the US). `is_damaging` reads `damage_property_usd`, `injuries` and
`deaths` — the same field names on either event type — so the damage
definition a contract pre-registered is the definition a pilot is judged by,
unchanged.

`_walk_events`, `panel_and_diagnostics`, `build_panel` and `diagnose` are one
code path for both event types; nothing in them branches on which ground
truth built the panel except `_regions_hit`'s single `isinstance` check that
short-circuits a `RecordEvent` straight to its own `region_ids`. That is the
whole of the change, which is the point: **every US panel this project has
ever built is bit-identical**, because a `StormEvent` walk touches none of
the new code. `Diagnostics.region_coding` defaults to `"county"` and only
reads `"ADM1"`/`"ADM2"` off a pilot contract, so the printed diagnostics line
for a US contract is exactly what it always was.

## What is US-only, and refused by name

`data.US_ONLY_FEATURES = ("terrain", "nri")`. `terrain` reads the Census
Gazetteer (water share, latitude); `nri` reads FEMA's National Risk Index.
Neither exists outside the US, so `data._load_features` refuses a pilot that
asks for either **by name**, before anything is built: "there is nothing for
them to read" — a `ValueError` naming the blocked connector(s), not an empty
source handed to a model that would look like a real experiment.

In practice this means a pilot runs the Phase 1 queue on the
`era5-antecedent` feature set alone, plus the history-only baseline: the
history-only `logistic`, `logistic` on `era5-antecedent`, and
`logistic+iso` on `era5-antecedent`. The four candidates whose feature sets
need `terrain` (`logistic` and `logistic+iso` with
`era5-antecedent+terrain`, `gbm` and `gbm+iso`) are **skipped with a
progress line and no card** — "could not be run" is not an experiment, and
scoring one against a frame of missing values would look like one. A global
terrain source is the obvious next connector (geoBoundaries centroids carry
latitude; the ERA5 extract already carries elevation) and is deliberately
not invented here — see `tests/test_global.py::pilot_queue`'s own comment to
that effect.

## Registering a pilot

```bash
readiness register flood-zz --hazard inland_flood --country ZZ \
    --ground-truth national_records --records /path/zz_records.csv \
    --admin-level ADM2 --regions-release "gbOpen 6.0.0" --period year

readiness register cyclone-zy --hazard tropical_cyclone --country ZY \
    --ground-truth emdat --records /path/zy_emdat.xlsx --admin-level ADM1
```

`--country CC` (ISO 3166-1 alpha-2, default `US`) is the switch: outside the
US the contract must also name `--ground-truth` and `--records`, and inside
it `--ground-truth`/`--records` are refused outright ("in the US the ground
truth is NOAA Storm Events, which the connector pins for itself"). Other
Phase 4 flags:

| flag | meaning |
|---|---|
| `--ground-truth {emdat,national_records,storm_events}` | which record the labels come from; `storm_events` (the default) is refused outside the US |
| `--records PATH` | the partner CSV or EM-DAT `.xlsx`; its sha256 is computed **now**, at registration, and written into the contract — the file itself is never read again until a real run, and never committed or copied |
| `--record-start-year YYYY` | first year the record is complete; read from a partner file's `# record_start_year:` header when it has one, else required (EM-DAT falls back to 2000, `config.GROUND_TRUTH_SOURCES["emdat"]`) |
| `--admin-level {ADM1,ADM2}` | the geoBoundaries level a pilot's regions come from (default `ADM1`) |
| `--regions-release RELEASE` | the geoBoundaries release, e.g. `"gbOpen 6.0.0"` (default `contracts.GEOBOUNDARIES_RELEASE`) — a hashed criterion, because `shapeID`s change between releases |

After writing the contract, `readiness register` prints exactly where the
file has to go and never touches it itself:

```
note: place the records file at snapshots/records/<basename>
(sha256:<first 16 hex>...). It is never committed, never copied and never
packed into the browser sandbox; only its hash is, here and in
snapshots/manifest.json.
```

and, only for `--ground-truth emdat`, that the crosswalk
`snapshots/records/<cc>_emdat_regions.csv` (`emdat_name,shape_id`) has to be
written before a panel can be built.

## `readiness verify --phase 4`

Takes no `-c`; like Phases 2 and 3, its criteria are about the registry, not
one contract (`readiness verify --phase 4` with `-c NAME` is refused, naming
the reason). Four checks, in this order:

1. **`pilots`.** How many registered contracts outside the US
   (`Contract.is_pilot`) pass the Phase 1 checks (`verify.phase1` run
   against each pilot's own ledger) — the exit needs at least `MIN_PILOTS =
   2`.
2. **`global inputs`.** Every input named on every passing pilot's test card
   (`data_snapshot.inputs` plus `feature_inputs`) resolves, through
   `readiness.connectors.connector_for_key`, to a connector with
   `global_coverage=True`. The check first asserts that none of
   `US_ONLY_CONNECTORS` (`census`, `storm_events`, `nws_zones`, `nri`,
   `usa_structures`, `gazetteer`) is wrongly marked global — so the check
   itself cannot pass vacuously — before it reads a single card.
3. **`ground truth pinned`.** Each pilot's pinned record hashes to the
   sha256 its contract carries as a criterion, read from
   `snapshots/manifest.json` alone (see [above](#the-records-directory-and-the-never-committed-rule)).
4. **`us digests`.** The three US example contracts (`inland-flood-la`,
   `tornado-ok`, `tropical-cyclone-gulf`) still hash to exactly what their
   blessed `harness_expected/*.json` fingerprints record — proof, not
   assertion, that the two new schema fields are elided at their US
   defaults.

Exit 0 only when all four hold. Nothing here builds a panel or opens a
partner's file: it reads committed ledgers, committed fingerprints and the
committed manifest, which is what lets it run in a clone that has never seen
a pilot's ground truth at all.

## Partner use, as of 2026-09-18

As documented today: **partner ground-truth bytes are never committed,
packed or published.** A partner file is referenced by sha256 and basename
only — in the contract (`ground_truth.sha256`, `ground_truth.file`) and in
`snapshots/manifest.json`. Nothing in this repository, the browser sandbox
or the published website has ever held, or will hold, the contents of a
partner's national record or an EM-DAT export; only the crosswalk a person
writes for EM-DAT (`snapshots/records/<cc>_emdat_regions.csv`) is ours to
commit, because it names no event, only which geoBoundaries region an
EM-DAT admin name means.

## What Phase 4 still owes

Everything above is provable offline, and `tests/test_global.py` proves it
on a synthetic panel. What it cannot prove — because this build session has
no network — is the data run: pulling a real geoBoundaries release, placing
a real partner file or EM-DAT export (with its crosswalk) at
`snapshots/records/`, running the ERA5 pulls for two candidate countries,
and finding out whether two pilots actually pass. The geoBoundaries URL
pattern and the EM-DAT column names above come from published documentation
and are **confirmed on the first real pull**, not before. No real pull has
been made against this branch: the nine contracts registered before Phase 4
(three examples, six national), their ledgers, their blessed fingerprints
and `snapshots/manifest.json` are all untouched by this phase's diff.
