/* Shared helpers for the overview website. No dependencies. */
(function () {
  "use strict";

  const RL = (window.RL = window.RL || {});
  RL.REPO = "https://github.com/jacquelineadean/disaster-risk-forecasting-readiness-loop";
  RL.GEN = "generated/";

  const _cache = new Map();
  RL.fetchJSON = function (name) {
    if (!_cache.has(name)) {
      _cache.set(
        name,
        fetch(RL.GEN + name, { cache: "no-cache" }).then((r) => {
          if (!r.ok) throw new Error(`${name}: HTTP ${r.status}`);
          return r.json();
        })
      );
    }
    return _cache.get(name);
  };

  RL.esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // --- number formatting (matching the CLI's own formats) ------------------
  const fmt = (RL.fmt = {});
  fmt.int = (n) => (n == null ? "–" : Number(n).toLocaleString("en-US"));
  fmt.f = (x, d) => (x == null || Number.isNaN(x) ? "–" : Number(x).toFixed(d));
  fmt.f4 = (x) => fmt.f(x, 4);
  fmt.f6 = (x) => fmt.f(x, 6);
  fmt.signed4 = (x) => (x == null || Number.isNaN(x) ? "–" : (x >= 0 ? "+" : "") + Number(x).toFixed(4));
  fmt.pct = (x, d = 1) => (x == null ? "–" : (100 * x).toFixed(d) + "%");
  fmt.usd = (x) => "$" + Number(x).toLocaleString("en-US", { maximumFractionDigits: 0 });

  // --- terminal transcripts ------------------------------------------------
  // The same few tokens tools/demo/capture.py tints, so the site's frames read
  // like the captured screenshots.
  const TOKENS = /\[ok\]|\[PASS\]|\[FAIL\]|\bPASS\b|\bFAIL\b|REJECTED|TRIPPED|WARNING|clear\]/g;
  const CLASS = { "[ok]": "ok", "[PASS]": "ok", PASS: "ok", "[FAIL]": "fail", FAIL: "fail", REJECTED: "rej", TRIPPED: "rej", WARNING: "warn", "clear]": "dim" };
  RL.colourLine = function (line) {
    if (line.startsWith("$ ")) {
      return `<span class="p">$</span> <span class="c">${RL.esc(line.slice(2))}</span>`;
    }
    return RL.esc(line).replace(TOKENS, (t) => `<span class="${CLASS[t]}">${t}</span>`);
  };
  RL.colourise = (text) => String(text).replace(/\n$/, "").split("\n").map(RL.colourLine).join("\n");

  RL.terminal = function (title, text, opts = {}) {
    const id = opts.id ? ` id="${RL.esc(opts.id)}"` : "";
    const cls = opts.live ? " live" : "";
    const actions = opts.actions || "";
    return `<div class="term${cls}"${id}><div class="tbar"><i></i><i></i><i></i><span class="t">${RL.esc(title)}</span><span class="act">${actions}</span></div><pre>${opts.raw ? text : RL.colourise(text)}</pre></div>`;
  };

  // --- reliability diagram: a port of readiness.dashboard.reliability_svg ----
  RL.reliabilitySVG = function (bins, tolerance, size = 300, opts = {}) {
    const pad = 46, inner = size - 2 * pad;
    const sx = (v) => pad + v * inner;
    const sy = (v) => pad + (1 - v) * inner;
    const filled = (bins || []).filter((b) => b.count);
    const maxCount = filled.length ? Math.max(...filled.map((b) => b.count)) : 1;
    const parts = [];
    parts.push(`<svg class="reliability" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img" aria-label="reliability diagram">`);
    parts.push(`<rect class="frame" x="${pad}" y="${pad}" width="${inner}" height="${inner}"/>`);
    const clamp = (v) => Math.min(1, Math.max(0, v));
    const band = [[0, tolerance], [1, tolerance], [1, -tolerance], [0, -tolerance]]
      .map(([x, d]) => `${sx(x).toFixed(1)},${sy(clamp(x + d)).toFixed(1)}`).join(" ");
    parts.push(`<polygon class="band" points="${band}"/>`);
    parts.push(`<line class="diag" x1="${sx(0).toFixed(1)}" y1="${sy(0).toFixed(1)}" x2="${sx(1).toFixed(1)}" y2="${sy(1).toFixed(1)}"/>`);
    for (const t of [0, 0.25, 0.5, 0.75, 1]) {
      parts.push(`<text x="${sx(t).toFixed(1)}" y="${size - 30}" text-anchor="middle">${t}</text>`);
      parts.push(`<text x="${pad - 8}" y="${(sy(t) + 4).toFixed(1)}" text-anchor="end">${t}</text>`);
    }
    parts.push(`<text x="${(size / 2).toFixed(0)}" y="${size - 10}" text-anchor="middle" style="font-weight:600">forecast probability</text>`);
    parts.push(`<text transform="translate(10,${(size / 2).toFixed(0)}) rotate(-90)" text-anchor="middle" style="font-weight:600">observed frequency</text>`);
    if (opts.ghost) {
      for (const b of opts.ghost.filter((b) => b.count)) {
        parts.push(`<circle cx="${sx(b.mean_forecast).toFixed(1)}" cy="${sy(b.observed_frequency).toFixed(1)}" r="4" fill="none" stroke="#b8b1a3" stroke-dasharray="2 2"/>`);
      }
    }
    for (const b of filled) {
      const r = 4 + 10 * Math.sqrt(b.count / maxCount);
      const thin = b.populated ? "" : " thin";
      parts.push(`<circle class="pt${thin}" cx="${sx(b.mean_forecast).toFixed(1)}" cy="${sy(b.observed_frequency).toFixed(1)}" r="${r.toFixed(1)}"><title>[${b.lower.toFixed(1)},${b.upper.toFixed(1)}) n=${fmt.int(b.count)} forecast ${b.mean_forecast.toFixed(3)} observed ${b.observed_frequency.toFixed(3)}${b.populated ? "" : " (thin)"}</title></circle>`);
    }
    parts.push("</svg>");
    return parts.join("");
  };

  RL.binsTable = function (bins) {
    const rows = (bins || []).filter((b) => b.count).map((b) =>
      `<tr><td>${b.lower.toFixed(1)}–${b.upper.toFixed(1)}</td><td class="num">${fmt.int(b.count)}</td><td class="num">${fmt.f4(b.mean_forecast)}</td><td class="num">${fmt.f4(b.observed_frequency)}</td><td class="num">${fmt.signed4(b.observed_frequency - b.mean_forecast)}${b.populated ? "" : ' <small>(thin)</small>'}</td></tr>`
    ).join("");
    return `<div class="table-wrap"><table><thead><tr><th>bin</th><th class="num">n</th><th class="num">forecast</th><th class="num">observed</th><th class="num">dev</th></tr></thead><tbody>${rows || '<tr><td colspan="5" class="muted">no populated bins</td></tr>'}</tbody></table></div>`;
  };

  // --- verdicts ---------------------------------------------------------------
  RL.verdictOf = function (card) {
    if (card.canary && card.canary.rejected) return "REJECTED";
    if (card.verdict && card.verdict.passed) return "PASS";
    return "FAIL";
  };
  RL.verdictTag = function (card) {
    const v = RL.verdictOf(card);
    const cls = v === "REJECTED" ? "reject" : v === "PASS" ? "pass" : "fail";
    return `<span class="tag ${cls}">${v}</span>`;
  };
  RL.checksList = (checks) =>
    `<ul class="checks">${(checks || []).map((c) => `<li><span class="tag ${c.passed ? "pass" : "fail"}">${c.passed ? "PASS" : "FAIL"}</span><span><strong>${RL.esc(c.name)}</strong> — ${RL.esc(c.detail)}</span></li>`).join("")}</ul>`;
  RL.findingsList = (findings) =>
    `<ul class="checks">${(findings || []).map((f) => `<li><span class="tag ${f.tripped ? "tripped" : "clear"}">${f.tripped ? "TRIPPED" : "clear"}</span><span><strong>${RL.esc(f.check)}</strong> — ${RL.esc(f.detail)}</span></li>`).join("")}</ul>`;

  RL.summaryTable = function (cards, opts = {}) {
    const rows = cards.map((c) => {
      const sc = c.scorecard || {};
      const id = opts.link ? `<a href="${opts.link}#${RL.esc(c.experiment_id)}">${RL.esc(c.experiment_id)}</a>` : RL.esc(c.experiment_id);
      return `<tr><td class="nowrap">${id}</td><td class="nowrap"><code>${RL.esc(c.model)}@${RL.esc(c.version)}</code></td><td>${RL.esc(c.split)}</td><td class="num">${fmt.signed4(sc.brier_skill_score)}</td><td class="num">${fmt.f4(sc.auc)}</td><td class="num">${fmt.f6(sc.brier_score)}</td><td>${RL.verdictTag(c)}</td></tr>`;
    }).join("");
    return `<div class="table-wrap"><table class="tabular"><thead><tr><th>id</th><th>model</th><th>split</th><th class="num">BSS</th><th class="num">AUC</th><th class="num">Brier</th><th>verdict</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  };

  // --- the split timeline of a contract, as an SVG -----------------------------
  RL.splitsSVG = function (contract, width = 640) {
    const s = contract.splits;
    const y0 = Math.min(s.train[0], s.validate[0], s.test[0]);
    const y1 = Math.max(s.train[1], s.validate[1], s.test[1]) + 1;
    const pad = 12, h = 74, barY = 22, barH = 26;
    const x = (y) => pad + ((y - y0) / (y1 - y0)) * (width - 2 * pad);
    const seg = (name, [a, b], cls) => {
      const w = x(b + 1) - x(a);
      return `<rect class="${cls}" x="${x(a).toFixed(1)}" y="${barY}" width="${w.toFixed(1)}" height="${barH}" stroke="var(--rule)"/>` +
        `<text class="label" x="${(x(a) + w / 2).toFixed(1)}" y="${barY + 17}" text-anchor="middle">${name}</text>` +
        `<text class="sub" x="${x(a).toFixed(1)}" y="${barY + barH + 16}">${a}</text>` +
        `<text class="sub" x="${x(b + 1).toFixed(1)}" y="${barY + barH + 16}" text-anchor="end">${b}</text>`;
    };
    const touches = contract.test_touch_budget;
    return `<svg viewBox="0 0 ${width} ${h}" width="${width}" height="${h}" role="img" aria-label="locked splits">` +
      seg(`train · ${s.train[1] - s.train[0] + 1}y`, s.train, "train") +
      seg(`validate · ${s.validate[1] - s.validate[0] + 1}y`, s.validate, "validate") +
      seg(`test · ${touches} touch`, s.test, "test") +
      `<text class="sub" x="${width - pad}" y="12" text-anchor="end">record trusted from 1996</text></svg>`;
  };

  // --- SHA-256 over text, via Web Crypto -------------------------------------
  RL.sha256Hex = async function (text) {
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, "0")).join("");
  };

  // --- ledger chain verification, from the raw JSONL lines -------------------
  // A card's hash covers its canonical JSON payload — every field except
  // card_hash, keys sorted, no whitespace. That is exactly how Ledger.append
  // writes each line, so removing the card_hash member from the raw line gives
  // the hashed bytes back without re-serialising anything.
  RL.GENESIS = "0".repeat(64);
  RL.hashedPayload = (raw) => raw.replace(/,"card_hash":"[0-9a-f]{64}"/, "");
  RL.verifyChain = async function (rawLines, anchor) {
    let prev = RL.GENESIS, n = 0;
    const cards = rawLines.map((l) => JSON.parse(l));
    for (let i = 0; i < cards.length; i++) {
      n = i + 1;
      const card = cards[i];
      if (card.prev_hash !== prev) {
        return { valid: false, n_cards: n, broken_at: i, reason: `prev_hash ${card.prev_hash.slice(0, 12)}... does not match preceding card ${prev.slice(0, 12)}...` };
      }
      const recomputed = await RL.sha256Hex(RL.hashedPayload(rawLines[i]));
      if (recomputed !== card.card_hash) {
        return { valid: false, n_cards: n, broken_at: i, reason: `card contents were modified after sealing (stored ${card.card_hash.slice(0, 12)}..., recomputed ${recomputed.slice(0, 12)}...)` };
      }
      prev = card.card_hash;
    }
    if (anchor == null) {
      if (n === 0) return { valid: true, n_cards: 0 };
      return { valid: false, n_cards: n, broken_at: n, reason: "anchor file is missing; tail truncation cannot be ruled out" };
    }
    if (anchor.n_cards !== n || anchor.head !== prev) {
      return { valid: false, n_cards: n, broken_at: n, reason: `anchor expects ${anchor.n_cards} card(s) ending at ${String(anchor.head).slice(0, 12)}..., but the ledger holds ${n} ending at ${prev.slice(0, 12)}... — cards have been removed from the end` };
    }
    return { valid: true, n_cards: n };
  };
  RL.chainText = (st) => st.valid
    ? `ledger chain intact: ${st.n_cards} card(s)`
    : `ledger chain BROKEN at card index ${st.broken_at}: ${st.reason}\n  history has been edited or reordered; the scores above this point cannot be trusted`;

  // --- deep links into the sandbox ---------------------------------------------
  RL.sandboxLink = (argv, extra = "") => `playground.html?cmd=${encodeURIComponent(argv)}${extra}`;
  RL.runLink = (argv, label) =>
    `<a class="btn small secondary" href="${RL.sandboxLink(argv)}">${label || "Run in the sandbox"} →</a>`;

  // --- page boot: active nav, build stamp ------------------------------------
  document.addEventListener("DOMContentLoaded", () => {
    const page = document.body.dataset.page;
    document.querySelectorAll("nav.site a[data-page]").forEach((a) => {
      if (a.dataset.page === page) a.classList.add("active");
    });
    const stamp = document.querySelector("[data-built]");
    if (stamp) {
      RL.fetchJSON("build.json").then((b) => {
        const when = b.built_at ? new Date(b.built_at).toISOString().slice(0, 10) : "";
        stamp.textContent = `Built ${when}${b.commit ? " from " + b.commit : ""} · readiness ${b.package_version}`;
      }).catch(() => { stamp.textContent = "Run `make site` to generate the data this page reads."; });
    }
    // mark report links according to whether the copy exists
    document.querySelectorAll("a[data-report]").forEach((a) => {
      a.href = "generated/report/index.html" + (a.dataset.report || "");
    });
  });
})();
