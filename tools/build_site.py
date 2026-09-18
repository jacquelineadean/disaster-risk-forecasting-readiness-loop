#!/usr/bin/env python3
"""Assemble the overview website's data from the repository's own artefacts.

The pages under `site/` are static and hand-written. Everything they show that
could drift from the code — the registered contracts and their digests, the
committed ledgers, the blessed fingerprints, the captured transcripts, the
hazard catalogue, the model registry — is generated here, from the same
modules the CLI uses, into `site/generated/`:

    contracts.json     every registered contract, its digest and describe() text,
                       and the path of its backtest report when one is committed
    backtest/          experiments/<name>/backtest.html, copied when it exists
    contract_defaults.json
                       what a contract gets for everything it does not say
                       (readiness.contracts.DEFAULTS); the register form reads it
    hazards.json       the hazard catalogue (readiness.config.HAZARDS)
    models.json        the proposable models, each with its parameter schema
                       and whether it needs features, and the loop's queues
    features.json      the feature catalogue (readiness.engine.features.FEATURE_SETS):
                       every set with its columns, sources, transforms and lags
    ledgers.json       every committed ledger: cards, raw lines, anchor, chain status
    expected.json      the blessed baseline fingerprints, one per contract
    panels.json        each contract's labelled panel: size, splits, event coverage
                       (only for contracts whose pinned data is present locally)
    tapes.json         the same panels as bit strings, period-major, for the tile
                       maps and the animated hero (only with the pinned data)
    fleet.json         `readiness fleet --status`, one row per registered contract,
                       read from the ledgers alone (national contracts are marked)
    briefs.json        every county brief under briefs/ that passes readiness.cite
                       (the rest are logged and left out), with its HTML copied to
                       generated/briefs/<fips>/<period>.html; [] when none ship
    exposure.json      per state with a pinned USA Structures extract: counties,
                       structures, layer vintage; [] when nothing is pinned
    transcripts.json   the walkthrough transcripts captured by tools/demo/capture.py
    media/             the recordings and screenshots the walkthrough embeds
    report/index.html  the research briefing, so the site is self-contained
    sandbox.zip        the package, registry, ledgers, fingerprints and pinned data
                       the browser sandbox (Pyodide) unpacks and runs
    sandbox.json       what the sandbox carries: data coverage per contract

    python3 tools/build_site.py [--out DIR] [--no-sandbox]

The sandbox archive packs whatever pinned Storm Events extracts are present in
`snapshots/` (run `readiness snapshot -c <name>` first). A contract whose
states have no extract still registers and validates in the browser; it just
cannot build a panel there. States named by a single-state contract are packed
with every event type, so a visitor can register a new hazard against them; a
state that only appears in a wide multi-state contract is packed with that
contract's event types only, to keep the download small.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as _dt
import io
import json
import pathlib
import re
import shutil
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from readiness import __version__, cite, config, contracts as contracts_mod  # noqa: E402
from readiness import brief as brief_mod  # noqa: E402
from readiness import data as data_mod  # noqa: E402
from readiness import fleet as fleet_mod  # noqa: E402
from readiness.agent import orchestrator  # noqa: E402
from readiness.connectors import usa_structures  # noqa: E402
from readiness.connectors.base import ConnectorError, Manifest  # noqa: E402
from readiness.engine.features import FEATURE_SETS  # noqa: E402
from readiness.engine.registry import REGISTRY  # noqa: E402
from readiness.exposure.table import ExposureError, ExposureTable  # noqa: E402
from readiness.harness.ledger import Ledger  # noqa: E402

SITE = ROOT / "site"
DEFAULT_OUT = SITE / "generated"
#: Where `readiness brief` writes: briefs/<fips>/<period>.html and .json.
BRIEFS_DIR = ROOT / "briefs"
#: Where `readiness issue` writes: issued/<contract>/<period>.json.
ISSUED_DIR = ROOT / "issued"

MEDIA = ("loop.gif", "dashboard.gif", "report-top.png", "report-section-2.png",
         "dashboard-index.png")

#: Raw-byte ceiling per state for packing every event type. Above it, the
#: state is packed with the contracts' event types only.
FULL_STATE_BUDGET = 20_000_000

_FIELDS_ORDER = ("EVENT_ID", "YEAR", "BEGIN_YEARMONTH", "EVENT_TYPE")


def log(msg: str) -> None:
    print(f"build_site: {msg}")


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def dump(path: pathlib.Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# JSON views of the registry, catalogue, ledgers, fingerprints and transcripts
# ---------------------------------------------------------------------------


def contract_view(c: contracts_mod.Contract, out: pathlib.Path) -> dict:
    return {
        "name": c.name,
        "version": c.version,
        "description": c.description,
        "hazard": c.hazard,
        "event_types": list(c.event_types),
        "country": c.country,
        "states": list(c.states),
        "scope_key": c.scope_key,
        "scope_label": c.scope_label,
        # Plan §3: "six national contracts registered as data"; the fleet and
        # `verify --phase 2` count only these, so the pages tell them apart.
        "national": not c.states and not c.is_pilot,
        "pilot": c.is_pilot,
        "ground_truth": dict(c.ground_truth),
        "regions": dict(c.regions),
        "record_start_year": c.record_start_year,
        "period": c.period,
        "periods_per_year": c.periods_per_year,
        "damage_property_usd_min": c.damage_property_usd_min,
        "damage_count_casualties": c.damage_count_casualties,
        "zone_policy": c.zone_policy,
        "splits": {
            "train": [c.train_years[0], c.train_years[-1]],
            "validate": [c.validate_years[0], c.validate_years[-1]],
            "test": [c.test_years[0], c.test_years[-1]],
        },
        "test_touch_budget": c.test_touch_budget,
        "reference_model": c.reference_model,
        "thresholds": {
            "min_brier_skill_score": c.min_brier_skill_score,
            "reliability_tolerance_pp": c.reliability_tolerance_pp,
            "reliability_min_bin_count": c.reliability_min_bin_count,
            "min_auc": c.min_auc,
            "n_reliability_bins": c.n_reliability_bins,
        },
        "digest": c.digest(),
        "canonical_json": c.canonical_json(),
        "describe": c.describe(),
        "spec": c.to_spec(),
        "hazard_coding": (
            config.HAZARDS[c.hazard].coding if c.hazard in config.HAZARDS else None
        ),
        # Relative to site/generated/, or None: the ledgers page links the
        # Phase 1 backtest report only for contracts that have committed one.
        "backtest": copy_backtest(c, out),
    }


def copy_backtest(c: contracts_mod.Contract, out: pathlib.Path) -> str | None:
    """Copy `experiments/<name>/backtest.html` under generated/, if committed.

    The report is built by `readiness backtest` from committed files only and
    is itself committed beside the ledger; the site serves that file as is,
    so what a visitor reads is exactly what `verify --phase 1` checked.
    """
    src = data_mod.paths(c).ledger.parent / "backtest.html"
    if not src.exists():
        return None
    rel = f"backtest/{c.name}.html"
    (out / "backtest").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, out / rel)
    return rel


def build_contracts(out: pathlib.Path) -> dict[str, contracts_mod.Contract]:
    registry = contracts_mod.registered()
    views = [contract_view(c, out) for c in registry.values()]
    dump(out / "contracts.json", views)
    with_backtest = [v["name"] for v in views if v["backtest"]]
    log(f"contracts.json: {len(registry)} registered contract(s); "
        f"backtest reports for {with_backtest}")
    # The defaults travel separately because contracts.json is the list the
    # other pages iterate; the register form reads these so it never restates
    # a default the package might change.
    dump(out / "contract_defaults.json", dict(contracts_mod.DEFAULTS))
    return registry


def model_params(spec) -> list[dict]:
    """A model's parameter schema as a list, so `dump`'s key sorting keeps the
    constructor's order."""
    return [{"name": name, **p} for name, p in spec.params.items()]


def build_hazards(out: pathlib.Path) -> None:
    dump(
        out / "hazards.json",
        {
            name: {
                "event_types": list(h.event_types),
                "coding": h.coding,
                "summary": h.summary,
            }
            for name, h in config.HAZARDS.items()
        },
    )


def candidate_view(c) -> dict:
    return {
        "model": c.model,
        "changed": c.changed,
        "hypothesis": c.hypothesis,
        "kwargs": dict(c.kwargs),
    }


def build_models(out: pathlib.Path) -> None:
    # The Phase 1 queue runs after the baselines under `loop --queue phase1`;
    # it is exported when the orchestrator defines it so the sandbox page can
    # list the feature candidates it will skip for want of packed sources.
    phase1 = getattr(orchestrator, "PHASE1_QUEUE", ())
    dump(
        out / "models.json",
        {
            "registry": [
                {
                    "name": name,
                    "description": spec.description,
                    "is_canary_target": spec.is_canary_target,
                    "needs_features": spec.needs_features,
                    "params": model_params(spec),
                }
                for name, spec in REGISTRY.items()
            ],
            "queue": [candidate_view(c) for c in orchestrator.BASELINE_QUEUE],
            "phase1_queue": [candidate_view(c) for c in phase1],
            "canary_candidate": {
                "model": orchestrator.CANARY_CANDIDATE.model,
                "changed": orchestrator.CANARY_CANDIDATE.changed,
                "hypothesis": orchestrator.CANARY_CANDIDATE.hypothesis,
            },
            "canary_ceilings": {
                "max_plausible_bss": config.CANARY_MAX_PLAUSIBLE_BSS,
                "max_plausible_auc": config.CANARY_MAX_PLAUSIBLE_AUC,
                "max_agreement": config.CANARY_MAX_AGREEMENT,
            },
            "record_start_year": config.RECORD_START_YEAR,
        },
    )


def build_features(out: pathlib.Path) -> None:
    """The feature catalogue as a list, in the engine's order.

    Each column carries its spec verbatim (`FeatureSpec.to_dict`), so a reader
    can see which source and transform produce it and how many months are
    withheld before the period; the JavaScript renders a `list` parameter's
    value against these names.
    """
    dump(
        out / "features.json",
        [
            {"name": name, "columns": [spec.to_dict() for spec in specs]}
            for name, specs in FEATURE_SETS.items()
        ],
    )
    log(f"features.json: {len(FEATURE_SETS)} feature set(s)")


def build_ledgers(out: pathlib.Path, registry: dict[str, contracts_mod.Contract]) -> None:
    ledgers: dict[str, dict] = {}
    for name, contract in registry.items():
        where = data_mod.paths(contract)
        ledger = Ledger(where.ledger)
        cards = []
        if where.ledger.exists():
            with where.ledger.open(encoding="utf-8") as fh:
                raw_lines = [line.strip() for line in fh if line.strip()]
        else:
            raw_lines = []
        for card, raw in zip(ledger.read(), raw_lines):
            cards.append(card.record() | {"raw": raw})
        status = ledger.verify()
        anchor = None
        if ledger.anchor_path.exists():
            anchor = json.loads(ledger.anchor_path.read_text())
        ledgers[name] = {
            "contract": name,
            "contract_digest": contract.digest(),
            "path": data_mod.relative(where.ledger),
            "cards": cards,
            "anchor": anchor,
            "status": {
                "valid": status.valid,
                "n_cards": status.n_cards,
                "broken_at": status.broken_at,
                "reason": status.reason,
                "text": status.format(),
            },
            "summary": ledger.summary(),
        }
        head = status.format().splitlines()[0]
        log(f"ledgers.json: {name}: {len(cards)} card(s), {head}")
    dump(out / "ledgers.json", ledgers)


def build_expected(
    out: pathlib.Path, registry: dict[str, contracts_mod.Contract]
) -> None:
    expected = {}
    for name, contract in registry.items():
        path = data_mod.paths(contract).expected
        if path.exists():
            expected[name] = json.loads(path.read_text())
    dump(out / "expected.json", expected)
    log(f"expected.json: fingerprints for {sorted(expected)}")


def build_panels(
    out: pathlib.Path, registry: dict[str, contracts_mod.Contract]
) -> dict[str, data_mod.Dataset]:
    """Panel statistics per contract, built from pinned data only; never downloads.

    Returns the datasets it built, so the tapes can be cut from the same panels.
    """
    from readiness.harness import splits

    snapshot_dir = data_mod.SNAPSHOT_DIR
    panels: dict[str, dict | None] = {}
    datasets: dict[str, data_mod.Dataset] = {}
    for name, c in registry.items():
        # `data_mod.pinned` is the package's own answer to "would build()
        # download anything?"; the site never re-derives the layout itself.
        if not data_mod.pinned(c, snapshot_dir):
            panels[name] = None
            log(f"panels.json: {name}: pinned data not present locally, skipped")
            continue
        try:
            ds = data_mod.build(c, snapshot_dir=snapshot_dir)
        except (ConnectorError, OSError, ValueError) as exc:
            panels[name] = None
            log(f"panels.json: {name}: could not build ({exc})")
            continue
        datasets[name] = ds
        panel, d = ds.panel, ds.diagnostics
        if c.is_pilot:
            # A pilot's labels are derived from a file that is never
            # committed, never copied and never packed. Per-split positive
            # counts, base rates, region names and the diagnostics block are
            # that file at panel resolution, so the site publishes only what
            # the contract card already publishes: the panel's identity, its
            # size and the data version it was built from.
            panels[name] = {
                "pilot": True,
                "n_units": len(panel),
                "digest": panel.digest(),
                "data_version": ds.data_version,
            }
            log(f"panels.json: {name}: pilot, digest and size only")
            continue
        by_split = {}
        for split in c.splits:
            sliced = panel.filter_years(split.years)
            by_split[split.name] = {
                "years": [split.years[0], split.years[-1]],
                "n_units": len(sliced),
                "n_positive": sum(sliced.labels),
                "base_rate": sliced.base_rate if len(sliced) else None,
            }
        panels[name] = {
            "summary": panel.summary(),
            "n_units": len(panel),
            "n_positive": sum(panel.labels),
            "base_rate": panel.base_rate,
            "digest": panel.digest(),
            "years": [panel.years[0], panel.years[-1]],
            "n_regions": len(ds.regions),
            "first_region": str(ds.regions[0]),
            "last_region": str(ds.regions[-1]),
            "data_version": ds.data_version,
            "splits": by_split,
            "coverage": splits.coverage_report(panel, c.splits),
            "diagnostics": {
                "text": d.format(),
                "n_events": d.n_events,
                "n_in_years": d.n_in_years,
                "n_county_coded": d.n_county_coded,
                "n_zone_coded": d.n_zone_coded,
                "n_zone_expanded": d.n_zone_expanded,
                "n_zone_unmapped": d.n_zone_unmapped,
                "n_outside_universe": d.n_outside_universe,
                "n_damaging": d.n_damaging,
                "n_positive_units": d.n_positive_units,
                "crosswalk_edition": d.crosswalk_edition,
                "n_regions_outside": d.n_regions_outside,
            },
        }
        log(f"panels.json: {name}: {panel.summary()}")
    dump(out / "panels.json", panels)
    return datasets


def build_tapes(
    out: pathlib.Path, registry: dict[str, contracts_mod.Contract],
    datasets: dict[str, data_mod.Dataset],
) -> None:
    """Each built panel as a bit string, period-major: bit p * n_regions + r is
    region r (in FIPS order) in period p (in time order). The site draws one tile
    per region from it — shaded by frequency on the contract cards, lit frame by
    frame in the hero — so the pictures are the panels, not illustrations.

    A pilot is skipped outright. The bitmap decodes to exactly which region had
    a damaging event in which period, for every year of the contract — that is
    the substance of a partner's archive at panel resolution, and the packer
    next door raises `PrivateDataError` over the very bytes it came from."""
    tapes: dict[str, dict] = {}
    for name, ds in datasets.items():
        c = registry[name]
        if c.is_pilot:
            log(f"tapes.json: {name}: pilot, not published (labels are partner data)")
            continue
        panel = ds.panel
        regions = [region.id for region in ds.regions]
        rindex = {fips: i for i, fips in enumerate(regions)}
        periods = sorted({(year, period) for _region, year, period in panel.units})
        pindex = {yp: i for i, yp in enumerate(periods)}
        n_regions, n_periods = len(regions), len(periods)
        buf = bytearray((n_regions * n_periods + 7) // 8)
        counts = [0] * n_periods
        freq = [0] * n_regions
        for (region, year, period), label in zip(panel.units, panel.labels):
            if not label:
                continue
            p, r = pindex[(year, period)], rindex[region]
            i = p * n_regions + r
            buf[i >> 3] |= 0x80 >> (i & 7)
            counts[p] += 1
            freq[r] += 1
        tapes[name] = {
            "contract": name,
            "hazard": c.hazard,
            "period": c.period,
            "periods_per_year": c.periods_per_year,
            "years": [periods[0][0], periods[-1][0]],
            "n_periods": n_periods,
            "n_regions": n_regions,
            "regions": [{"fips": r.id, "name": r.name} for r in ds.regions],
            "splits": {
                "train": [c.train_years[0], c.train_years[-1]],
                "validate": [c.validate_years[0], c.validate_years[-1]],
                "test": [c.test_years[0], c.test_years[-1]],
            },
            "base_rate": panel.base_rate,
            "n_positive": sum(panel.labels),
            "counts": counts,
            "freq": freq,
            "bits": base64.b64encode(bytes(buf)).decode("ascii"),
        }
        log(f"tapes.json: {name}: {n_regions} regions x {n_periods} periods")
    dump(out / "tapes.json", tapes)


def build_transcripts(out: pathlib.Path) -> None:
    transcripts_dir = ROOT / "docs" / "media" / "transcripts"
    transcripts = {
        p.stem: p.read_text(encoding="utf-8")
        for p in sorted(transcripts_dir.glob("*.txt"))
    } if transcripts_dir.exists() else {}
    manifest_path = ROOT / "docs" / "media" / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    dump(out / "transcripts.json", {"transcripts": transcripts, "manifest": manifest})
    log(f"transcripts.json: {len(transcripts)} transcript(s)")


def copy_media(out: pathlib.Path) -> None:
    media_out = out / "media"
    media_out.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in MEDIA:
        src = ROOT / "docs" / "media" / name
        if src.exists():
            shutil.copyfile(src, media_out / name)
            copied.append(name)
    log(f"media/: {copied}")
    report = ROOT / "report" / "index.html"
    if report.exists():
        (out / "report").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(report, out / "report" / "index.html")
        log("report/index.html copied")
    else:
        log("note: report/index.html is missing (run `make report`); "
            "the site will link to GitHub")


# ---------------------------------------------------------------------------
# Phase 2: the fleet, the briefs and the exposure pins
# ---------------------------------------------------------------------------


def build_fleet(out: pathlib.Path, registry: dict[str, contracts_mod.Contract]) -> None:
    """`readiness fleet --status` as rows, from the ledgers and touch files alone.

    Nothing is fitted: `fleet.status` reads what `verify --phase 1` reads, so a
    row on the site is the same evidence the exit check would accept.
    """
    rows = []
    for row in fleet_mod.status(registry):
        contract = registry[row.name]
        rows.append(
            dataclasses.asdict(row)
            | {"national": not contract.states and not contract.is_pilot}
        )
    dump(out / "fleet.json", rows)
    passing = [r["name"] for r in rows if r["phase1_ok"]]
    log(f"fleet.json: {len(rows)} contract(s); phase 1 met for {passing}")


def _ledger_refs(registry: dict[str, contracts_mod.Contract]) -> dict[str, dict]:
    """Card records a brief may cite, by `exp-NNNN` and by `<contract>/exp-NNNN`.

    Card ids restart per ledger, so a bare id resolves to the last contract
    that has it; the qualified spelling is unambiguous.
    """
    refs: dict[str, dict] = {}
    for name, contract in registry.items():
        for card in Ledger(data_mod.paths(contract).ledger).read():
            record = card.record()
            refs[card.experiment_id] = record
            refs[f"{name}/{card.experiment_id}"] = record
    return refs


def _issued_periods(path: pathlib.Path) -> dict[str, dict]:
    """What an issued file covers: its periods, and the probability per county.

    The same shape `readiness.brief.issued_refs` builds, so a brief written by
    the command and re-validated here resolves `#<period>.<county>` to the same
    number both times.
    """
    periods: dict[str, dict] = {path.stem: {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return periods
    if not isinstance(raw, dict):
        return periods
    probabilities = raw.get("probabilities")
    per_county = (
        {str(k): v for k, v in probabilities.items()}
        if isinstance(probabilities, dict) else {}
    )
    for key in ("period", "period_label"):
        value = raw.get(key)
        if isinstance(value, str):
            periods[value] = dict(per_county)
    return periods


def _issued_refs(issued_dir: pathlib.Path) -> dict[str, dict[str, dict]]:
    """Issued files by every spelling of their path a brief may use."""
    refs: dict[str, dict[str, dict]] = {}
    for path in sorted(issued_dir.glob("*/*.json")):
        periods = _issued_periods(path)
        rel = path.relative_to(issued_dir).as_posix()
        for spelling in (f"issued/{rel}", rel, rel[: -len(".json")]):
            refs[spelling] = periods
    return refs


def brief_resolver(
    registry: dict[str, contracts_mod.Contract], *, issued_dir: pathlib.Path = ISSUED_DIR,
) -> cite.DictResolver:
    """What a committed brief may lean on: the ledgers, the issued files, the
    manifest and the guidance registry. Nothing else exists to a brief."""
    manifest_path = data_mod.SNAPSHOT_DIR / "manifest.json"
    manifest = Manifest.load(manifest_path) if manifest_path.exists() else None
    return cite.DictResolver({
        "ledger": _ledger_refs(registry),
        "issued": _issued_refs(issued_dir),
        "manifest": set(manifest.records) if manifest else set(),
    })


def _cited_contracts(doc: cite.Document) -> list[str]:
    """The contracts whose issued files the brief cites: issued/<contract>/<period>."""
    names: list[str] = []
    for claim in doc.claims:
        if claim.source.kind != "issued":
            continue
        parts = claim.source.ref.partition("#")[0].split("/")
        if len(parts) >= 2 and parts[-2] not in names:
            names.append(parts[-2])
    return names


def _brief_contracts(
    doc: cite.Document, registry: dict[str, contracts_mod.Contract]
) -> list[contracts_mod.Contract]:
    """The registered contracts this brief cites, for its attribution lines."""
    return [registry[name] for name in _cited_contracts(doc) if name in registry]


def _exposure_joined(doc: cite.Document) -> bool:
    """Whether the brief reports structure counts from a pinned extract."""
    return any(
        claim.id.startswith("exposure-") and claim.source.kind == "manifest"
        for claim in doc.claims
    )


def brief_view(
    path: pathlib.Path,
    doc: cite.Document,
    out: pathlib.Path,
    registry: dict[str, contracts_mod.Contract],
) -> dict:
    """One listed brief, with its page rendered from the document that validated.

    Not the committed sibling `.html`: that file is bytes nobody re-checked,
    and the whole point of re-validating here is that what is published is
    the document the rules were run against. The page is `brief.render_html`,
    the same renderer `readiness brief` uses, so it carries the caveat and the
    attribution the command writes.
    """
    fips, period = path.parent.name, path.stem
    rel = f"briefs/{fips}/{period}.html"
    (out / "briefs" / fips).mkdir(parents=True, exist_ok=True)
    lines = brief_mod.attribution(
        _brief_contracts(doc, registry), exposure_joined=_exposure_joined(doc)
    )
    (out / rel).write_text(brief_mod.render_html(doc, lines), encoding="utf-8")
    return {
        "fips": fips,
        "period": period,
        "title": doc.title,
        "contracts": _cited_contracts(doc),
        "html": rel,
        "generated_at": doc.generated_at,
        "inputs": dict(doc.inputs),
    }


def build_briefs(
    out: pathlib.Path, registry: dict[str, contracts_mod.Contract],
    *, briefs_dir: pathlib.Path = BRIEFS_DIR, resolver: cite.Resolver | None = None,
) -> None:
    """Every brief under briefs/ that validates today, and only those.

    `readiness brief` already refused to write a brief with a violation; the
    site re-runs `readiness.brief.check` — the citation rules *and* the
    county-only rule, the same call the command made — against the repository
    as it stands, so a brief whose card or issued file has since gone, or
    whose JSON has grown a sub-county key since it was written, is dropped
    from the list rather than shown.
    """
    resolver = resolver or brief_resolver(registry)
    listed: list[dict] = []
    for path in sorted(briefs_dir.glob("*/*.json")) if briefs_dir.exists() else []:
        try:
            doc = cite.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log(f"briefs.json: {data_mod.relative(path)}: unreadable ({exc}), skipped")
            continue
        violations = brief_mod.check(doc, resolver)
        if violations:
            log(f"briefs.json: {data_mod.relative(path)}: {len(violations)} violation(s), "
                f"first: {violations[0]}; not listed")
            continue
        listed.append(brief_view(path, doc, out, registry))
    dump(out / "briefs.json", listed)
    log(f"briefs.json: {len(listed)} validated brief(s)")


def build_exposure(out: pathlib.Path, snapshot_dir: pathlib.Path = data_mod.SNAPSHOT_DIR) -> None:
    """One row per state whose USA Structures counts are pinned and intact.

    The table is county-only by construction (`readiness.exposure.table`), and
    the site goes coarser still: a state's county count, its structures and
    the layer vintage, so a visitor can see what `readiness exposure snapshot`
    has pulled without the site carrying any exposure row itself.
    """
    manifest_path = snapshot_dir / "manifest.json"
    manifest = Manifest.load(manifest_path) if manifest_path.exists() else None
    state_of = {v: k for k, v in data_mod.state_fips(snapshot_dir).items()}
    rows: list[dict] = []
    for key in sorted(manifest.records) if manifest else []:
        if not key.startswith(usa_structures.KEY_PREFIX):
            continue
        st = key[len(usa_structures.KEY_PREFIX):]
        try:
            table = ExposureTable.load(snapshot_dir, manifest, [st])
        except (ExposureError, ConnectorError, OSError, ValueError) as exc:
            log(f"exposure.json: {key}: not loadable ({exc}), skipped")
            continue
        rows.append({
            "state_fips": st,
            "state": state_of.get(st),
            "n_counties": len(table),
            "total_structures": sum(r.total for r in table.rows.values()),
            "vintage": table.vintage,
            "manifest_key": key,
            "digest": table.digest(),
        })
    dump(out / "exposure.json", rows)
    log(f"exposure.json: {len(rows)} state(s) with pinned USA Structures counts")


# ---------------------------------------------------------------------------
# The sandbox archive
# ---------------------------------------------------------------------------


#: Manifest-key prefixes whose *bytes* are ground truth somebody gave us under
#: terms that forbid redistribution: a partner's national record, an EM-DAT
#: export. Only their sha256 is committed, and the sandbox archive is a
#: published artefact, so nothing pinned under these ever goes into it.
PRIVATE_KEY_PREFIXES: tuple[str, ...] = ("records/", "emdat/")

#: The snapshot subdirectory those files live in. Refused as a whole, by path,
#: so a future pattern added to the packer cannot sweep one in by accident —
#: the guard is structural rather than a list of what we remembered to exclude.
PRIVATE_SNAPSHOT_DIR = "snapshots/records"


class PrivateDataError(RuntimeError):
    """Raised rather than skipping: packing a partner's bytes is not a warning."""


def is_private(relpath: str) -> bool:
    """Whether a repo-relative path holds ground-truth bytes that must not ship."""
    posix = str(relpath).replace("\\", "/")
    return posix == PRIVATE_SNAPSHOT_DIR or posix.startswith(
        PRIVATE_SNAPSHOT_DIR + "/"
    )


def is_private_key(manifest_key: str) -> bool:
    """Whether a manifest key names such a file."""
    return str(manifest_key).startswith(PRIVATE_KEY_PREFIXES)


def _package_files() -> list[pathlib.Path]:
    return sorted(
        p for p in (ROOT / "readiness").rglob("*.py") if "__pycache__" not in p.parts
    )


def _filter_rows(text: str, keep: set[str]) -> tuple[str, int]:
    kept = []
    for line in text.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("EVENT_TYPE") in keep:
            kept.append(line)
    return ("\n".join(kept) + "\n") if kept else "", len(kept)


def _public_manifest(snapshot_dir: pathlib.Path) -> str:
    """`snapshots/manifest.json` with every private ground-truth record removed.

    Not because the basename is secret — it is not, and this build would be
    lying if it said so. A pilot's `ground_truth.file` is a criterion: it is in
    the committed contract, in `contracts/*.json` inside this same archive, in
    `site/generated/contracts.json` and in every ledger card's
    `data_snapshot.inputs`, because a panel cannot be reproduced without
    knowing which file it means. DATA-LICENSES.md says so, and tells operators
    to name the file neutrally.

    What is dropped is a *record the archive cannot use*: a pilot's panel
    cannot be built in the browser, because the bytes are not there and never
    will be, so the entry would be a dangling pin — carrying the record's byte
    size and the connector's counts for a file the sandbox can never hold.
    """
    blob = json.loads((snapshot_dir / "manifest.json").read_text())
    records = blob.get("records") or {}
    blob["records"] = {k: v for k, v in records.items() if not is_private_key(k)}
    return json.dumps(blob, indent=2, sort_keys=True) + "\n"


def _adder(zf: zipfile.ZipFile, root: pathlib.Path):
    """The packer's one way in.

    Every file the archive holds goes through this, and a file physically
    under the private records directory raises rather than being skipped: a
    pattern added later cannot sweep one in quietly, because it fails where it
    would have written. Module level so a test can execute the guard rather
    than infer it from a clean archive.
    """

    def add(path: pathlib.Path) -> None:
        rel = path.relative_to(root).as_posix()
        if is_private(rel):
            raise PrivateDataError(
                f"refusing to pack {rel}: partner records and EM-DAT exports are "
                "pinned by hash and never redistributed (DATA-LICENSES.md)"
            )
        zf.write(path, rel)

    return add


def _generated_at(manifest_path: pathlib.Path) -> str | None:
    if not manifest_path.exists():
        return None
    return json.loads(manifest_path.read_text()).get("generated_at")


def build_sandbox(out: pathlib.Path, registry: dict[str, contracts_mod.Contract]) -> None:
    snapshot_dir = data_mod.SNAPSHOT_DIR
    manifest = Manifest.load(snapshot_dir / "manifest.json")
    fips_of = data_mod.state_fips(snapshot_dir)

    # Which states each contract needs, and with which event types.
    #   full         every event type: a visitor can register any hazard there
    #   hazard-only  the event types of the contracts that name the state
    wanted: dict[str, set[str] | None] = {}
    for c in registry.values():
        for state in c.states:
            fips = fips_of.get(state)
            if fips is None:
                continue
            if len(c.states) <= 2:
                wanted[fips] = None
            elif wanted.get(fips, set()) is not None:
                wanted.setdefault(fips, set()).update(c.event_types)

    buf = io.BytesIO()
    zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)

    add = _adder(zf, ROOT)

    for p in _package_files():
        add(p)
    for pattern in (
        "contracts/*.json",
        "harness_expected/*.json",
        "skills/*.md",
        "experiments/*/ledger.jsonl",
        "experiments/*/ledger.jsonl.anchor.json",
        "experiments/*/test_touches.json",
    ):
        for p in sorted(ROOT.glob(pattern)):
            add(p)
    zf.writestr("experiments/README.md", (ROOT / "experiments" / "README.md").read_text())

    states_info: dict[str, dict] = {}
    if manifest and (snapshot_dir / "census" / "national_county2020.txt").exists():
        zf.writestr("snapshots/manifest.json", _public_manifest(snapshot_dir))
        add(snapshot_dir / "census" / "national_county2020.txt")
        crosswalk = snapshot_dir / "nws" / "zone_county.dbx"
        if crosswalk.exists():
            add(crosswalk)
        state_of = {v: k for k, v in fips_of.items()}
        for fips, keep in sorted(wanted.items()):
            parts = sorted(
                p for p in (snapshot_dir / "storm_events").glob(f"{fips}_*.jsonl")
                if re.fullmatch(r"\d{2}_\d{4}", p.stem)  # not the combined *_extract
            )
            if not parts:
                continue
            raw_bytes = sum(p.stat().st_size for p in parts)
            if keep is None and raw_bytes > FULL_STATE_BUDGET:
                keep = set()
                for c in registry.values():
                    if state_of[fips] in c.states:
                        keep.update(c.event_types)
            rows = 0
            years = []
            for p in parts:
                years.append(int(p.stem.split("_")[1]))
                if keep is None:
                    add(p)
                    with p.open(encoding="utf-8") as fh:
                        rows += sum(1 for line in fh if line.strip())
                else:
                    text, n = _filter_rows(p.read_text(encoding="utf-8"), keep)
                    rows += n
                    zf.writestr(p.relative_to(ROOT).as_posix(), text)
            states_info[fips] = {
                "state": state_of[fips],
                "event_types": None if keep is None else sorted(keep),
                "years": [min(years), max(years)],
                "rows": rows,
                "raw_bytes": raw_bytes,
            }
    # What was packed, readable from inside the sandbox: `sandbox.py` uses it to
    # tell a visitor that a contract's hazard has no rows in a hazard-only state,
    # rather than building an all-zero panel.
    zf.writestr(
        "snapshots/sandbox_coverage.json",
        json.dumps({"states": states_info}, indent=1, sort_keys=True) + "\n",
    )
    zf.close()
    (out / "sandbox.zip").write_bytes(buf.getvalue())

    contracts_info: dict[str, dict] = {}
    for name, c in registry.items():
        needed = [fips_of.get(s) for s in c.states]
        packed = [f for f in needed if f in states_info]
        if not c.states:
            coverage = "missing"  # a national scope is not packed
        elif len(packed) != len(needed):
            coverage = "missing"
        elif all(states_info[f]["event_types"] is None for f in packed):
            coverage = "full"
        else:
            coverage = "hazard-only"
        needs_crosswalk = c.zone_policy == "expand"
        if coverage != "missing" and needs_crosswalk and not (
            snapshot_dir / "nws" / "zone_county.dbx"
        ).exists():
            coverage = "missing"
        try:
            data_version = manifest.digest(data_mod.input_keys(c)) if manifest else None
        except ConnectorError:
            data_version = None
        expected_path = data_mod.paths(c).expected
        blessed = (
            json.loads(expected_path.read_text()).get("_data_version")
            if expected_path.exists() else None
        )
        contracts_info[name] = {
            "coverage": coverage,
            "states": {s: fips_of.get(s) for s in c.states},
            "data_version": data_version,
            "blessed_data_version": blessed,
            "reproducible": bool(
                coverage != "missing" and data_version and data_version == blessed
            ),
        }
        log(f"sandbox: {name}: data {coverage}"
            + ("" if coverage == "missing" else
               f", data version {data_version} "
               + ("matches" if data_version == blessed else "DIFFERS FROM")
               + " the blessed fingerprints"))

    dump(
        out / "sandbox.json",
        {
            "built_at": utc_now(),
            "package_version": __version__,
            "python_built_with": sys.version.split()[0],
            "zip_bytes": len(buf.getvalue()),
            "manifest_generated_at": _generated_at(snapshot_dir / "manifest.json"),
            "states": states_info,
            "contracts": contracts_info,
        },
    )
    log(f"sandbox.zip: {len(buf.getvalue()) / 1e6:.2f} MB")


# ---------------------------------------------------------------------------


def _git_commit() -> str | None:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def build(out: pathlib.Path, *, sandbox: bool = True) -> None:
    out.mkdir(parents=True, exist_ok=True)
    registry = build_contracts(out)
    build_hazards(out)
    build_models(out)
    build_features(out)
    build_ledgers(out, registry)
    build_expected(out, registry)
    datasets = build_panels(out, registry)
    build_tapes(out, registry, datasets)
    build_fleet(out, registry)
    build_briefs(out, registry)
    build_exposure(out)
    build_transcripts(out)
    copy_media(out)
    if sandbox:
        build_sandbox(out, registry)
    dump(out / "build.json", {
        "built_at": utc_now(),
        "package_version": __version__,
        "commit": _git_commit(),
    })
    log(f"done -> {data_mod.relative(out)}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    p.add_argument("--no-sandbox", action="store_true", help="skip the sandbox archive")
    args = p.parse_args(argv)
    build(pathlib.Path(args.out), sandbox=not args.no_sandbox)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
