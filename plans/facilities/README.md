# plans/facilities/

The facility records `readiness gap-report` reads. One JSON per facility, named
by its slug.

**Only the fictional example is committed.** `.gitignore` keeps every other
`*.json` here out of git, because a real record names a building and lists the
ways it fails: where the switchgear sits, how many hours of fuel and water it
holds, whether the evacuation trigger is written down. That is exactly the
document an attacker would want and exactly the document a planner must be able
to keep private, so the tree carries this page and
[`example-rural-hospital.json`](example-rural-hospital.json) — a facility that
does not exist, in a county that does not exist — and nothing else.

## What the schema is, and what it is not

`readiness/plans/facility.py` is the schema, and it refuses two things by name:

* **Unknown keys.** A field no rule reads is a field a planner thought they had
  supplied. The refusal names the key rather than dropping it.
* **Address and coordinates, by key *and* by value.** The keys
  `facility.FORBIDDEN_KEYS` lists — `address`, `address_line1`,
  `address_line2`, `apn`, `block`, `block_group`, `coordinates`, `easting`,
  `geocode`, `geohash`, `geometry`, `gps`, `lat`, `lat_lon`, `latitude`,
  `latlon`, `lon`, `longitude`, `northing`, `parcel`, `plus_code`,
  `postal_code`, `street`, `tract`, `zip`, `zipcode` — are refused anywhere in
  the file, with the reason. So is any *string value* that reads as a street
  address (`412 Riverside Drive`), a ZIP+4 (`27834-1234`), a decimal-degree
  pair (`35.6127, -77.3664`) or the token `ZIP`: a key list cannot cover a
  free-text note, and a note is where an address actually ends up. A bare
  five-digit number is a county FIPS and is not flagged. Nothing in this
  repository is keyed finer than a county (report §7), and address-level
  intensity would have to be pinned somewhere for a model to read it.
* **Negative hours, elevations, counts and lead times.** `-5` hours of
  transport notice is a typo, not a facility that needs five hours less
  warning, and a rule reasoning over it would report a break at a negative
  hour.

## `blind_id`, and choosing a slug

Every record carries a `blind_id`: 32 lowercase hex characters, generated once
and kept forever.

```
python3 -c "import secrets; print(secrets.token_hex(16))"
```

The label a blinded report shows a reviewer is `FACILITY-` plus its first
twelve characters. It is deliberately not derived from the slug: a truncated
digest of a human-chosen slug is a dictionary search away from the building's
name, and the blinded page is the artefact that leaves the building.

Choose a **distinctive** slug while you are at it. The blinded render checks
its own output and refuses to ship a page that still contains the slug, a
partner name, an evidence document or a county FIPS — and a slug that is an
ordinary English word ("one", "valley") cannot be told apart from the word in
prose by any mechanism, so such a record is refused rather than half-blinded.

The design intensity the 96-hour scenario needs therefore comes from *you*:
`design_intensity.flood_elevation_ft` is the base flood elevation from your own
Elevation Certificate or FIRM panel, and `flood_elevation_source` names the
document it was read from. When it is missing, the switchgear question reports
that it cannot be run and names the certificate that would supply it. No county
probability is ever put in its place — a region-level answer to a switchgear
question is worse than no answer, because it looks like one.

## Evidence

Every populated field but `blind_id` — which is this repository's bookkeeping,
not a fact read off a document — must be named by an entry under `evidence`, keyed by its
field path (`power.fuel_hours`, `evacuation.transport_lead_hours`), each with
the text it was read as, the `source_doc` it came from, and a `page` where
there is one. A field without evidence refuses the file, naming the field. This
is not bureaucracy: every sentence a gap report writes about a number cites the
field path, and the field path is only worth citing if it leads somewhere.

A leaf inside a list may be evidenced by the list itself — one signed transfer
agreement is one document — so `transfer_agreements` covers every agreement's
name, county and flags.

## Writing one

Copy the example, replace its values with yours, delete the fields you do not
have (set them to `null`; only the fields the schema marks optional may be
null), and run:

```
readiness gap-report --facility plans/facilities/<slug>.json --period 2026-Q4
```

A record the schema refuses exits 2 and names the field. A report whose
citations do not validate exits 1 and prints every violation; nothing is
written either way.
