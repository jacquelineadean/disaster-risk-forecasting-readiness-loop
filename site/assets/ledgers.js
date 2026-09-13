/* The ledger explorer: committed cards, reliability diagrams, and a chain
   verification that runs in the browser over the raw JSONL lines. */
(async function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const host = $("#ledger");
  let contracts, ledgers, expected, panels;
  try {
    [contracts, ledgers, expected, panels] = await Promise.all([
      RL.fetchJSON("contracts.json"), RL.fetchJSON("ledgers.json"),
      RL.fetchJSON("expected.json").catch(() => ({})), RL.fetchJSON("panels.json").catch(() => ({})),
    ]);
  } catch (e) {
    host.innerHTML = `<div class="card"><p class="mb0"><strong>Could not load the generated data</strong> (${RL.esc(e.message)}). Build it with <code>make site</code> and serve the <code>site/</code> directory over HTTP.</p></div>`;
    return;
  }
  const names = contracts.map((c) => c.name).filter((n) => ledgers[n]);
  const byName = Object.fromEntries(contracts.map((c) => [c.name, c]));

  // --- tabs --------------------------------------------------------------------
  const tabs = $("#tabs");
  tabs.innerHTML = names.map((n) => `<button data-name="${RL.esc(n)}">${RL.esc(n)}</button>`).join("");
  tabs.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-name]");
    if (b) { location.hash = b.dataset.name; }
  });

  function currentName() {
    const h = decodeURIComponent(location.hash.slice(1));
    const [name] = h.split("/");
    return names.includes(name) ? name : names[0];
  }

  // --- one contract's ledger -------------------------------------------------------
  const tamperState = {};

  function render() {
    const name = currentName();
    const c = byName[name], led = ledgers[name], panel = panels[name], exp = expected[name];
    tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.name === name));
    const cards = led.cards;
    const parts = [];
    parts.push(`<section class="section" style="padding-top:var(--s4)"><div class="col">
      <p class="eyebrow">${RL.esc(name)}<span class="n">contract sha256:${RL.esc(c.digest)}</span></p>
      <h2>${RL.esc(c.description || c.hazard)}</h2>
      <div class="cols">
        <div class="main"><pre class="code" style="margin-top:16px">${RL.esc(c.describe)}</pre></div>
        <aside class="aside"><strong>Ledger</strong><br><code>${RL.esc(led.path)}</code><br>${cards.length} card(s)${led.anchor ? `, anchored at <code>${RL.esc(String(led.anchor.head).slice(0, 12))}…</code>` : ", no anchor"}<br><br>${RL.runLink("ledger -c " + name, "Verify with the CLI in the sandbox")}</aside>
      </div>`);
    if (panel) {
      parts.push(`<h3>The panel</h3><p>${RL.esc(panel.summary)}<br><span class="small">${RL.fmt.int(panel.n_regions)} regions (${RL.esc(panel.first_region)} … ${RL.esc(panel.last_region)}) · data version sha256:${RL.esc(panel.data_version)}</span></p>
        <div class="cards cards--2"><div><h4>split coverage</h4><pre class="code">${RL.esc(panel.coverage)}</pre></div><div><h4>event coverage</h4><pre class="code">${RL.esc(panel.diagnostics.text)}</pre></div></div>`);
    }
    parts.push(`<h3>Experiments</h3>${RL.summaryTable(cards, { link: "ledgers.html" + "#" + encodeURIComponent(name) + "/" })}`);
    parts.push(`<div class="card mt16" id="chain">
      <h3 class="mt0">Hash chain</h3>
      <p class="small">Recorded at build time: <code>${RL.esc(led.status.text.split("\n")[0])}</code>. The button re-verifies the committed lines in this browser with Web Crypto: each card's <code>prev_hash</code> must equal the previous card's hash, each card's SHA-256 must match its stored <code>card_hash</code>, and the anchor must name the last card and the count.</p>
      <div class="btn-row"><button class="btn small" id="verify-btn">Verify in this browser</button>
        <select id="tamper-action" class="select" style="width:auto;max-width:100%">
          <option value="">— then try an attack —</option>
          <option value="edit">edit exp-0002's skill score in place</option>
          <option value="swap">swap exp-0002 and exp-0003</option>
          <option value="delete-middle">delete exp-0003 from the middle</option>
          <option value="truncate">delete the last card</option>
          <option value="no-anchor">delete the anchor file</option>
          <option value="forge">delete the last card and forge the anchor</option>
        </select>
        <button class="btn small ghost" id="tamper-reset">Restore</button></div>
      <div id="chain-result"></div>
    </div>`);
    parts.push(cards.map((k) => cardHTML(k, c)).join(""));
    if (exp) parts.push(fingerprintsHTML(exp, cards, c));
    parts.push("</div></section>");
    host.innerHTML = parts.join("");

    tamperState.lines = cards.map((k) => k.raw);
    tamperState.anchor = led.anchor ? { ...led.anchor } : null;
    tamperState.note = "";
    $("#verify-btn").addEventListener("click", verifyNow);
    $("#tamper-action").addEventListener("change", (e) => { applyTamper(e.target.value); e.target.value = ""; });
    $("#tamper-reset").addEventListener("click", () => {
      tamperState.lines = cards.map((k) => k.raw);
      tamperState.anchor = led.anchor ? { ...led.anchor } : null;
      tamperState.note = "restored the committed ledger and anchor";
      verifyNow();
    });

    const target = decodeURIComponent(location.hash.slice(1)).split("/")[1];
    if (target) { const el = document.getElementById("card-" + target); if (el) el.scrollIntoView({ block: "start" }); }
    renderCompare();
  }

  async function verifyNow() {
    const out = $("#chain-result");
    out.innerHTML = `<p class="small">hashing…</p>`;
    const st = await RL.verifyChain(tamperState.lines, tamperState.anchor);
    const cls = st.valid ? "ok" : "fail";
    const lines = [];
    if (tamperState.note) lines.push(`<span class="dim"># ${RL.esc(tamperState.note)}</span>`);
    lines.push(`<span class="${cls}">${RL.esc(RL.chainText(st))}</span>`);
    if (st.valid && tamperState.forged) {
      lines.push(`<span class="warn">WARNING: the chain reads intact because the anchor was rewritten to match. The anchor is not a proof; it turns a silent truncation into two coordinated edits that show up in git history.</span>`);
    }
    out.innerHTML = RL.terminal("ledger chain, verified here", lines.join("\n"), { raw: true });
  }

  function applyTamper(action) {
    // Each attack starts from the committed ledger, so the message describes
    // exactly one edit; Restore puts the original back after the last one.
    tamperState.lines = ledgers[currentName()].cards.map((k) => k.raw);
    tamperState.anchor = ledgers[currentName()].anchor ? { ...ledgers[currentName()].anchor } : null;
    const L = tamperState.lines;
    tamperState.forged = false;
    switch (action) {
      case "edit":
        if (L.length > 1) {
          L[1] = L[1].replace(/"brier_skill_score":(-?[0-9.eE+-]+)/, (m, v) => `"brier_skill_score":${(parseFloat(v) + 0.25).toFixed(4)}`);
          tamperState.note = "rewrote scorecard.brier_skill_score on exp-0002 (+0.25) without re-sealing the card";
        }
        break;
      case "swap":
        if (L.length > 2) { [L[1], L[2]] = [L[2], L[1]]; tamperState.note = "swapped the lines for exp-0002 and exp-0003"; }
        break;
      case "delete-middle":
        if (L.length > 2) { L.splice(2, 1); tamperState.note = "deleted the line for exp-0003"; }
        break;
      case "truncate":
        if (L.length > 0) { L.pop(); tamperState.note = "deleted the last line (a perfectly self-consistent prefix remains)"; }
        break;
      case "no-anchor":
        tamperState.anchor = null; tamperState.note = "deleted ledger.jsonl.anchor.json";
        break;
      case "forge":
        if (L.length > 0) {
          L.pop();
          const last = L.length ? JSON.parse(L[L.length - 1]).card_hash : RL.GENESIS;
          tamperState.anchor = { n_cards: L.length, head: last };
          tamperState.forged = true;
          tamperState.note = "deleted the last line AND rewrote the anchor to match";
        }
        break;
      default: return;
    }
    verifyNow();
  }

  function cardHTML(k, c) {
    const sc = k.scorecard || {};
    return `<section class="card wide mt24" id="card-${RL.esc(k.experiment_id)}">
      <header class="card__head">
        <h3 class="mt0" style="margin-bottom:0">${RL.esc(k.experiment_id)} · <code>${RL.esc(k.model)}@${RL.esc(k.version)}</code></h3>
        <div>${RL.verdictTag(k)} <span class="small">${RL.esc(k.split)} · ${RL.esc(k.timestamp)}</span></div>
      </header>
      <div class="card__split">
        <div>
          ${RL.reliabilitySVG(sc.reliability_bins, c.thresholds.reliability_tolerance_pp, 300)}
          <dl class="kv">
            <dt>Brier</dt><dd>${RL.fmt.f6(sc.brier_score)} (reference ${RL.fmt.f6(sc.brier_score_reference)})</dd>
            <dt>BSS</dt><dd>${RL.fmt.signed4(sc.brier_skill_score)}</dd>
            <dt>AUC</dt><dd>${RL.fmt.f4(sc.auc)}</dd>
            <dt>sharpness</dt><dd>${RL.fmt.f4(sc.sharpness)}</dd>
            <dt>Murphy</dt><dd>rel ${RL.fmt.f6(sc.reliability)} · res ${RL.fmt.f6(sc.resolution)} · unc ${RL.fmt.f6(sc.uncertainty)}</dd>
            <dt>units</dt><dd>${RL.fmt.int(sc.n_units)} (${RL.fmt.int(sc.n_positive)} positive, base rate ${RL.fmt.f4(sc.base_rate)})</dd>
            <dt>panel</dt><dd><code>${RL.esc(sc.panel_digest)}</code> · train <code>${RL.esc(sc.train_digest)}</code></dd>
            <dt>data</dt><dd><code>${RL.esc((k.data_snapshot || {}).data_version)}</code></dd>
            <dt>hashes</dt><dd><code title="${RL.esc(k.prev_hash)}">prev ${RL.esc(k.prev_hash.slice(0, 12))}…</code> → <code title="${RL.esc(k.card_hash)}">${RL.esc(k.card_hash.slice(0, 12))}…</code></dd>
          </dl>
        </div>
        <div>
          <p><strong>Changed.</strong> ${RL.esc(k.changed)}</p>
          <p><strong>Hypothesis.</strong> ${RL.esc(k.hypothesis)}</p>
          <p><strong>Outcome.</strong> ${RL.esc(k.outcome)}</p>
          <h4>Contract</h4>${RL.checksList((k.verdict || {}).checks)}
          <h4>Leakage canary</h4>${RL.findingsList((k.canary || {}).findings)}
          <h4>Reliability bins</h4>${RL.binsTable(sc.reliability_bins)}
        </div>
      </div>
    </section>`;
  }

  function fingerprintsHTML(exp, cards, c) {
    const models = Object.keys(exp).filter((k) => !k.startsWith("_"));
    const rows = [];
    for (const m of models) {
      const card = cards.find((k) => k.model === m);
      for (const [field, value] of Object.entries(exp[m])) {
        const got = card ? card.scorecard[field] : undefined;
        const comparable = field !== "reliability_bins_sha256";
        const same = comparable ? JSON.stringify(got) === JSON.stringify(value) : null;
        rows.push(`<tr class="diffrow ${same === false ? "bad" : "good"}"><td><code>${RL.esc(m)}</code></td><td><code>${RL.esc(field)}</code></td><td class="num">${RL.esc(JSON.stringify(value))}</td><td class="num">${comparable ? RL.esc(JSON.stringify(got)) : '<span class="muted">hash of the bins</span>'}</td><td class="mark">${same === null ? "" : same ? "=" : "≠"}</td></tr>`);
      }
    }
    return `<section class="card wide mt24"><h3 class="mt0">Blessed fingerprints · <code>harness_expected/${RL.esc(c.name)}.json</code></h3>
      <p class="small">What <code>readiness verify</code> compares against: every scorecard number the two climatology baselines produce on the validate split, blessed once from a clean clone. Beside each, the value the committed ledger card carries. Data version <code>${RL.esc(exp._data_version)}</code>, contract <code>${RL.esc(exp._contract)}</code>. ${RL.runLink("verify -c " + c.name + " --quiet", "Recompute in the sandbox")}</p>
      <div class="table-wrap"><table class="tabular"><thead><tr><th>model</th><th>field</th><th class="num">blessed</th><th class="num">on the card</th><th></th></tr></thead><tbody>${rows.join("")}</tbody></table></div></section>`;
  }

  // --- compare across contracts ------------------------------------------------------
  const cmp = $("#cmp-model");
  const modelNames = [...new Set(names.flatMap((n) => ledgers[n].cards.map((k) => k.model)))];
  cmp.innerHTML = modelNames.map((m) => `<option value="${RL.esc(m)}"${m === "climatology-seasonal" ? " selected" : ""}>${RL.esc(m)}</option>`).join("");
  cmp.addEventListener("change", renderCompare);
  function renderCompare() {
    const m = cmp.value;
    $("#compare").innerHTML = names.map((n) => {
      const k = ledgers[n].cards.find((x) => x.model === m);
      const c = byName[n];
      if (!k) return `<div class="card"><p class="mb0 muted">${RL.esc(n)}: no card for <code>${RL.esc(m)}</code></p></div>`;
      return `<figure class="card">${RL.reliabilitySVG(k.scorecard.reliability_bins, c.thresholds.reliability_tolerance_pp, 260)}<figcaption><a href="#${encodeURIComponent(n)}/${RL.esc(k.experiment_id)}">${RL.esc(n)}</a> ${RL.verdictTag(k)}<br>${RL.esc(c.hazard)} · ${RL.esc(c.period)}ly · base rate ${RL.fmt.pct(k.scorecard.base_rate, 2)}<br>BSS ${RL.fmt.signed4(k.scorecard.brier_skill_score)} · AUC ${RL.fmt.f4(k.scorecard.auc)}</figcaption></figure>`;
    }).join("");
  }

  window.addEventListener("hashchange", render);
  render();
})();
