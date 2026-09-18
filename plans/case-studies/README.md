# Case studies

Worked examples of the scenario library's tests, one JSON file per real
event, checked by `readiness scenarios check`. None ships by default: a case
study is an *example*, added deliberately, and every fact in it must cite a
published investigation, an after-action report or a court or regulatory
record — a fact with no source fails the schema and the file is refused, not
silently accepted.

## The JSON shape

The block below is the whole schema `readiness/plans/case_studies.py` enforces,
and `tests/test_case_studies.py` loads this very block through
`CaseStudy.from_json` — so if the code and this page disagree, the suite says
so rather than a reader finding out.

```json
{
  "slug": "example-flood-2021",
  "scenario": "96h-isolation-acute-care",
  "event": {
    "text": "The flood that closed the hospital, with its date.",
    "source": 0
  },
  "hazard": {
    "text": "inland flood",
    "source": 0
  },
  "dates": {
    "text": "the week it happened",
    "source": 1
  },
  "sources": [
    {
      "title": "After-action report",
      "publisher": "the agency that published it",
      "year": 2021,
      "url": "https://example.invalid/aar",
      "note": "what this source establishes (optional)"
    },
    {
      "title": "Regulatory survey",
      "publisher": "the regulator",
      "year": 2022,
      "url": "https://example.invalid/survey"
    }
  ],
  "facility_as_recorded": {
    "slug": "example-flood-2021-hospital",
    "blind_id": "00000000000000000000000000000000",
    "occupancy_type": "hospital",
    "county_fips": "99001",
    "census": 24,
    "staff_on_shift": 11,
    "power": {
      "generator": true,
      "fuel_hours": 72,
      "switchgear_elevation_ft": 6.5,
      "transfer_switch_elevation_ft": 6.5,
      "load_test_interval_days": null
    },
    "water": {
      "on_site_storage_hours": null
    },
    "design_intensity": {
      "flood_elevation_ft": 9.5,
      "flood_elevation_source": "Elevation Certificate, Section C",
      "design_wind_mph": null
    },
    "evacuation": {
      "trigger_written": false,
      "trigger_text": null,
      "authority": null,
      "transport_lead_hours": null,
      "priority_order_written": false,
      "priority_decided_on": null
    },
    "transfer_agreements": [
      {
        "name": "the receiving facility the record names",
        "county_fips": "99001",
        "signed": true,
        "same_floodplain": true,
        "same_grid_feeder": null
      }
    ],
    "co_located_operators": [],
    "evidence": {
      "slug": {
        "text": "the identifier this record uses",
        "source_doc": "After-action report",
        "page": 1
      },
      "occupancy_type": {
        "text": "critical access hospital",
        "source_doc": "After-action report",
        "page": 1
      },
      "county_fips": {
        "text": "the county it sits in",
        "source_doc": "After-action report",
        "page": 1
      },
      "census": {
        "text": "occupants on the night of the event",
        "source_doc": "After-action report",
        "page": 3
      },
      "staff_on_shift": {
        "text": "staff on the night shift",
        "source_doc": "After-action report",
        "page": 3
      },
      "power.generator": {
        "text": "one generator on site",
        "source_doc": "Regulatory survey",
        "page": 2
      },
      "power.fuel_hours": {
        "text": "seventy-two hours of fuel",
        "source_doc": "Regulatory survey",
        "page": 2
      },
      "power.switchgear_elevation_ft": {
        "text": "switchgear elevation above datum",
        "source_doc": "After-action report",
        "page": 8
      },
      "power.transfer_switch_elevation_ft": {
        "text": "transfer switch in the same room",
        "source_doc": "After-action report",
        "page": 8
      },
      "design_intensity.flood_elevation_ft": {
        "text": "base flood elevation for the building",
        "source_doc": "After-action report",
        "page": 8
      },
      "design_intensity.flood_elevation_source": {
        "text": "the certificate it was read from",
        "source_doc": "After-action report",
        "page": 8
      },
      "evacuation.trigger_written": {
        "text": "no written trigger in the plan",
        "source_doc": "Regulatory survey",
        "page": 5
      },
      "evacuation.priority_order_written": {
        "text": "no written priority order",
        "source_doc": "Regulatory survey",
        "page": 5
      },
      "transfer_agreements": {
        "text": "one signed agreement, same county and floodplain",
        "source_doc": "Regulatory survey",
        "page": 6
      }
    }
  },
  "expected_findings": {
    "q1": "failed",
    "q2": "unanswered",
    "q3": "failed",
    "q4": "failed",
    "q5": "answered",
    "q6": "failed"
  }
}
```

Top-level keys, and no others: `slug`, `scenario`, `sources`,
`facility_as_recorded`, `expected_findings`, and `event`, `hazard` and `dates`
— each of those three an **object** with a `text` and a `source` index into
`sources`, never a bare string. A source entry carries `title`, `publisher`,
`year` and `url`, and may carry a `note`. An unknown key is refused by name at
every level, the way a facility record refuses one: this is the one `plans/`
directory that is committed, so a field nothing reads is a field that could
carry anything into git unexamined. Values are scanned too — a street address,
a ZIP+4 or a latitude/longitude pair in any string refuses the file, and a
bare five-digit number is a county FIPS and is not flagged.

`facility_as_recorded` is a full facility record, the same schema
[`plans/facilities/README.md`](../facilities/README.md) documents, including
its own `blind_id`.

Every fact under `facility_as_recorded` must have a matching `evidence` entry
whose `source_doc` names one of the entries in `sources` — the same rule a
real facility record follows, so a case study is tested through the *same*
rules a live facility goes through, not a special-cased shortcut. Loading a
case study with a fact that names no source, or naming a source that is not
listed under `sources`, is refused rather than silently skipped.

`readiness scenarios check` runs `readiness/plans/rules.py` against
`facility_as_recorded` and the scenario named by `scenario`, and compares
the resulting finding for every question to `expected_findings`. A mismatch
is reported by question id, with both statuses, so a case study that
regresses is a specific, readable failure — this is report §6's promise that
"case studies become regression tests" made literal.

A case study file should, in its sources and its mapping onto the scenario,
not only its JSON structure:

1. name the event, the facility type and the hazard, with dates;
2. map what happened onto the injects of a scenario in
   [`../scenarios/`](../scenarios/), hour by hour where the record allows;
3. answer, from the record, each question the scenario asks — and say plainly
   where the record is silent (an unanswered question is `unanswered`, not
   omitted);
4. list its sources in full, with enough detail that another reader could
   find the same passage.

Case studies are used here as the engineering standard they deserve to be,
and any product surface that renders one should say the same.
