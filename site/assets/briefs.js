/* The briefs page: validated county briefs, the fleet status table and the
   pinned exposure extracts, all read from generated/ and never recomputed. */
(async function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const host = $("#briefs");
  let briefs, fleet, exposure;
  try {
    [briefs, fleet, exposure] = await Promise.all([
      RL.fetchJSON("briefs.json"), RL.fetchJSON("fleet.json"),
      RL.fetchJSON("exposure.json").catch(() => []),
    ]);
  } catch (e) {
    host.innerHTML = `<div class="card"><p class="mb0"><strong>Could not load the generated data</strong> (${RL.esc(e.message)}). Build it with <code>make site</code> and serve the <code>site/</code> directory over HTTP.</p></div>`;
    return;
  }

  // --- the briefs -----------------------------------------------------------------
  function briefRow(b) {
    const contracts = (b.contracts || []).map((c) => `<code>${RL.esc(c)}</code>`).join(", ") || "–";
    const when = b.generated_at ? String(b.generated_at).slice(0, 10) : "–";
    return `<tr><td><code>${RL.esc(b.fips)}</code></td><td>${RL.esc(b.title || "")}</td><td><code>${RL.esc(b.period)}</code></td><td>${contracts}</td><td>${RL.esc(when)}</td><td><a href="${RL.GEN}${RL.esc(b.html)}">brief</a></td></tr>`;
  }

  function briefsHTML() {
    if (!briefs.length) {
      return `<div class="notice"><p><strong>No brief ships in the repository.</strong> A brief is produced by <code>readiness brief --county FIPS --period YYYY-Qn</code> (or <code>--state XX</code>) from the <code>issued/</code> files that cover the county, and an issued file needs a contract with a passing test card, which only the real-data run writes. When one is committed, this page lists it — and only if <code>readiness.cite.validate</code> still finds no violation at build time.</p><p class="mb0">What one will say, sentence by sentence, is fixed in <a href="https://github.com/jacquelineadean/disaster-risk-forecasting-readiness-loop/blob/main/docs/brief.md">docs/brief.md</a>.</p></div>`;
    }
    return `<div class="table-wrap"><table><thead><tr><th>county</th><th>title</th><th>period</th><th>contracts cited</th><th>generated</th><th></th></tr></thead><tbody>${briefs.map(briefRow).join("")}</tbody></table></div>`;
  }

  host.innerHTML = `<section class="section" style="padding-top:var(--s4)"><div class="col">
    <p class="eyebrow">Validated briefs<span class="n">${briefs.length} listed</span></p>
    ${briefsHTML()}
  </div></section>`;

  // --- the fleet ------------------------------------------------------------------
  function pill(ok, yes, no) {
    return `<span class="pill ${ok ? "pass" : "neutral"}">${ok ? yes : no}</span>`;
  }

  function fleetRow(r) {
    const passes = (r.validate_passes || []).map((m) => `<code>${RL.esc(m)}</code>`).join(", ") || "–";
    const test = r.test_card ? `<code>${RL.esc(r.test_card)}</code> ${RL.esc(r.promoted_model || "")}${r.test_bss == null ? "" : " · BSS " + RL.fmt.signed4(r.test_bss)}` : "–";
    const phase1 = r.phase1_ok
      ? pill(true, "ok", "")
      : `${pill(false, "", "not met")} <span class="small">${RL.esc(r.phase1_detail || "")}</span>`;
    return `<tr><td><a href="ledgers.html#${encodeURIComponent(r.name)}"><code>${RL.esc(r.name)}</code></a>${r.national ? ' <span class="pill neutral">national</span>' : ""}</td><td>${RL.esc(r.hazard)}</td><td>${RL.esc(r.scope_key)}</td><td>${RL.esc(r.period)}</td><td class="num">${RL.fmt.int(r.n_cards)}</td><td>${passes}</td><td>${test}</td><td>${phase1}</td></tr>`;
  }

  $("#fleet-table").innerHTML = fleet.length
    ? `<table><thead><tr><th>contract</th><th>hazard</th><th>scope</th><th>period</th><th class="num">cards</th><th>validate passes</th><th>test card</th><th>phase 1</th></tr></thead><tbody>${fleet.map(fleetRow).join("")}</tbody></table>`
    : `<p class="muted">No contracts registered.</p>`;

  // --- the exposure pins -----------------------------------------------------------
  function exposureRow(s) {
    const label = s.state ? `${RL.esc(s.state)} <span class="small">(${RL.esc(s.state_fips)})</span>` : `<code>${RL.esc(s.state_fips)}</code>`;
    return `<tr><td>${label}</td><td class="num">${RL.fmt.int(s.n_counties)}</td><td class="num">${RL.fmt.int(s.total_structures)}</td><td class="num">${RL.esc(s.vintage)}</td><td><code>${RL.esc(s.manifest_key)}</code></td></tr>`;
  }

  $("#exposure-table").innerHTML = exposure.length
    ? `<table><thead><tr><th>state</th><th class="num">counties</th><th class="num">structures</th><th class="num">vintage</th><th>manifest key</th></tr></thead><tbody>${exposure.map(exposureRow).join("")}</tbody></table>`
    : `<p class="muted">No USA Structures extract is pinned in this build. <code>readiness exposure snapshot --states OK,LA,TX</code> (or <code>--all-states</code>) pulls the county counts per state and records each under <code>fema/usa_structures/&lt;fips&gt;</code> in <code>snapshots/manifest.json</code>; the counts themselves are not committed.</p>`;
})();
