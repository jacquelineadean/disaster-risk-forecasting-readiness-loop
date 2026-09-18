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
    [--record-start-year YYYY] [--admin-level ADM1|ADM2] \
    [--regions-release RELEASE] [--regions-sha256 HEX]

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
| `ground_truth.file` | the records file's *basename* only (`BASENAME_RE`); a committed contract must never record one operator's filesystem. A basename shaped like the committed crosswalk (`??_emdat_regions.csv`, `CROSSWALK_BASENAME_RE`) is refused, because that is the one name under `snapshots/records/` that git would commit |
| `ground_truth.sha256` | 64 lowercase hex digits — the criterion that pins the record's exact bytes |
| `ground_truth.record_start_year` | required unless the source carries a fixed floor (below); a JSON **integer**, never coerced — `"2005"` and `2005` would be the same contract under two digests, so the string is refused |
| `regions.source` | must be `geoboundaries` |
| `regions.admin_level` | `ADM1` or `ADM2` |
| `regions.release` | `"gbOpen <version>"` (`RELEASE_RE`, the same pattern `geoboundaries.release_ref` reads), checked here rather than at build time: a contract is a pre-registered, hashed artefact, so `--regions-release latest` must not reach a digest and then fail every build |
| `regions.sha256` | **optional**: the boundary file's own sha256. Absent, the field is not written and an existing pilot's digest does not move; present, it is a criterion and `geoboundaries.load` refuses other bytes exactly as `national_records.load` refuses another record — the release names an *edition*, and a mirror can serve anything under that name |
| *(nothing else)* | both sections refuse an unknown key, exactly as the US branch does. `to_spec()` writes them back verbatim, `criteria()` hashes them and the site publishes them, so an extra key — an operator's absolute path, say — would be committed, packed and published |
| `scope.states` | must be empty |
| `event_types` | forced empty. Outside the US there is no Storm Events vocabulary to name, so `Contract.from_spec` drops the field whether or not a spec carries one, and `readiness register` refuses `--event-type` as US-only. Hashing NOAA's strings into a pilot's digest would mark two pilots with identical panels incomparable for nothing |
| `zone_policy` | must be `drop` (there is no NWS crosswalk outside the US) |
| `hazard` | must be in `config.HAZARD_CATEGORIES` **with a mapping for this contract's `ground_truth.source`** — a hazard mapped for partner records and not for EM-DAT is refused for an EM-DAT contract, at registration rather than at build time. `readiness hazards` prints which catalogue hazards qualify (`config.global_hazards()`); `dust_storm` and `lightning` have no entry and cannot be registered outside the US at all, because EM-DAT does not record either at a level that supports a region x period panel |

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
contract        flood-zz 1.0.0  (sha256:69a29281ed1a3b7b)
hazard          inland_flood  ['inland_flood', 'flood', 'flash_flood', 'riverine_flood', 'river_flood']
geography       ZZ, every region
ground truth    partner national records zz_records.csv (sha256:aaaaaaaaaaaaaaaa), from 2005
regions         geoBoundaries ADM1 for ZZ (gbOpen 6.0.0)
forecast unit   region x year
...
```

(a fictional worked example — no such contract is registered in this
repository. It is `tests.fixtures.make_pilot_contract(sha256="a" * 64,
period="year")`, and `tests/test_docs.py` recomputes the digest quoted above
from that fixture, so this block cannot drift into a digest nobody can
reproduce.)

## The three connectors

### geoBoundaries: the region universe

[`geoboundaries.py`](../readiness/connectors/geoboundaries.py) fetches gbOpen
(CC BY 4.0, William & Mary geoLab) GeoJSON from
`https://raw.githubusercontent.com/wmgeolab/geoBoundaries/{ref}/releaseData/gbOpen/{country}/{level}/geoBoundaries-{country}-{level}.geojson`
(`URL_TEMPLATE`), where `{ref}` is the release tag pulled out of
`regions.release` (`"gbOpen 6.0.0"` → `"6.0.0"`). `READINESS_GEOBOUNDARIES_URL`
overrides the whole template — for a mirror — and the substitution is still
pinned by hash into the manifest, so it is visible there rather than hidden
in an environment variable. **The override must be `https`**: the region
universe decides which places exist in a pilot's panel, and it is not fetched
over a transport anybody on the path can rewrite. A contract that also names
`regions.sha256` refuses bytes that are not the ones it was registered
against, whoever served them, and a boundary file over `MAX_BYTES` (512 MB)
is refused unparsed.

Every feature must carry `shapeID` and `shapeName`; a release that stops
doing so is schema drift and an error, not a silently empty universe. The
level and the country are checked against what was *asked for*, not only
against what was requested upstream: a feature whose `shapeType` is not the
contract's `admin_level`, or whose `shapeGroup` is not its country, is
refused (the check is skipped where the property is absent, so an older
release still parses). Without it an ADM3 file admitted as a pilot's ADM1
universe would publish sub-county names in the site's region lists, and a
mirror template with no `{level}` field would serve one file for both levels
and pin the same sha256 under two manifest keys.
`centroid_lat`/`centroid_lon` are
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

Only the **leading** comment block is stripped, and row numbers in a refusal
are the file's own line numbers. Filtering every `#` line would delete a data
row whose `event_id` is a case reference written `#2006/0012` — counted in
neither `n_rows` nor `n_skipped_hazard`, a silent zero in the panel — and
would cut the continuation line of a quoted field, leaving the quote
unterminated and swallowing the row after it. Every numeric cell must be
finite: `nan` is what many ad-hoc exporters write for a missing numeric, and
read as a damage figure it makes the row quietly *not damaging*; `inf` used
to escape as an `OverflowError` rather than as a one-line refusal. A missing
value is an empty cell.

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
saw nothing. Those misses are no longer thrown away at the `data.py` seam:
`n_rows_unmapped` reaches the diagnostics as `n_skipped_region` and prints as
an `unplaced rows` line, so a crosswalk covering half a country no longer
reads like a country with half the events.
[`tests/data/emdat_sample.xlsx`](../tests/data/emdat_sample.xlsx)
is a synthetic export in the real column shape.

**The export must cover exactly one country.** EM-DAT's public download is a
query result and normally covers a region or the world; admin names collide
constantly across borders ("Northern Province", "Central", "Eastern"), and
the crosswalk lookup folds case and whitespace, so a cross-border row would
become a damaging event in a district that saw nothing. There is nothing to
filter on — the `Country` column carries a name, not the alpha-2 code the
contract names — so an export naming more than one country is **refused**,
naming them, and the operator re-exports one country. Three more refusals in
the same voice: the workbook's first sheet is found by the *numeric* suffix
of its part (`sheet2.xml` before `sheet10.xml`), the header is detected by
scanning the first ten non-blank rows for the one carrying every expected
column (so a title or banner row is not reported as upstream schema drift,
and a real renaming still is — with the rows it looked at), and `Start Year`
is range-checked to 1900–2100 so an Excel serial date cannot read as a year
and empty the panel in silence. A zip member that decompresses to more than
256 MB, or claims a compression ratio over 200:1, is refused before it is
read.

## The records directory and the never-committed rule

A pilot's ground-truth file lives at `snapshots/records/<CC>/<basename>`
(`data.RECORDS_DIRNAME = "records"`, `data.records_path`) — the contract
names the basename only, so the directory is policy and `records_path` is
where that policy lives. The country is part of that policy, and matches the
manifest key (`records/<CC>/<basename>`): two agencies whose exports are both
called `records.csv` (or `emdat.xlsx`, the default download name) must not
collide on one path backing two pinned hashes. The committed EM-DAT crosswalk
stays flat, one per country, at `snapshots/records/<cc>_emdat_regions.csv`:
its name already carries the country code, and it is the one file in that
directory that is committed.

Five things enforce that the bytes stay there and nowhere else:

1. **`.gitignore`** ignores everything under `snapshots/records/` except the
   directory itself and a crosswalk named in *exactly* that shape:
   `!snapshots/records/??_emdat_regions.csv`, two letters and no more. The
   re-inclusion used to be a suffix (`*_emdat_regions.csv`), so a partner file
   named `PARTNER_CONFIDENTIAL_emdat_regions.csv` was re-included and
   committed by the next `git add -A` — the packer would have refused those
   bytes, but git got there first. `contracts._validate_sources` refuses a
   `ground_truth.file` of that shape for the same reason, so the guard sits on
   both sides of the boundary. A ground-truth file itself is one level down,
   and git does not descend into an excluded directory, so it stays ignored.
   The crosswalk is ours to publish: it is a person's own judgement about
   which geoBoundaries region an EM-DAT name means, and a panel cannot be
   reproduced without it.
2. **The site packer refuses by path, not by memory.** `tools/build_site.py`
   defines `PRIVATE_SNAPSHOT_DIR = "snapshots/records"`; `is_private()`
   raises `PrivateDataError` on anything physically under it — including the
   committed crosswalk file, since the guard is a path check, not a
   whitelist of what it remembered to exclude. Every file the archive holds
   goes through one `add()` (`_adder`), which raises where it would have
   written, so a glob pattern added later cannot sweep one in quietly.
   `PRIVATE_KEY_PREFIXES = ("records/", "emdat/")` additionally strips every
   manifest record filed under those prefixes out of the public manifest the
   sandbox archive ships, via `is_private_key()` — see the note on the
   basename below for what that redaction does and does not mean.
3. **The website publishes no pilot labels.** `tools/build_site.py` skips a
   pilot in `build_tapes` outright, and emits for a pilot in `build_panels`
   only what a contract card already publishes (`digest`, `n_units`,
   `data_version`). A tape is a bitmap in which bit `p * n_regions + r` says
   which region had a damaging event in which period, for every year of the
   contract — that *is* the partner's archive at panel resolution, and
   publishing it while the packer refuses the bytes it came from would be a
   distinction without a difference. The per-split positive counts, base
   rates, region names and the diagnostics block go with it.
4. **The manifest `notes` carry counts, never text from the file.** Each
   connector renders two strings: `summary()`, with the partner's own hazard
   values and EM-DAT's unmapped admin names, for the terminal progress line
   and `readiness panel`; and `counts()`, numbers only, which is what
   `snapshots/manifest.json` — the one committed, published file under
   `snapshots/` — records. Real archives carry operation names, outbreak
   names and place names in those columns.
5. **`verify --phase 4`'s `ground truth pinned` check** confirms each
   pilot's pinned hash still matches the sha256 its contract names — from
   `snapshots/manifest.json` alone, never by reading the bytes, because the
   check has to be able to run in a clone that has never seen them.

Registering a pilot computes the sha256 once, at registration time, from
whatever file `--records` names; nothing about the bytes is read again until
a real `readiness panel` or `readiness loop` run, and even then the file is
read in place and pinned, never copied.

**The basename is published, and that is deliberate.** A pilot's
`ground_truth.file` is a criterion: it is in the committed contract, in
`contracts/<name>.json` inside `sandbox.zip`, in
`site/generated/contracts.json`, in `describe()` and in every ledger card's
`data_snapshot.inputs` — a panel cannot be reproduced without knowing which
file it means. The packer's redaction of the manifest record is not a promise
that the name is secret; it drops a pin the archive cannot use, because a
pilot's panel can never be built in the browser. So **name the file
neutrally**: not after the partner, the data-sharing agreement or the case.
`readiness register` prints that line next to the path it tells you to use,
and DATA-LICENSES.md says it again.

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

`RecordEvent.__post_init__` checks the three things the walk assumes and
cannot check cheaply: `1 <= month <= 12`, `year > 0`, and at least one region
id. `_walk_events` turns a month into a period index while `_dense_panel`
only materialises periods inside the year, so a month outside 1..12 was
counted as a positive unit that then was not in the panel — contradicting the
walk's own invariant that the positive count is the count of ones in the
panel by construction. Both connectors already satisfied all three; the
dataclass is where the next one finds out.

Two counters were added beside `n_skipped_hazard`, both defaulting to zero so
the US rendering is byte-for-byte what it was, and both printed only when
non-zero:

```
    unplaced rows            3   rows whose admin units the crosswalk could not place
    regions dropped          1   named regions outside the universe on rows that had one inside
```

`n_skipped_region` is the EM-DAT connector's `n_rows_unmapped` (a partner
file names shapeIDs directly and passes 0). `n_regions_outside` counts
*regions* rather than rows: `n_outside_universe` only ever counted a row that
lost **every** region, so an event naming five districts of which four were
renumbered by a different geoBoundaries release produced one positive and
four silent losses while the diagnostics said everything landed. That is
precisely the failure making `regions.release` a hashed criterion is meant to
surface.

## What is US-only, and refused by name

`data.US_ONLY_FEATURES = ("terrain", "nri")`. `terrain` reads the Census
Gazetteer (water share, latitude); `nri` reads FEMA's National Risk Index.
Neither exists outside the US, so `data._load_features` refuses a pilot that
asks for either **by name**, before anything is built: "there is nothing for
them to read" — a `ValueError` naming the blocked connector(s), not an empty
source handed to a model that would look like a real experiment.

`data.pinned()` answers **False** for a US-only connector outside the US, for
the same reason: `readiness loop`, `score` and `fleet` with no `--features`
flag load every connector whose data is already pinned, so a pilot
auto-selects the global ones alone and the candidates that need the others
are skipped by the orchestrator. Answering True there (which an empty
pinned-file list used to do) auto-selected `terrain` for any pinned pilot and
`build` then refused the whole run — a pilot was unusable by default the
moment its base data was pinned. Asking for one outright is still a refusal
that names the connector; it is now a usage error, printed as one line rather
than as a traceback.

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
| `--regions-release RELEASE` | the geoBoundaries release, e.g. `"gbOpen 6.0.0"` (default `contracts.GEOBOUNDARIES_RELEASE`) — a hashed criterion, because `shapeID`s change between releases, and validated here rather than at build time |
| `--regions-sha256 HEX` | optional: pin the boundary file's own bytes, so a mirror cannot serve a different universe under the same release name. Omitted, the field is not written and the digest is what it would have been |

`--admin-level`, `--regions-release` and `--regions-sha256` describe a
geoBoundaries universe and are **refused** for a US contract rather than
ignored, exactly as `--ground-truth` and `--records` already were; and
`--event-type` names a NOAA Storm Events vocabulary and is refused outside
the US. A flag that does not apply is a mistake worth saying out loud: it is
otherwise dropped without a word, and the operator believes a criterion was
registered that was not.

After writing the contract, `readiness register` prints exactly where the
file has to go and never touches it itself:

```
note: place the records file at snapshots/records/<CC>/<basename>
(sha256:<first 16 hex>...). It is never committed, never copied and never
packed into the browser sandbox; only its hash is, here and in
snapshots/manifest.json.
      its basename (<basename>) is a criterion: it is written into the
committed contract, into every ledger card's inputs and into the published
site, because a panel cannot be reproduced without knowing which file it
means. Name the file neutrally — not after the partner, the agreement or the
case (DATA-LICENSES.md).
```

and, only for `--ground-truth emdat`, that the crosswalk
`snapshots/records/<cc>_emdat_regions.csv` (`emdat_name,shape_id`) has to be
written before a panel can be built, and that the export must cover one
country.

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
packed or published, and neither are the labels derived from them.** A
partner file is referenced by sha256 and basename only — in the contract
(`ground_truth.sha256`, `ground_truth.file`) and in
`snapshots/manifest.json`, whose `notes` carry counts and no text lifted out
of the file. Nothing in this repository, the browser sandbox or the published
website has ever held, or will hold, the contents of a partner's national
record or an EM-DAT export; the website publishes no pilot label bitmap, no
pilot positive counts or base rates and no pilot region names. Only the
crosswalk a person writes for EM-DAT
(`snapshots/records/<cc>_emdat_regions.csv`) is ours to commit, because it
names no event, only which geoBoundaries region an EM-DAT admin name means.

The one thing that *is* published is the file's basename, because it is a
criterion a reproduction needs. Operators are told to name the file
neutrally, by `readiness register` and by DATA-LICENSES.md.

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
