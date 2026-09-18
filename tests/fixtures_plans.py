"""Synthetic facilities, risk layers and case studies for the Phase 3 tests.

Everything here is placeless in the same way `tests/fixtures.py` is: the county
FIPS sits under state 99, which does not exist, and every name is invented. The
facility schema has no address or coordinate field to make placeless in the
first place — that is the point of it — but the county still has to be a county
nobody lives in.

`facility_dict` fills the `evidence` block automatically for whatever fields
the overrides leave populated, so a test that wants a facility with no stored
water writes `facility_dict(**{"water.on_site_storage_hours": None})` and does
not have to maintain twenty evidence entries to say it.
"""

from __future__ import annotations

import copy
import json
import pathlib
from typing import Any

from readiness import cite
from readiness.issue import Issued
from readiness.plans import scenarios as scenarios_mod
from readiness.plans.facility import Facility, populated_paths
from readiness.plans.risk import RiskLayer

COUNTY = "99001"
OTHER_COUNTY = "99007"
PERIOD = "2026-Q4"
CONTRACT = "flood-zz"

#: A facility that answers every question it can and fails the ones the example
#: is built to fail. Overridden per test.
BASE: dict[str, Any] = {
    "slug": "test-facility",
    "occupancy_type": "hospital",
    "county_fips": COUNTY,
    "census": 30,
    "staff_on_shift": 12,
    "power": {
        "generator": True,
        "fuel_hours": 120,
        "switchgear_elevation_ft": 14,
        "transfer_switch_elevation_ft": 14,
        "load_test_interval_days": 30,
    },
    "water": {"on_site_storage_hours": 96},
    "design_intensity": {
        "flood_elevation_ft": 10,
        "flood_elevation_source": "Elevation Certificate, Section C (synthetic)",
        "design_wind_mph": 120,
    },
    "evacuation": {
        "trigger_written": True,
        "trigger_text": "Evacuate on a mandatory order for this zone (synthetic).",
        "authority": "the administrator on call",
        "transport_lead_hours": 8,
        "priority_order_written": True,
        "priority_decided_on": "approved at the preparedness committee, synthetic date",
    },
    "transfer_agreements": [
        {
            "name": "Far Ridge Hospital",
            "county_fips": OTHER_COUNTY,
            "signed": True,
            "same_floodplain": False,
            "same_grid_feeder": False,
        },
    ],
    "co_located_operators": [],
}


def deep_set(data: dict, path: str, value: Any) -> None:
    """Set a dotted path, walking through mappings and list indices."""
    node: Any = data
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[int(part)] if part.isdigit() else node[part]
    last = parts[-1]
    if last.isdigit():
        node[int(last)] = value
    else:
        node[last] = value


def auto_evidence(data: dict) -> dict:
    """One evidence entry per populated field, or per list for a list's leaves."""
    evidence: dict[str, dict] = {}
    for path in populated_paths(data):
        head = path.split(".", 1)[0]
        key = head if isinstance(data.get(head), list) else path
        evidence.setdefault(key, {
            "text": f"synthetic evidence for {key}",
            "source_doc": "synthetic planning record",
            "page": None,
        })
    return evidence


def facility_dict(**overrides: Any) -> dict:
    """The base record with dotted-path overrides applied and evidence refilled.

    Use `slug="x"` for a top-level field and `**{"power.fuel_hours": 4}` for a
    nested one; a value of `None` empties an optional field, and the evidence
    block follows.
    """
    data = copy.deepcopy(BASE)
    for path, value in overrides.items():
        deep_set(data, path, copy.deepcopy(value))
    data["evidence"] = auto_evidence(data)
    return data


def make_facility(**overrides: Any) -> Facility:
    return Facility.from_json(facility_dict(**overrides))


def write_facility(path: pathlib.Path, **overrides: Any) -> pathlib.Path:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(facility_dict(**overrides), indent=2) + "\n", encoding="utf-8"
    )
    return path


def scenario() -> scenarios_mod.Scenario:
    """The committed 96-hour scenario, which is the specification under test."""
    return scenarios_mod.load()


# --------------------------------------------------------------------------- #
# The risk layer
# --------------------------------------------------------------------------- #


def make_issued(
    *, contract: str = CONTRACT, label: str = PERIOD, probabilities=None
) -> Issued:
    """An issued file's contents, with the provenance a claim may cite."""
    return Issued(
        contract=contract, contract_digest="c" * 16, model="logistic+iso",
        version="1.0.0", model_kwargs={}, validated_by="exp-0002",
        period=(2026, 4), period_label=label,
        probabilities=dict(probabilities or {COUNTY: 0.18, OTHER_COUNTY: 0.11}),
        train_digest="t" * 16, feature_digest="f" * 16, feature_version="fv" * 8,
        data_version="dv" * 8, harness_digest="h" * 16,
        issued_at="2026-09-18T00:00:00+00:00", inputs=("census/national_county2020",),
    )


def make_risk(*, label: str = PERIOD, issued: Issued | None = None) -> RiskLayer:
    """A risk layer built in memory from one issued file, with its card record."""
    one = issued or make_issued(label=label)
    return RiskLayer(
        period_label=label,
        probabilities={
            (fips, one.contract): value for fips, value in one.probabilities.items()
        },
        issued={one.contract: one},
        cards={
            f"{one.contract}/{one.validated_by}": {"experiment_id": one.validated_by},
            one.validated_by: {"experiment_id": one.validated_by},
        },
    )


def resolver_for(facility: Facility, scen=None, risk: RiskLayer | None = None):
    """The resolver a gap report for this facility validates against."""
    from readiness.plans import gap_report as gap_report_mod

    return gap_report_mod.resolver(
        facility, scen or scenario(), risk or RiskLayer.empty(PERIOD)
    )


def sentences_text(doc: cite.Document) -> str:
    return " ".join(cite.strip_markers(s.text) for s in doc.sentences)


# --------------------------------------------------------------------------- #
# Case studies
# --------------------------------------------------------------------------- #


def synthetic_case_study(**overrides: Any) -> dict:
    """A case study that exercises the mechanism without pretending to be one.

    Zero real case studies ship (`plans/case-studies/README.md` says why: each
    is an example added deliberately, with a published investigation cited for
    every fact). This one names an event that did not happen, in a county that
    does not exist, so that `readiness scenarios check` and
    `verify --phase 3`'s case-study criterion have something to run.
    """
    facility = facility_dict(
        slug="synthetic-case-facility",
        **{
            "power.switchgear_elevation_ft": 3,
            "power.transfer_switch_elevation_ft": 3,
            "design_intensity.flood_elevation_ft": 11,
            "evacuation.priority_order_written": False,
            "evacuation.priority_decided_on": None,
            "evacuation.transport_lead_hours": 24,
            "water.on_site_storage_hours": 12,
            "transfer_agreements.0.county_fips": COUNTY,
            "transfer_agreements.0.same_floodplain": True,
        },
    )
    study = {
        "slug": "synthetic-river-flood",
        "scenario": scenarios_mod.DEFAULT_SCENARIO,
        "event": {
            "text": "A river flood that did not happen, at a hospital that does not "
                    "exist, in a county that does not exist.",
            "source": 0,
        },
        "hazard": {"text": "inland flood", "source": 0},
        "dates": {"text": "an invented week in an invented year", "source": 0},
        "sources": [{
            "title": "Synthetic after-action report (not a real document)",
            "publisher": "tests/fixtures_plans.py",
            "year": None,
            "url": "https://example.invalid/synthetic",
        }],
        "facility_as_recorded": facility,
        "expected_findings": {
            "q1": "failed", "q2": "failed", "q3": "failed",
            "q4": "failed", "q5": "answered", "q6": "failed",
        },
    }
    study.update(overrides)
    return study


def write_case_study(path: pathlib.Path, **overrides: Any) -> pathlib.Path:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(synthetic_case_study(**overrides), indent=2) + "\n", encoding="utf-8"
    )
    return path


__all__ = [
    "BASE",
    "CONTRACT",
    "COUNTY",
    "OTHER_COUNTY",
    "PERIOD",
    "auto_evidence",
    "deep_set",
    "facility_dict",
    "make_facility",
    "make_issued",
    "make_risk",
    "resolver_for",
    "scenario",
    "sentences_text",
    "synthetic_case_study",
    "write_case_study",
    "write_facility",
]
