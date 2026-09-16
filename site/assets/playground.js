/* The sandbox page: drives the Pyodide worker, the terminal and the guided demos. */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  // --- the terminal ------------------------------------------------------------------
  class Term {
    constructor(el) { this.el = el; this.pre = el.querySelector("pre"); }
    append(html) {
      const nearBottom = this.pre.scrollHeight - this.pre.scrollTop - this.pre.clientHeight < 60;
      this.pre.insertAdjacentHTML("beforeend", html + "\n");
      if (nearBottom) this.pre.scrollTop = this.pre.scrollHeight;
    }
    line(text, cls) { this.append(cls ? `<span class="${cls}">${RL.esc(text)}</span>` : RL.colourLine(text)); }
    prompt(argv) { this.append(RL.colourLine("$ readiness " + argv)); }
    clear() { this.pre.innerHTML = ""; }
  }

  // --- the worker --------------------------------------------------------------------
  class Sandbox {
    constructor() {
      this.worker = null; this.pending = new Map(); this.seq = 0; this.ready = false; this.info = null;
      this.onStatus = () => {}; this.onOut = () => {}; this.queue = Promise.resolve();
    }
    start() {
      return new Promise((resolve, reject) => {
        this.worker = new Worker("assets/sandbox-worker.js");
        this.worker.onmessage = (e) => this._on(e.data, resolve, reject);
        this.worker.onerror = (e) => { this.onStatus("error", e.message || "worker failed"); reject(new Error(e.message || "worker failed")); };
        // Relative URLs inside a worker resolve against the worker script, not
        // the page: resolve them here.
        this.worker.postMessage({
          type: "init",
          zipURL: new URL(RL.GEN + "sandbox.zip", location.href).href,
          pyURL: new URL("assets/sandbox.py", location.href).href,
        });
      });
    }
    _on(m, resolve, reject) {
      if (m.type === "status") this.onStatus(m.stage, m.detail);
      else if (m.type === "ready") { this.ready = true; this.info = m.info; this.pyodide = m.pyodide; resolve(m); }
      else if (m.type === "out") { const p = this.pending.get(m.id); if (p && p.onOut) p.onOut(m.text, m.stream); else this.onOut(m.text, m.stream); }
      else if (m.type === "done" || m.type === "result") { const p = this.pending.get(m.id); this.pending.delete(m.id); if (p) p.resolve(m.type === "done" ? m.code : m.value); }
      else if (m.type === "error") {
        const p = this.pending.get(m.id); this.pending.delete(m.id);
        if (p) p.reject(new Error(m.message)); else { this.onStatus("error", m.message); reject(new Error(m.message)); }
      }
    }
    _send(msg, onOut) {
      const id = ++this.seq;
      const run = () => new Promise((resolve, reject) => { this.pending.set(id, { resolve, reject, onOut }); this.worker.postMessage({ ...msg, id }); });
      const p = this.queue.then(run, run);
      this.queue = p.catch(() => {});
      return p;
    }
    cli(argv, onOut) { return this._send({ type: "cli", argv }, onOut); }
    call(fn, args, onOut) { return this._send({ type: "call", fn, args }, onOut).then((v) => { if (v && v.error) throw new Error(v.error); return v; }); }
    terminate() {
      if (this.worker) this.worker.terminate();
      this.worker = null; this.ready = false; this.info = null;
      for (const p of this.pending.values()) p.reject(new Error("sandbox was reset"));
      this.pending.clear(); this.queue = Promise.resolve();
    }
  }

  // shell-like tokenizer for the command line: quotes group, backslash escapes
  function tokenize(s) {
    const out = []; let cur = "", q = null, has = false;
    for (let i = 0; i < s.length; i++) {
      const ch = s[i];
      if (q) { if (ch === q) q = null; else if (ch === "\\" && q === '"' && i + 1 < s.length) cur += s[++i]; else cur += ch; }
      else if (ch === '"' || ch === "'") { q = ch; has = true; }
      else if (ch === "\\" && i + 1 < s.length) { cur += s[++i]; has = true; }
      else if (/\s/.test(ch)) { if (cur || has) { out.push(cur); cur = ""; has = false; } }
      else { cur += ch; has = true; }
    }
    if (cur || has) out.push(cur);
    return out;
  }
  const quote = (s) => (/^[A-Za-z0-9_.\-+:/@=,]+$/.test(s) ? s : `"${s.replace(/(["\\])/g, "\\$1")}"`);

  // --- page state --------------------------------------------------------------------
  const term = new Term($("#term"));
  const sb = new Sandbox();
  let coverage = null, hazards = {}, contracts = [], models = null, features = [];
  let queuedCmd = null, busy = false, bootedAt = 0;

  function setStatus(kind, text, sub) {
    const dot = $("#status-dot");
    dot.className = "dot " + (kind === "ready" ? "ready" : kind === "err" ? "err" : "busy");
    $("#status-text").textContent = text;
    if (sub !== undefined) $("#status-sub").innerHTML = sub;
    const bar = $("#progress i");
    bar.className = kind === "busy" ? "indet" : "";
    bar.style.width = kind === "ready" ? "100%" : "";
    $("#progress").hidden = kind !== "busy";
  }
  function setBusy(v) {
    busy = v;
    $$("[data-needs-sandbox]").forEach((el) => { el.disabled = v || !sb.ready; });
    $("#term-busy").hidden = !v;
  }

  async function runCLI(argvString, { echo = true } = {}) {
    if (!sb.ready) { queuedCmd = argvString; term.line(`# queued until the sandbox is ready: readiness ${argvString}`, "dim"); return null; }
    const argv = tokenize(argvString);
    setBusy(true);
    if (echo) term.prompt(argvString);
    const t0 = performance.now();
    let code = null;
    try {
      code = await sb.cli(argv, (text, stream) => term.line(text, stream === "err" ? "err" : null));
      term.line(`[exit ${code} · ${((performance.now() - t0) / 1000).toFixed(1)} s]`, "dim");
    } catch (e) {
      term.line("sandbox error: " + e.message, "err");
    } finally {
      setBusy(false);
    }
    return code;
  }

  async function refreshInfo() {
    if (!sb.ready) return;
    sb.info = await sb.call("sandbox_info", {});
    populateSelects();
  }

  // --- coverage table ------------------------------------------------------------------
  function renderCoverage() {
    const host = $("#coverage");
    if (!coverage) { host.innerHTML = `<p class="small">No <code>sandbox.json</code>; build the site to see what the archive carries.</p>`; return; }
    const info = sb.info || { contracts: {} };
    const rows = Object.entries(coverage.contracts).map(([name, c]) => {
      const live = info.contracts[name] || {};
      const cov = c.coverage === "missing" ? '<span class="pill fail" title="no pinned extract on board">no data</span>' : c.coverage === "full" ? '<span class="pill ok" title="every event type of the state is on board">full</span>' : '<span class="pill warn" title="only the contract\'s event types are on board">hazard-only</span>';
      const repro = c.coverage === "missing" ? "–" : c.reproducible ? '<span class="pill ok" title="the packed data version matches the blessed fingerprints">reproducible</span>' : '<span class="pill warn" title="the packed data version differs from the blessed fingerprints">differs</span>';
      return `<tr><td><code>${RL.esc(name)}</code><br><span class="small">${RL.esc(Object.keys(c.states).join(", "))}</span></td><td>${cov}</td><td>${repro}</td><td>${live.has_ledger ? "yes" : "not yet"}</td></tr>`;
    }).join("");
    const states = Object.values(coverage.states).map((s) => `${s.state} (${s.event_types ? s.event_types.length + " event types, " : "every event type, "}${RL.fmt.int(s.rows)} rows)`).join(" · ");
    host.innerHTML = `<div class="table-wrap"><table class="compact"><thead><tr><th>contract</th><th>data</th><th>fingerprints</th><th>ledger</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="small mt8">Packed extracts: ${RL.esc(states)}. Snapshot pinned ${RL.esc((coverage.manifest_generated_at || "").slice(0, 10))}; archive ${(coverage.zip_bytes / 1e6).toFixed(2)} MB. <em>Full</em> means every event type of the state is on board, so any catalogue hazard can be registered against it and run here; <em>hazard-only</em> means the contract's own event types. <em>Reproducible</em> means the packed data version matches the blessed fingerprints.</p>`;
  }

  // --- selects -----------------------------------------------------------------------
  function contractOptions(filter) {
    const info = sb.info ? sb.info.contracts : {};
    const names = Object.keys(info).length ? Object.keys(info) : contracts.map((c) => c.name);
    return names.filter((n) => !filter || filter(info[n] || {}, n)).map((n) => `<option value="${RL.esc(n)}">${RL.esc(n)}</option>`).join("");
  }
  function fill(sel, html, keep) {
    const prev = keep ? sel.value : null;
    sel.innerHTML = html;
    if (prev && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
  }
  function populateSelects() {
    fill($("#walk-contract"), contractOptions(), true);
    fill($("#cal-contract"), contractOptions((c) => c.has_data), true);
    fill($("#tam-contract"), contractOptions(), true);
    fill($("#ver-contract"), contractOptions((c) => c.has_data), true);
    fill($("#dash-contract"), contractOptions(), true);
    if (!populateSelects.done) {
      // first fill: start every demo on the walkthrough's contract
      populateSelects.done = true;
      for (const id of ["#walk-contract", "#cal-contract", "#tam-contract", "#ver-contract", "#dash-contract"]) {
        const sel = $(id);
        if ([...sel.options].some((o) => o.value === "tornado-ok")) sel.value = "tornado-ok";
      }
    }
    walkNote(); tamperState(); renderCoverage();
    if (sb.info) $("#tree-label").textContent = sb.info.experiments_dir;
  }

  // --- demo 1: the walkthrough commands ------------------------------------------------
  function walkNote() {
    const name = $("#walk-contract").value;
    const c = sb.info && sb.info.contracts[name];
    const note = $("#walk-note");
    if (!c) { note.textContent = ""; return; }
    note.innerHTML = c.has_data ? `<span class="pill ok">pinned data on board</span> the panel for <code>${RL.esc(name)}</code> builds here.` : `<span class="pill warn">no pinned data</span> ${RL.esc(c.data_gap || "")} — the commands that need a panel will say so.`;
  }
  $("#walk-contract").addEventListener("change", walkNote);
  $$("[data-cmd]").forEach((b) => b.addEventListener("click", () => {
    const cmd = b.dataset.cmd.replace(/\{c\}/g, $("#walk-contract").value);
    runCLI(cmd);
  }));

  // --- demo 2: register a contract -----------------------------------------------------
  // The form's defaults are the package's (readiness.contracts.DEFAULTS), read from
  // generated/contract_defaults.json; the values written in playground.html only
  // hold the form until that file has loaded. A field left at its default is not
  // put on the command line, because `readiness register` fills it in itself.
  const REG_FIELDS = {
    period: ["#reg-period", "--period"], property_usd_min: ["#reg-usd", "--damage-usd"],
    zone_policy: ["#reg-zone", "--zone-policy"], train: ["#reg-train", "--train"],
    validate: ["#reg-validate", "--validate"], test: ["#reg-test", "--test"],
  };
  const REG_THRESHOLDS = {
    min_brier_skill_score: "#reg-bss", reliability_tolerance_pp: "#reg-tol", reliability_min_bin_count: "#reg-minbin",
    min_auc: "#reg-auc", n_reliability_bins: "#reg-bins",
  };
  function formDefaults() {
    const d = {};
    for (const [k, [id]] of Object.entries(REG_FIELDS)) d[k] = $(id).value;
    for (const [k, id] of Object.entries(REG_THRESHOLDS)) d[k] = $(id).placeholder;
    d.count_casualties = !$("#reg-nocas").checked;
    return d;
  }
  let defaults = formDefaults();
  function applyDefaults(d) {
    defaults = d;
    for (const [k, [id]] of Object.entries(REG_FIELDS)) $(id).value = String(d[k]);
    for (const [k, id] of Object.entries(REG_THRESHOLDS)) $(id).placeholder = String(d[k]);
    $("#reg-nocas").checked = !d.count_casualties;
  }
  const sameValue = (a, b) => a === String(b) || (a !== "" && !isNaN(a) && !isNaN(b) && Number(a) === Number(b));
  function registerArgv() {
    const v = (id) => $(id).value.trim();
    const parts = ["register", v("#reg-name") || "my-contract", "--hazard", v("#reg-hazard")];
    for (const st of v("#reg-states").toUpperCase().split(/[\s,]+/).filter(Boolean)) parts.push("--state", st);
    const opt = (k) => { const [id, flag] = REG_FIELDS[k]; if (v(id) !== "" && !sameValue(v(id), defaults[k])) parts.push(flag, v(id)); };
    opt("period"); opt("property_usd_min");
    if ($("#reg-nocas").checked) parts.push("--no-casualties");
    opt("zone_policy"); opt("train"); opt("validate"); opt("test");
    if (v("#reg-bss")) parts.push("--min-bss", v("#reg-bss"));
    if (v("#reg-tol")) parts.push("--tolerance", v("#reg-tol"));
    if (v("#reg-minbin")) parts.push("--min-bin-count", v("#reg-minbin"));
    if (v("#reg-auc")) parts.push("--min-auc", v("#reg-auc"));
    if (v("#reg-bins")) parts.push("--bins", v("#reg-bins"));
    if (v("#reg-desc")) parts.push("--description", v("#reg-desc"));
    if ($("#reg-force").checked) parts.push("--force");
    if ($("#reg-dry").checked) parts.push("--dry-run");
    return parts;
  }
  function registerPreview() {
    const argv = registerArgv();
    $("#reg-preview").textContent = "readiness " + argv.map(quote).join(" ");
    const hz = hazards[$("#reg-hazard").value];
    const states = $("#reg-states").value.toUpperCase().split(/[\s,]+/).filter(Boolean);
    const hint = $("#reg-hint");
    if (hz && hz.coding !== "county" && $("#reg-zone").value === "drop") {
      hint.innerHTML = `<span class="pill warn">${RL.esc(hz.coding)}-coded</span> Storm Events codes this hazard against forecast zones; with <code>drop</code> the panel will under-count it. Consider <code>expand</code>.`;
    } else if (coverage && states.length) {
      const packed = Object.values(coverage.states);
      const gaps = states.filter((s) => { const p = packed.find((x) => x.state === s); return !p || (p.event_types && hz && hz.event_types.some((t) => !p.event_types.includes(t))); });
      hint.innerHTML = gaps.length ? `<span class="pill warn">no rows on board</span> the sandbox has no ${RL.esc(hz ? hz.event_types.join("/") : "matching")} extract for ${RL.esc(gaps.join(", "))}: the contract registers, but its panel cannot be built here.` : `<span class="pill ok">pinned data on board</span> after registering you can build the panel and run the loop here.`;
    } else hint.textContent = "";
  }
  $$("#register-form input, #register-form select").forEach((el) => el.addEventListener("input", registerPreview));
  $("#register-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const argv = registerArgv();
    const name = argv[1];
    const code = await runCLI(argv.map(quote).join(" "));
    if (code === 0 && !$("#reg-dry").checked) {
      await refreshInfo();
      const c = sb.info.contracts[name];
      $("#reg-follow").innerHTML = c ? `<p class="small mt8">Registered <code>${RL.esc(name)}</code> (sha256:${RL.esc(c.digest)}) in the sandbox's registry.</p><div class="btn-row">
        <button class="btn small secondary" data-needs-sandbox data-follow="contract -c ${RL.esc(name)}">contract</button>
        <button class="btn small secondary" data-needs-sandbox data-follow="panel -c ${RL.esc(name)} --quiet" ${c.has_data ? "" : "disabled"}>panel</button>
        <button class="btn small" data-needs-sandbox data-follow="loop -c ${RL.esc(name)} --quiet" ${c.has_data ? "" : "disabled"}>loop</button>
        <button class="btn small secondary" data-needs-sandbox data-follow="verify -c ${RL.esc(name)} --quiet" ${c.has_data ? "" : "disabled"}>verify</button></div>${c.has_data ? "" : `<p class="small">${RL.esc(c.data_gap || "")}</p>`}` : "";
      $$("[data-follow]").forEach((b) => b.addEventListener("click", () => runCLI(b.dataset.follow)));
    }
  });
  $("#reg-presets").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-preset]"); if (!b) return;
    const p = JSON.parse(b.dataset.preset);
    for (const [k, v] of Object.entries(p)) { const el = $("#reg-" + k); if (!el) continue; if (el.type === "checkbox") el.checked = v; else el.value = v; }
    registerPreview();
  });

  // --- demo 3: the calibration playground -----------------------------------------------
  // Which parameters a model takes, their defaults and what they mean come from
  // generated/models.json (each registry entry's `params`, the package's
  // ModelSpec.params). Only the input ranges, which are presentation, live here.
  const RANGES = { shrinkage: [0, 200, 1], hit: [0.01, 0.99, 0.01], miss: [0.001, 0.5, 0.001] };
  function modelEntry(name) { return models && models.registry.find((x) => x.name === name); }
  function modelParams(name) {
    const m = modelEntry(name);
    return (m && m.params) || [];
  }
  // One input per parameter, by the schema's JSON type: a `list` (the feature
  // sets) is a comma-separated text field whose default is the registry's, a
  // `bool` a checkbox, a `str` a text field, `float`/`int` a number field.
  function paramInput(p) {
    const id = `cal-p-${p.name}`, attrs = `id="${id}" data-param="${p.name}" data-type="${p.type}"`;
    if (p.type === "list") {
      const names = features.map((f) => f.name);
      return `<div class="field"><label for="${id}">${RL.esc(p.help)}</label><input type="text" ${attrs} value="${RL.esc((p.default || []).join(","))}" list="cal-feature-sets" placeholder="comma-separated set names, empty for none"><datalist id="cal-feature-sets">${names.map((n) => `<option value="${RL.esc(n)}">`).join("")}</datalist></div>`;
    }
    if (p.type === "bool") return `<div class="field"><label></label><span class="check"><input type="checkbox" ${attrs}${p.default ? " checked" : ""}><label for="${id}" style="color:inherit">${RL.esc(p.help)}</label></span></div>`;
    if (p.type === "str") return `<div class="field"><label for="${id}">${RL.esc(p.help)}</label><input type="text" ${attrs} value="${RL.esc(String(p.default))}"></div>`;
    const [lo, hi, step] = RANGES[p.name] || [undefined, undefined, p.type === "int" ? 1 : "any"];
    const bounds = lo === undefined ? "" : ` min="${lo}" max="${hi}"`;
    return `<div class="field"><label for="${id}">${RL.esc(p.help)}</label><input type="number" ${attrs} value="${p.default}"${bounds} step="${step}"></div>`;
  }
  function paramValue(el) {
    const t = el.dataset.type;
    if (t === "list") return el.value.split(",").map((x) => x.trim()).filter(Boolean);
    if (t === "bool") return el.checked;
    if (t === "str") return el.value;
    return Number(el.value);
  }
  function renderParams() {
    const m = $("#cal-model").value, entry = modelEntry(m);
    const note = entry && entry.needs_features
      ? `<p class="small"><span class="pill warn">needs features</span> the sandbox packs no feature sources (ERA5, terrain, NRI, CLIMADA), so a non-empty feature-set list is refused here. Clear the field to run the history-only variant; on a machine with the pinned data, <code>readiness score ${RL.esc(m)} --features era5,terrain</code> runs the full model.</p>`
      : "";
    $("#cal-params").innerHTML = modelParams(m).map(paramInput).join("") + note;
    $$("#cal-params input").forEach((el) => el.addEventListener("change", () => calibrate.scheduled && calibrate()));
  }
  $("#cal-model").addEventListener("change", () => { renderParams(); if (calibrate.scheduled) calibrate(); });
  const showVal = () => { $("#cal-scale-val").textContent = Number($("#cal-scale").value).toFixed(2); $("#cal-shift-val").textContent = Number($("#cal-shift").value).toFixed(2); };
  let calTimer = null;
  ["#cal-scale", "#cal-shift"].forEach((id) => $(id).addEventListener("input", () => { showVal(); if (calibrate.scheduled) { clearTimeout(calTimer); calTimer = setTimeout(calibrate, 250); } }));
  $("#cal-reset").addEventListener("click", () => { $("#cal-scale").value = 1; $("#cal-shift").value = 0; showVal(); if (calibrate.scheduled) calibrate(); });
  $("#cal-run").addEventListener("click", () => { calibrate.scheduled = true; calibrate(); });
  async function calibrate() {
    if (!sb.ready || busy) return;
    const params = {}; $$("#cal-params input").forEach((el) => { params[el.dataset.param] = paramValue(el); });
    const args = { contract: $("#cal-contract").value, model: $("#cal-model").value, params, scale: Number($("#cal-scale").value), shift: Number($("#cal-shift").value) };
    const out = $("#cal-result");
    out.innerHTML = `<p class="small">fitting and scoring with the harness…</p>`;
    setBusy(true);
    try {
      const r = await sb.call("score_playground", args);
      const sc = r.scorecard;
      const v = r.verdict, k = r.canary;
      const tag = k.rejected ? '<span class="pill reject">REJECTED</span>' : v.passed ? '<span class="pill pass">PASS</span>' : '<span class="pill fail">FAIL</span>';
      out.innerHTML = `<div><p class="mt0"><strong>${RL.esc(r.model)}</strong> on <code>${RL.esc(r.contract)}</code> · ${tag}</p>
        ${RL.reliabilitySVG(sc.reliability_bins, r.tolerance, 300, { ghost: r.base_scorecard.reliability_bins })}
        <p class="small">Filled: populated bins; hollow: thin. Dotted grey: the untransformed model's bins.</p>
        <div>
        <dl class="kv"><dt>BSS</dt><dd>${RL.fmt.signed4(sc.brier_skill_score)} <span class="muted">(was ${RL.fmt.signed4(r.base_scorecard.brier_skill_score)})</span></dd><dt>AUC</dt><dd>${RL.fmt.f4(sc.auc)}</dd><dt>Brier</dt><dd>${RL.fmt.f6(sc.brier_score)} (reference ${RL.fmt.f6(sc.brier_score_reference)})</dd><dt>sharpness</dt><dd>${RL.fmt.f4(sc.sharpness)}</dd><dt>Murphy</dt><dd>rel ${RL.fmt.f6(sc.reliability)} · res ${RL.fmt.f6(sc.resolution)} · unc ${RL.fmt.f6(sc.uncertainty)}</dd></dl>
        <h4>Contract</h4>${RL.checksList(v.checks)}<h4>Leakage canary</h4>${RL.findingsList(k.findings)}</div></div>
        ${RL.terminal("harness output", r.text)}`;
    } catch (e) {
      out.innerHTML = `<p class="small"><span class="pill fail">error</span> ${RL.esc(e.message)}</p>`;
    } finally { setBusy(false); }
  }

  // --- demo 4: tamper with a ledger ------------------------------------------------------
  async function tamperState() {
    if (!sb.ready) return;
    const name = $("#tam-contract").value; if (!name) return;
    try {
      const st = await sb.call("ledger_state", { contract: name });
      $("#tam-state").innerHTML = st.exists
        ? `<code>${RL.esc(st.path)}</code> · ${st.n_cards} card(s) · <span class="pill ${st.valid ? "ok" : "fail"}">${RL.esc(st.status.split("\n")[0])}</span>${st.backup ? ' <span class="pill warn">tampered — restore to get the original back</span>' : ""}`
        : `<span class="pill neutral">no ledger yet in this tree</span> run the loop first: <button class="btn small" data-needs-sandbox id="tam-loop">loop -c ${RL.esc(name)} --quiet</button>`;
      const b = $("#tam-loop"); if (b) b.addEventListener("click", async () => { await runCLI(`loop -c ${name} --quiet`); await refreshInfo(); tamperState(); });
      $$("[data-tamper]").forEach((x) => { x.disabled = !st.exists; });
    } catch (e) { $("#tam-state").innerHTML = `<span class="pill fail">error</span> ${RL.esc(e.message)}`; }
  }
  $("#tam-contract").addEventListener("change", tamperState);
  $$("[data-tamper]").forEach((b) => b.addEventListener("click", async () => {
    const name = $("#tam-contract").value;
    setBusy(true);
    try {
      const r = await sb.call("tamper", { contract: name, action: b.dataset.tamper });
      term.line(`# ${r.description}`, "dim");
    } catch (e) { term.line("sandbox error: " + e.message, "err"); }
    finally { setBusy(false); }
    await runCLI(`ledger -c ${name}`);
    if (b.dataset.tamper === "forge") term.line("# the chain reads intact: the anchor is a commitment made visible in git history, not a proof. Forging it is two coordinated edits instead of one silent `head -n 1`.", "warn");
    tamperState();
  }));
  $("#tam-restore").addEventListener("click", async () => {
    const name = $("#tam-contract").value;
    setBusy(true);
    try { await sb.call("restore_ledger", { contract: name }); term.line("# restored the ledger and its anchor", "dim"); } catch (e) { term.line("sandbox error: " + e.message, "err"); } finally { setBusy(false); }
    await runCLI(`ledger -c ${name}`); tamperState();
  });

  // --- demo 5: bit-for-bit ------------------------------------------------------------------
  $("#ver-run").addEventListener("click", async () => {
    const name = $("#ver-contract").value; const out = $("#ver-result");
    out.innerHTML = `<p class="small">building the panel and scoring both baselines…</p>`;
    setBusy(true);
    try {
      const r = await sb.call("fingerprints", { contract: name });
      if (!r.has_expected) { out.innerHTML = `<p class="small"><span class="pill warn">nothing blessed</span> no <code>${RL.esc(r.expected_path)}</code>; <code>readiness verify --bless</code> would write one.</p>`; return; }
      const rows = r.rows.map((x) => `<tr class="diffrow ${x.match ? "good" : "bad"}"><td><code>${RL.esc(x.group)}</code></td><td><code>${RL.esc(x.field)}</code></td><td class="num">${RL.esc(JSON.stringify(x.expected))}</td><td class="num">${RL.esc(JSON.stringify(x.observed))}</td><td class="mark">${x.match ? "=" : "≠"}</td></tr>`).join("");
      const n = r.rows.length, bad = r.rows.filter((x) => !x.match).length;
      out.innerHTML = `<p class="small">${r.all_match ? `<span class="pill ok">bit-for-bit</span> all ${n} fields recomputed in this browser equal the fingerprints blessed from a clean clone (<code>${RL.esc(r.expected_path)}</code>).` : `<span class="pill fail">${bad} of ${n} differ</span> the reproducibility criterion fails for this contract in this sandbox.`}</p>
        <div class="table-wrap"><table class="tabular"><thead><tr><th>group</th><th>field</th><th class="num">blessed</th><th class="num">recomputed here</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
    } catch (e) { out.innerHTML = `<p class="small"><span class="pill fail">error</span> ${RL.esc(e.message)}</p>`; }
    finally { setBusy(false); }
  });
  $("#ver-cli").addEventListener("click", () => runCLI(`verify -c ${$("#ver-contract").value} --quiet`));

  // --- demo 6: the dashboard ------------------------------------------------------------------
  $("#dash-run").addEventListener("click", async () => {
    const name = $("#dash-contract").value;
    setBusy(true);
    try {
      const r = await sb.call("dashboard_html", { contract: name });
      $("#dash-frame").srcdoc = r.html; $("#dash-frame").hidden = false;
      term.line(`# rendered readiness.dashboard.render() for ${name} into the frame below the demos`, "dim");
    } catch (e) { term.line("sandbox error: " + e.message, "err"); } finally { setBusy(false); }
  });

  // --- terminal controls ----------------------------------------------------------------------
  $("#term-clear").addEventListener("click", () => term.clear());
  $("#cmd-run").addEventListener("click", () => { const v = $("#cmd-input").value.trim(); if (v) { history.push(v); hi = history.length; runCLI(v); $("#cmd-input").value = ""; } });
  const history = []; let hi = 0;
  $("#cmd-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("#cmd-run").click(); }
    else if (e.key === "ArrowUp") { if (hi > 0) { hi--; e.target.value = history[hi]; } e.preventDefault(); }
    else if (e.key === "ArrowDown") { if (hi < history.length - 1) { hi++; e.target.value = history[hi]; } else { hi = history.length; e.target.value = ""; } e.preventDefault(); }
  });
  $("#chips").addEventListener("click", (e) => { const c = e.target.closest(".chip"); if (c) { $("#cmd-input").value = c.dataset.chip; $("#cmd-input").focus(); } });
  $$("input[name=tree]").forEach((r) => r.addEventListener("change", async () => {
    if (!sb.ready) return;
    setBusy(true);
    try { sb.info = await sb.call("set_tree", { tree: r.value }); term.line(`# experiments tree: ${sb.info.experiments_dir}`, "dim"); populateSelects(); } finally { setBusy(false); }
  }));
  $("#term-reset").addEventListener("click", () => { sb.terminate(); term.clear(); boot(); });

  // --- boot --------------------------------------------------------------------------------------
  function openDemoFromHash() {
    const id = location.hash.slice(1);
    if (!id) return;
    const d = document.getElementById(id);
    if (d && d.tagName === "DETAILS") { d.open = true; d.scrollIntoView({ block: "start" }); }
  }
  async function boot() {
    bootedAt = performance.now();
    setStatus("busy", "starting the sandbox…", "");
    setBusy(false);
    sb.onStatus = (stage, detail) => setStatus(stage === "error" ? "err" : "busy", detail);
    sb.onOut = (text, stream) => term.line(text, stream === "err" ? "err" : null);
    try {
      let contractDefaults;
      [coverage, hazards, contracts, models, contractDefaults, features] = await Promise.all([
        RL.fetchJSON("sandbox.json").catch(() => null), RL.fetchJSON("hazards.json").catch(() => ({})),
        RL.fetchJSON("contracts.json").catch(() => []), RL.fetchJSON("models.json").catch(() => null),
        RL.fetchJSON("contract_defaults.json").catch(() => null), RL.fetchJSON("features.json").catch(() => []),
      ]);
      if (contractDefaults) applyDefaults(contractDefaults);
      fill($("#reg-hazard"), Object.keys(hazards).map((h) => `<option value="${RL.esc(h)}">${RL.esc(h)}</option>`).join(""));
      if (models) fill($("#cal-model"), models.registry.map((m) => `<option value="${RL.esc(m.name)}"${m.name === "climatology-seasonal" ? " selected" : ""}>${RL.esc(m.name)}</option>`).join(""));
      renderParams(); showVal();
      const firstPreset = $("#reg-presets button[data-preset]");
      if (firstPreset) firstPreset.click(); else registerPreview();
      renderCoverage();
      const ready = await sb.start();
      const secs = ((performance.now() - bootedAt) / 1000).toFixed(1);
      setStatus("ready", `ready in ${secs} s`, `Pyodide ${RL.esc(ready.pyodide)} · Python ${RL.esc(sb.info.python)} · readiness ${RL.esc(sb.info.package_version)} · experiments tree <code id="tree-label">${RL.esc(sb.info.experiments_dir)}</code>`);
      populateSelects();
      setBusy(false);
      term.line(`# sandbox ready: readiness ${sb.info.package_version} on Python ${sb.info.python} (Pyodide ${ready.pyodide}); the repository snapshot is unpacked at /repo`, "dim");
      const params = new URLSearchParams(location.search);
      const cmd = params.get("cmd") || queuedCmd;
      queuedCmd = null;
      if (cmd) { history.push(cmd); hi = history.length; await runCLI(cmd); }
      if (params.get("tree") === "committed") { $("#tree-committed").checked = true; $("#tree-committed").dispatchEvent(new Event("change")); }
    } catch (e) {
      setStatus("err", "the sandbox could not start: " + e.message, `Pyodide loads from <code>cdn.jsdelivr.net</code>; the archive comes from <code>generated/sandbox.zip</code> (build it with <code>make site</code>).`);
    }
  }
  openDemoFromHash();
  window.addEventListener("hashchange", openDemoFromHash);
  boot();
})();
