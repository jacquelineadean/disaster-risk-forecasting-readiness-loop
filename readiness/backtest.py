"""The backtest report: one contract's Phase 1 record, from committed files only.

Plan §2's exit is "published with the ledger", and this is the publication.
Everything on the page comes from files that are in git: the contract, its
ledger and anchor, the snapshot manifest and the blessed Phase 0
fingerprints. No dataset is built, nothing is fitted or scored, no network is
touched and no language model is consulted, so the page renders from a clean
clone and says exactly what the ledger says — including the failures, the
refused source and the number of times the test split was ever touched.

`verify --phase 1` requires the page to embed the test card's hash and the
ledger head, so a report that was rendered before the touch, or after the
ledger moved, is visibly stale rather than quietly wrong.

    readiness backtest -c <contract>   -> experiments/<contract>/backtest.html
                                          experiments/<contract>/backtest.json
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Mapping, Sequence

from readiness import data as data_mod
from readiness.connectors import CONNECTORS
from readiness.connectors.base import Manifest
from readiness.contracts import Contract
from readiness.dashboard import CSS, _esc, reliability_svg
from readiness.engine.features import FEATURE_SETS
from readiness.harness import features as features_mod
from readiness.harness.ledger import ExperimentCard, Ledger

LICENSES_PATH = data_mod.REPO_ROOT / "DATA-LICENSES.md"

#: What Phase 1 deliberately left alone, one sentence each, so the report says
#: what was not tried rather than leaving a reader to wonder.
NOT_TRIED: tuple[tuple[str, str], ...] = (
    (
        "CLIMADA subprocess model",
        "The GPL-3.0 engine stays outside the package: only a pinned event-set layer "
        "produced by a tool run separately is admissible as a feature, and driving "
        "CLIMADA itself from the loop is deferred until that seam has been exercised.",
    ),
    (
        "AIWP reforecasts",
        "Archived GraphCast, Pangu-Weather and FourCastNet hindcasts are a Phase 2 "
        "input; none is admitted until each model's training window has been checked "
        "against the holdout years, because a reforecast that saw them is a leak.",
    ),
    (
        "In-period nowcasts",
        "Anything observed after a period starts is not available when the forecast "
        "is issued; the firewall's one-month lag rules it out by construction, so it "
        "was never a candidate.",
    ),
)

#: Lines of the licence file's attribution block that always apply when the
#: file cannot be read (the browser sandbox packs the package, not the docs).
FALLBACK_ATTRIBUTION: tuple[str, ...] = (
    "Hazard history: NOAA National Centers for Environmental Information, "
    "Storm Events Database (public domain).",
    "Geography: US Census Bureau (public domain).",
    "Weather data by Open-Meteo.com (CC BY 4.0); ERA5 by ECMWF/Copernicus.",
    "Not an official warning product. Official alerts come from the US National "
    "Weather Service and IPAWS.",
)

_STATUS_CLASS = {"REJECTED": "reject", "PASS": "pass", "FAIL": "fail"}


# ---------------------------------------------------------------------------
# facts: everything the page and its JSON twin state, computed once
# ---------------------------------------------------------------------------


def worst_bin(scorecard: Mapping) -> float | None:
    """The largest deviation over populated bins, or None when none is populated."""
    populated = [b for b in scorecard.get("reliability_bins", []) if b.get("populated")]
    if not populated:
        return None
    return max(abs(b["observed_frequency"] - b["mean_forecast"]) for b in populated)


def spec_for(column: str) -> features_mod.FeatureSpec | None:
    """The catalogue spec behind a card's column, if the catalogue still has it."""
    for specs in FEATURE_SETS.values():
        for spec in specs:
            if spec.column == column:
                return spec
    return None


def test_card(
    cards: Sequence[ExperimentCard], contract: Contract
) -> ExperimentCard | None:
    """The test card under the current digest, when there is exactly one."""
    digest = contract.digest()
    mine = [c for c in cards if c.split == "test" and c.contract_digest == digest]
    return mine[0] if len(mine) == 1 else None


class _Layer:
    """A stand-in static source built from a manifest record, for `admit()`.

    The report has no connector output to hand the harness, only the pinned
    record and the vintage its notes carry; that is enough for the admission
    rule, whose words the benchmark row then quotes.
    """

    kind = "static"
    global_coverage = False

    def __init__(self, name: str, key: str, derived_through: int) -> None:
        self.name = name
        self.manifest_keys = (key,)
        self.derived_through = derived_through

    def series(self, region: str, variable: str) -> None:
        return None

    def static(self, region: str) -> None:
        return None


def _connector_name(key: str) -> str | None:
    for name, info in CONNECTORS.items():
        if key.startswith(info.key_prefix):
            return name
    return None


def inadmissible(
    cards: Sequence[ExperimentCard], manifest: Manifest, contract: Contract
) -> list[dict]:
    """Every source the audit refused, from the cards and from the pinned layers.

    A refusal on a card is quoted as recorded. A pinned static layer whose
    manifest notes declare a `derived_through` year is put through the
    harness's own `admit()`, so FEMA NRI shows up as INADMISSIBLE under every
    current contract even though no card could ever have been scored on it.
    """
    rows: dict[str, dict] = {}
    for card in cards:
        audit = (card.scorecard or {}).get("feature_audit") or {}
        for finding in audit.get("findings", []):
            if finding.get("check") != "admission" or finding.get("passed"):
                continue
            match = re.search(r"source '([^']+)'", finding["detail"])
            name = match.group(1) if match else "?"
            rows.setdefault(name, {
                "source": name, "reason": finding["detail"], "recorded": card.experiment_id,
            })
    for key, record in sorted(manifest.records.items()):
        match = re.search(r"derived_through=(\d{4})", record.notes or "")
        name = _connector_name(key)
        if not match or name is None or name in rows:
            continue
        try:
            features_mod.admit(_Layer(name, key, int(match.group(1))), contract)
        except features_mod.FeatureAdmissionError as exc:
            rows[name] = {"source": name, "reason": str(exc), "recorded": key}
    return [rows[name] for name in sorted(rows)]


def attribution(
    contract: Contract,
    *,
    nri_shown: bool,
    climada_used: bool,
    path: pathlib.Path = LICENSES_PATH,
) -> list[str]:
    """The licence file's attribution block, with its conditional lines resolved."""
    if not path.exists():
        return list(FALLBACK_ATTRIBUTION)
    match = re.search(
        r"## Attribution block for published outputs.*?```\n(.*?)```",
        path.read_text(),
        re.S,
    )
    if match is None:
        return list(FALLBACK_ATTRIBUTION)
    entries: list[str] = []
    for line in match.group(1).splitlines():
        if line.startswith("  ") and entries:
            entries[-1] += " " + line.strip()
        elif line.strip():
            entries.append(line.rstrip())
    applies = {
        "[if zone events expanded]": contract.zone_policy == "expand",
        "[if NRI shown]": nri_shown,
        "[if exposure joined]": False,
        # Footprints are an ODbL layer this repository never downloads; the
        # exposure join is USA Structures county counts and nothing else.
        "[if footprints joined]": False,
        "[if risk engine used]": climada_used,
    }
    out = []
    for entry in entries:
        tag = re.match(r"\[if [^\]]+\]", entry)
        if tag is None:
            out.append(entry)
        elif applies.get(tag.group(0), False):
            out.append(entry[tag.end():].strip())
    return out


def _history_row(card: ExperimentCard, contract: Contract) -> dict:
    sc = card.scorecard or {}
    return {
        "id": card.experiment_id,
        "model": f"{card.model}@{card.version}",
        "kwargs": card.data_snapshot.get("model_kwargs", {}),
        "bss": sc.get("brier_skill_score"),
        "auc": sc.get("auc"),
        "worst_bin": worst_bin(sc),
        "status": card.status,
        "current_contract": card.contract_digest == contract.digest(),
        "timestamp": card.timestamp,
    }


def facts(
    contract: Contract, ledger: Ledger, manifest: Manifest, expected: dict | None
) -> dict:
    """Every fact the report states, as plain data; the JSON twin is this verbatim."""
    cards = list(ledger.read())
    card = test_card(cards, contract)
    latest = card or (cards[-1] if cards else None)
    snapshot = latest.data_snapshot if latest is not None else {}
    columns = list((card.scorecard or {}).get("feature_columns") or []) if card else []
    refused = inadmissible(cards, manifest, contract)
    feature_inputs = list(snapshot.get("feature_inputs", []))
    return {
        "contract": contract.name,
        "contract_digest": contract.digest(),
        "ledger": data_mod.relative(ledger.path),
        "ledger_head": ledger.head(),
        "chain": ledger.verify().format(),
        "n_cards": len(cards),
        "data_version": snapshot.get("data_version"),
        "inputs": [_input(k, manifest) for k in snapshot.get("inputs", [])],
        "feature_version": snapshot.get("feature_version"),
        "feature_inputs": [_input(k, manifest) for k in feature_inputs],
        "feature_columns": [
            (spec.to_dict() if (spec := spec_for(c)) else {"column": c}) for c in columns
        ],
        "feature_audit": (card.scorecard or {}).get("feature_audit") if card else None,
        "inadmissible": refused,
        "validate_history": [
            _history_row(c, contract) for c in cards if c.split == "validate"
        ],
        "test_touches": sum(1 for c in cards if c.split == "test"),
        "test_card": card.card_hash if card else "",
        "test": (
            _history_row(card, contract) | {"card_hash": card.card_hash} if card else None
        ),
        "not_tried": [{"what": what, "why": why} for what, why in NOT_TRIED],
        "attribution": attribution(
            contract,
            nri_shown=any(r["source"] == "nri" for r in refused),
            climada_used=any(k.startswith("climada/") for k in feature_inputs),
        ),
        "phase0_expected": expected,
    }


def _input(key: str, manifest: Manifest) -> dict:
    record = manifest.records.get(key)
    return {
        "key": key,
        "sha256": record.sha256 if record else None,
        "source": record.source if record else "not pinned in the committed manifest",
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def _num(value: object, fmt: str) -> str:
    return format(value, fmt) if isinstance(value, (int, float)) else "–"


def _tag(status: str) -> str:
    return f'<span class="tag {_STATUS_CLASS.get(status, "clear")}">{_esc(status)}</span>'


def _inputs_table(rows: Sequence[dict]) -> str:
    body = "".join(
        f"<tr><td><code>{_esc(r['key'])}</code></td>"
        f"<td><code>{_esc((r['sha256'] or '')[:16])}</code></td><td>{_esc(r['source'])}</td></tr>"
        for r in rows
    )
    return (
        "<table><thead><tr><th>manifest key</th><th>sha256</th><th>source</th></tr>"
        f"</thead><tbody>{body}</tbody></table>"
    )


def _data_section(f: dict) -> str:
    feature = (
        f"<p>feature version <code>sha256:{_esc(f['feature_version'])}</code>, hashed over:</p>"
        + _inputs_table(f["feature_inputs"])
        if f["feature_version"]
        else "<p class=\"sub\">No feature sources on the record.</p>"
    )
    return f"""
<h2>Data</h2>
<p>data version <code>sha256:{_esc(f["data_version"] or "–")}</code>, hashed over:</p>
{_inputs_table(f["inputs"])}
{feature}"""


def _features_section(f: dict) -> str:
    rows = "".join(
        f"<tr><td><code>{_esc(s.get('column'))}</code></td><td>{_esc(s.get('source', '–'))}</td>"
        f"<td>{_esc(s.get('variable', '–'))}</td><td>{_esc(s.get('transform', '–'))}</td>"
        f"<td class=\"num\">{_esc(s.get('window_months', '–'))}</td>"
        f"<td class=\"num\">{_esc(s.get('lag_months', '–'))}</td></tr>"
        for s in f["feature_columns"]
    )
    columns = (
        "<table><thead><tr><th>column</th><th>source</th><th>variable</th><th>transform</th>"
        f"<th class=\"num\">window (months)</th><th class=\"num\">lag (months)</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        if rows
        else "<p class=\"sub\">The test card carries no feature columns.</p>"
    )
    audit = f["feature_audit"] or {}
    findings = "".join(
        f'<li><span class="tag {"pass" if a["passed"] else "fail"}">'
        f'{"ok" if a["passed"] else "REFUSED"}</span> {_esc(a["check"])} — {_esc(a["detail"])}</li>'
        for a in audit.get("findings", [])
    )
    audit_html = (
        f'<ul class="checks">{findings}</ul>'
        if findings
        else "<p class=\"sub\">No audit on the test card.</p>"
    )
    refused = "".join(
        f"<tr><td><code>{_esc(r['source'])}</code></td><td>{_tag('INADMISSIBLE')}</td>"
        f"<td>{_esc(r['reason'])}</td><td><code>{_esc(r['recorded'])}</code></td></tr>"
        for r in f["inadmissible"]
    )
    benchmark = (
        "<h3>Benchmark layers refused by the firewall</h3>"
        "<table><thead><tr><th>source</th><th>admission</th><th>why</th><th>recorded on</th>"
        f"</tr></thead><tbody>{refused}</tbody></table>"
        if refused
        else ""
    )
    return f"""
<h2>Features</h2>
<p class="sub">Every column is built by the harness under its own cutoff (period start minus the lag) and audited under poisoning before any model is fitted.</p>
{columns}
<h3>Audit on the test card</h3>
{audit_html}
{benchmark}"""


def _history_section(f: dict) -> str:
    rows = "".join(
        f"<tr><td>{_esc(r['id'])}</td><td>{_esc(r['model'])}"
        f"{'' if r['current_contract'] else ' <small>(earlier contract)</small>'}</td>"
        f"<td><code>{_esc(json.dumps(r['kwargs'], sort_keys=True))}</code></td>"
        f"<td class=\"num\">{_num(r['bss'], '+.4f')}</td>"
        f"<td class=\"num\">{_num(r['auc'], '.4f')}</td>"
        f"<td class=\"num\">{_num(r['worst_bin'], '.4f')}</td><td>{_tag(r['status'])}</td></tr>"
        for r in f["validate_history"]
    )
    table = (
        "<table><thead><tr><th>id</th><th>model</th><th>arguments</th><th class=\"num\">BSS</th>"
        "<th class=\"num\">AUC</th><th class=\"num\">worst bin</th><th>verdict</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        if rows
        else "<p class=\"sub\">No validate cards yet.</p>"
    )
    return f"""
<h2>Validate history</h2>
<p class="sub">Every validate card in the ledger, failures included. A card is an experiment, not an advertisement.</p>
{table}"""


def _test_section(f: dict, card: ExperimentCard | None, contract: Contract) -> str:
    touches = f["test_touches"]
    if card is None:
        return f"""
<h2>Test</h2>
<p class="sub">No test card under contract sha256:{_esc(f["contract_digest"])}. Test touches in the ledger so far: {touches}.</p>"""
    sc = card.scorecard or {}
    checks = "".join(
        f'<li><span class="tag {"pass" if c["passed"] else "fail"}">'
        f'{"PASS" if c["passed"] else "FAIL"}</span> {_esc(c["name"])} — {_esc(c["detail"])}</li>'
        for c in (card.verdict or {}).get("checks", [])
    )
    findings = "".join(
        f'<li><span class="tag {"tripped" if x["tripped"] else "clear"}">'
        f'{"TRIPPED" if x["tripped"] else "clear"}</span> {_esc(x["check"])} — '
        f'{_esc(x["detail"])}</li>'
        for x in (card.canary or {}).get("findings", [])
    )
    bins = "".join(
        f'<tr><td>{b["lower"]:.1f}–{b["upper"]:.1f}</td><td class="num">{b["count"]:,}</td>'
        f'<td class="num">{b["mean_forecast"]:.4f}</td>'
        f'<td class="num">{b["observed_frequency"]:.4f}</td>'
        f'<td class="num">{b["observed_frequency"] - b["mean_forecast"]:+.4f}'
        f'{"" if b.get("populated") else " <small>(thin)</small>"}</td></tr>'
        for b in sc.get("reliability_bins", [])
        if b["count"]
    )
    return f"""
<h2>Test</h2>
<section class="card" id="{_esc(card.experiment_id)}">
  <header>
    <h3>{_esc(card.experiment_id)} · {_esc(card.model)}@{_esc(card.version)}</h3>
    <div>{_tag(card.status)} <span class="sub">test · {_esc(card.timestamp)}</span></div>
  </header>
  <div class="grid">
    <div>
      {reliability_svg(sc.get("reliability_bins", []), contract.reliability_tolerance_pp)}
      <dl class="kv">
        <dt>Brier</dt><dd>{_num(sc.get("brier_score"), ".6f")} (reference {_num(sc.get("brier_score_reference"), ".6f")})</dd>
        <dt>BSS</dt><dd>{_num(sc.get("brier_skill_score"), "+.4f")}</dd>
        <dt>AUC</dt><dd>{_num(sc.get("auc"), ".4f")}</dd>
        <dt>sharpness</dt><dd>{_num(sc.get("sharpness"), ".4f")}</dd>
        <dt>reliability</dt><dd>{_num(sc.get("reliability"), ".6f")}</dd>
        <dt>resolution</dt><dd>{_num(sc.get("resolution"), ".6f")}</dd>
        <dt>uncertainty</dt><dd>{_num(sc.get("uncertainty"), ".6f")}</dd>
        <dt>units</dt><dd>{sc.get("n_units", 0):,} ({sc.get("n_positive", 0):,} positive, base rate {_num(sc.get("base_rate"), ".4f")})</dd>
        <dt>arguments</dt><dd><code>{_esc(json.dumps(card.data_snapshot.get("model_kwargs", {}), sort_keys=True))}</code></dd>
        <dt>card hash</dt><dd><code>{_esc(card.card_hash)}</code></dd>
        <dt>test touches</dt><dd>{touches} in the ledger, ever</dd>
      </dl>
    </div>
    <div>
      <div class="prose">
        <p><strong>Changed.</strong> {_esc(card.changed)}</p>
        <p><strong>Hypothesis.</strong> {_esc(card.hypothesis)}</p>
        <p><strong>Outcome.</strong> {_esc(card.outcome)}</p>
      </div>
      <h4 style="margin:14px 0 4px;font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)">Contract</h4>
      <ul class="checks">{checks}</ul>
      <h4 style="margin:14px 0 4px;font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)">Leakage canary</h4>
      <ul class="checks">{findings}</ul>
      <h4 style="margin:14px 0 4px;font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)">Reliability bins</h4>
      <table><thead><tr><th>bin</th><th class="num">n</th><th class="num">forecast</th><th class="num">observed</th><th class="num">dev</th></tr></thead><tbody>{bins}</tbody></table>
    </div>
  </div>
</section>"""


def _anchor_section(expected: dict | None) -> str:
    if not expected:
        return ""
    rows = "".join(
        f"<tr><td>{_esc(name)}</td><td class=\"num\">{_num(fp.get('brier_skill_score'), '+.4f')}</td>"
        f"<td class=\"num\">{_num(fp.get('auc'), '.4f')}</td>"
        f"<td><code>{_esc(fp.get('reliability_bins_sha256', ''))}</code></td></tr>"
        for name, fp in sorted(expected.items())
        if isinstance(fp, dict)
    )
    return f"""
<h2>Phase 0 anchor</h2>
<p class="sub">The blessed baseline fingerprints (data version <code>{_esc(expected.get("_data_version", "–"))}</code>, contract <code>{_esc(expected.get("_contract", "–"))}</code>) that <code>readiness verify --phase 0</code> reproduces bit-for-bit.</p>
<table><thead><tr><th>model</th><th class="num">BSS</th><th class="num">AUC</th><th>bins sha256</th></tr></thead><tbody>{rows}</tbody></table>"""


def render(
    contract: Contract, ledger: Ledger, manifest: Manifest, expected: dict | None = None
) -> str:
    """The self-contained page. Same style as the dashboard; no scripts, no assets."""
    f = facts(contract, ledger, manifest, expected)
    return _html(contract, f, test_card(list(ledger.read()), contract))


def _html(contract: Contract, f: dict, card: ExperimentCard | None) -> str:
    not_tried = "".join(
        f"<li><strong>{_esc(n['what'])}.</strong> {_esc(n['why'])}</li>" for n in f["not_tried"]
    )
    attribution_block = "\n".join(_esc(line) for line in f["attribution"])
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="ledger-head" content="{_esc(f["ledger_head"])}">
<meta name="test-card" content="{_esc(f["test_card"])}">
<title>{_esc(contract.name)} · backtest</title><style>{CSS}</style></head>
<body><main>
<h1>{_esc(contract.name)} · backtest</h1>
<p class="sub">{_esc(contract.description or contract.hazard)} · contract sha256:{_esc(f["contract_digest"])} · ledger head {_esc(f["ledger_head"][:16])}…</p>
<pre>{_esc(contract.describe())}</pre>
{_data_section(f)}
{_features_section(f)}
{_history_section(f)}
{_test_section(f, card, contract)}
<h2>Deliberately not tried</h2>
<ul class="index">{not_tried}</ul>
{_anchor_section(f["phase0_expected"])}
<h2>Attribution</h2>
<pre>{attribution_block}</pre>
<footer>Generated by <code>readiness backtest</code> from <code>{_esc(f["ledger"])}</code> and <code>snapshots/manifest.json</code>; {_esc(f["chain"])}. Nothing here was recomputed. Decision support, never a warning channel.</footer>
</main></body></html>
"""


def write(contract: Contract, out: pathlib.Path | None = None) -> pathlib.Path:
    """Write the page and its JSON twin next to the contract's ledger."""
    where = data_mod.paths(contract)
    ledger = Ledger(where.ledger)
    manifest = Manifest.load(data_mod.MANIFEST_PATH)
    expected = json.loads(where.expected.read_text()) if where.expected.exists() else None
    out = out or (where.directory / "backtest.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    f = facts(contract, ledger, manifest, expected)
    page = _html(contract, f, test_card(list(ledger.read()), contract))
    out.write_text(page, encoding="utf-8")
    out.with_suffix(".json").write_text(
        json.dumps(f, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return out
