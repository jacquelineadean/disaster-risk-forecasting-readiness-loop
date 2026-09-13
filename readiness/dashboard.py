"""A static, self-contained HTML view of one contract's experiment ledger.

Everything on the page comes from files that are committed: the contract, its
ledger and its anchor. No data pull, no scoring, no JavaScript, no external
assets — so it renders from a clean clone, offline, and prints. The point is
to make the reliability diagram *visible*: a calibration failure that is one
number in a table is a shape on a chart.

    readiness dashboard -c <contract>           -> experiments/<contract>/dashboard.html
    readiness dashboard --all                   -> ... plus experiments/index.html
"""

from __future__ import annotations

import html
import math
import pathlib
from typing import Iterable

from readiness import contracts as contracts_mod
from readiness import data as data_mod
from readiness.contracts import Contract
from readiness.harness.ledger import ExperimentCard, Ledger

CSS = """
:root{--ink:#1c1a16;--muted:#6b6557;--rule:#e3ddd0;--paper:#faf7f2;--card:#fff;
  --accent:#2b5aa8;--pass:#2f7d4f;--fail:#b03a2e;--reject:#8a1c9e;--band:#e8effa}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:1100px;margin:0 auto;padding:32px 24px 64px}
h1{font-size:30px;margin:0 0 4px;font-weight:600;letter-spacing:-.01em}
h2{font-size:20px;margin:36px 0 12px;font-weight:600}
.sub{color:var(--muted);margin:0 0 18px}
pre{background:var(--card);border:1px solid var(--rule);padding:14px 16px;overflow-x:auto;
  font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;margin:0}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--rule)}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--rule);font-size:14px}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.06em}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.tag{display:inline-block;padding:2px 8px;border-radius:3px;font-size:12px;font-weight:600;
  letter-spacing:.04em}
.tag.pass{background:#e2f2e8;color:var(--pass)}
.tag.fail{background:#f9e3e0;color:var(--fail)}
.tag.reject{background:#f0e0f4;color:var(--reject)}
.tag.clear{background:#eef0ee;color:var(--muted)}
.tag.tripped{background:#f0e0f4;color:var(--reject)}
.card{background:var(--card);border:1px solid var(--rule);margin:18px 0;padding:20px 22px}
.card header{display:flex;justify-content:space-between;align-items:baseline;gap:16px;
  flex-wrap:wrap;margin-bottom:12px}
.card header h3{margin:0;font-size:18px;font-weight:600}
.grid{display:grid;grid-template-columns:320px minmax(0,1fr);gap:22px 28px;align-items:start}
@media (max-width:760px){.grid{grid-template-columns:minmax(0,1fr)}}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;font-size:14px}
.kv dt{color:var(--muted)}
.kv dd{margin:0;font-variant-numeric:tabular-nums}
.checks{list-style:none;padding:0;margin:8px 0 0;font-size:13.5px}
.checks li{padding:4px 0;border-top:1px solid var(--rule)}
.checks li:first-child{border-top:0}
.prose{font-size:14px;color:#33302a}
.prose p{margin:6px 0}
.prose strong{font-weight:600}
svg text{font:11px -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  fill:var(--muted)}
.chain{margin-top:24px;font-size:14px}
.chain.ok{color:var(--pass)}.chain.bad{color:var(--fail)}
.index li{margin:6px 0}
footer{margin-top:40px;color:var(--muted);font-size:13px;border-top:1px solid var(--rule);
  padding-top:12px}
@media print{body{background:#fff}.card{break-inside:avoid}}
"""


def _esc(s: object) -> str:
    return html.escape(str(s))


def reliability_svg(bins: Iterable[dict], tolerance: float, size: int = 300) -> str:
    """Forecast probability on x, observed frequency on y; the diagonal is calibration."""
    pad = 46
    inner = size - 2 * pad

    def sx(v: float) -> float:
        return pad + v * inner

    def sy(v: float) -> float:
        return pad + (1 - v) * inner

    bins = list(bins)
    counts = [b["count"] for b in bins if b["count"]]
    max_count = max(counts) if counts else 1
    parts = [
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" '
        'aria-label="reliability diagram">',
        f'<rect x="{pad}" y="{pad}" width="{inner}" height="{inner}" fill="#fff" '
        'stroke="#e3ddd0"/>',
    ]
    # tolerance band around the diagonal
    lo, hi = -tolerance, tolerance
    band = " ".join(
        f"{sx(x):.1f},{sy(min(1, max(0, x + d))):.1f}"
        for x, d in ((0, hi), (1, hi), (1, lo), (0, lo))
    )
    parts.append(f'<polygon points="{band}" fill="var(--band)" opacity="0.9"/>')
    parts.append(
        f'<line x1="{sx(0):.1f}" y1="{sy(0):.1f}" x2="{sx(1):.1f}" y2="{sy(1):.1f}" '
        'stroke="#9a948a" stroke-dasharray="4 3"/>'
    )
    for t in (0, 0.25, 0.5, 0.75, 1):
        parts.append(
            f'<text x="{sx(t):.1f}" y="{size - 30}" text-anchor="middle">{t:g}</text>'
        )
        parts.append(
            f'<text x="{pad - 8}" y="{sy(t) + 4:.1f}" text-anchor="end">{t:g}</text>'
        )
    parts.append(
        f'<text x="{size / 2:.0f}" y="{size - 10}" text-anchor="middle" '
        'style="font-weight:600">forecast probability</text>'
    )
    parts.append(
        f'<text transform="translate(10,{size / 2:.0f}) rotate(-90)" text-anchor="middle" '
        'style="font-weight:600">observed frequency</text>'
    )
    for b in bins:
        if not b["count"]:
            continue
        r = 4 + 10 * math.sqrt(b["count"] / max_count)
        fill = "var(--accent)" if b.get("populated") else "none"
        parts.append(
            f'<circle cx="{sx(b["mean_forecast"]):.1f}" cy="{sy(b["observed_frequency"]):.1f}" '
            f'r="{r:.1f}" fill="{fill}" stroke="var(--accent)" stroke-width="1.5" '
            f'opacity="0.85"><title>[{b["lower"]:.1f},{b["upper"]:.1f}) n={b["count"]:,} '
            f'forecast {b["mean_forecast"]:.3f} observed {b["observed_frequency"]:.3f}'
            "</title></circle>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _verdict_tag(card: ExperimentCard) -> str:
    if card.canary and card.canary.get("rejected"):
        return '<span class="tag reject">REJECTED</span>'
    if (card.verdict or {}).get("passed"):
        return '<span class="tag pass">PASS</span>'
    return '<span class="tag fail">FAIL</span>'


def _experiment(card: ExperimentCard, contract: Contract) -> str:
    sc = card.scorecard or {}
    checks = "".join(
        f'<li><span class="tag {"pass" if c["passed"] else "fail"}">'
        f'{"PASS" if c["passed"] else "FAIL"}</span> {_esc(c["name"])} — {_esc(c["detail"])}</li>'
        for c in (card.verdict or {}).get("checks", [])
    )
    findings = "".join(
        f'<li><span class="tag {"tripped" if f["tripped"] else "clear"}">'
        f'{"TRIPPED" if f["tripped"] else "clear"}</span> {_esc(f["check"])} — '
        f'{_esc(f["detail"])}</li>'
        for f in (card.canary or {}).get("findings", [])
    )
    bins = [b for b in sc.get("reliability_bins", []) if b["count"]]
    rows = "".join(
        f'<tr><td>{b["lower"]:.1f}–{b["upper"]:.1f}</td><td class="num">{b["count"]:,}</td>'
        f'<td class="num">{b["mean_forecast"]:.4f}</td>'
        f'<td class="num">{b["observed_frequency"]:.4f}</td>'
        f'<td class="num">{b["observed_frequency"] - b["mean_forecast"]:+.4f}'
        f'{"" if b.get("populated") else " <small>(thin)</small>"}</td></tr>'
        for b in bins
    )
    return f"""
<section class="card" id="{_esc(card.experiment_id)}">
  <header>
    <h3>{_esc(card.experiment_id)} · {_esc(card.model)}@{_esc(card.version)}</h3>
    <div>{_verdict_tag(card)} <span class="sub">{_esc(card.split)} · {_esc(card.timestamp)}</span></div>
  </header>
  <div class="grid">
    <div>
      {reliability_svg(sc.get("reliability_bins", []), contract.reliability_tolerance_pp)}
      <dl class="kv">
        <dt>Brier</dt><dd>{sc.get("brier_score", float("nan")):.6f} (reference {sc.get("brier_score_reference", float("nan")):.6f})</dd>
        <dt>BSS</dt><dd>{sc.get("brier_skill_score", float("nan")):+.4f}</dd>
        <dt>AUC</dt><dd>{sc.get("auc", float("nan")):.4f}</dd>
        <dt>sharpness</dt><dd>{sc.get("sharpness", float("nan")):.4f}</dd>
        <dt>units</dt><dd>{sc.get("n_units", 0):,} ({sc.get("n_positive", 0):,} positive, base rate {sc.get("base_rate", 0):.4f})</dd>
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
      <table><thead><tr><th>bin</th><th class="num">n</th><th class="num">forecast</th><th class="num">observed</th><th class="num">dev</th></tr></thead><tbody>{rows}</tbody></table>
    </div>
  </div>
</section>"""


def render(contract: Contract, ledger: Ledger) -> str:
    cards = list(ledger.read())
    status = ledger.verify()
    rows = "".join(
        f'<tr><td><a href="#{_esc(c.experiment_id)}">{_esc(c.experiment_id)}</a></td>'
        f"<td>{_esc(c.model)}@{_esc(c.version)}</td><td>{_esc(c.split)}</td>"
        f'<td class="num">{(c.scorecard or {}).get("brier_skill_score", float("nan")):+.4f}</td>'
        f'<td class="num">{(c.scorecard or {}).get("auc", float("nan")):.4f}</td>'
        f'<td class="num">{(c.scorecard or {}).get("brier_score", float("nan")):.6f}</td>'
        f"<td>{_verdict_tag(c)}</td></tr>"
        for c in cards
    )
    body = "".join(_experiment(c, contract) for c in cards) or (
        '<p class="sub">No experiments yet. Run <code>readiness loop -c '
        f"{_esc(contract.name)}</code>.</p>"
    )
    chain = (
        f'<p class="chain {"ok" if status.valid else "bad"}">{_esc(status.format())}</p>'
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(contract.name)} · readiness ledger</title><style>{CSS}</style></head>
<body><main>
<h1>{_esc(contract.name)}</h1>
<p class="sub">{_esc(contract.description or contract.hazard)} · contract sha256:{_esc(contract.digest())}</p>
<pre>{_esc(contract.describe())}</pre>
<h2>Experiments</h2>
<table><thead><tr><th>id</th><th>model</th><th>split</th><th class="num">BSS</th><th class="num">AUC</th><th class="num">Brier</th><th>verdict</th></tr></thead><tbody>{rows}</tbody></table>
{chain}
{body}
<footer>Generated by <code>readiness dashboard</code> from <code>{_esc(data_mod.relative(ledger.path))}</code>. Every number is from the ledger; nothing here was recomputed. Decision support, never a warning channel.</footer>
</main></body></html>
"""


def render_index(pages: list[tuple[Contract, pathlib.Path, Ledger]]) -> str:
    items = []
    for contract, path, ledger in pages:
        cards = list(ledger.read())
        passed = sum(1 for c in cards if (c.verdict or {}).get("passed")
                     and not (c.canary or {}).get("rejected"))
        rejected = sum(1 for c in cards if (c.canary or {}).get("rejected"))
        items.append(
            f'<li><a href="{_esc(path.parent.name)}/{_esc(path.name)}"><strong>{_esc(contract.name)}</strong></a> '
            f"— {_esc(contract.hazard)}, {_esc(contract.scope_label)}, {_esc(contract.period)}ly · "
            f"{len(cards)} experiment(s), {passed} passed, {rejected} rejected by the canary"
            f'<br><span class="sub">{_esc(contract.description)}</span></li>'
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>readiness ledgers</title><style>{CSS}</style></head>
<body><main><h1>Experiment ledgers</h1><p class="sub">One ledger per registered contract.</p>
<ul class="index">{"".join(items)}</ul>
<footer>Generated by <code>readiness dashboard --all</code>.</footer></main></body></html>
"""


def write(contract: Contract, out: pathlib.Path | None = None) -> pathlib.Path:
    where = data_mod.paths(contract)
    ledger = Ledger(where.ledger)
    out = out or (where.directory / "dashboard.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(contract, ledger), encoding="utf-8")
    return out


def write_all(names: Iterable[str] | None = None) -> list[pathlib.Path]:
    registry = contracts_mod.registered()
    chosen = [registry[n] for n in (names or registry)]
    pages = []
    written = []
    for contract in chosen:
        path = write(contract)
        pages.append((contract, path, Ledger(data_mod.paths(contract).ledger)))
        written.append(path)
    index = data_mod.experiments_root() / "index.html"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(render_index(pages), encoding="utf-8")
    written.append(index)
    return written
