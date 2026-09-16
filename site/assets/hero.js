/* The index hero: a contract's labelled panel replayed on a canvas — one tile per
   region, one frame per period, lit when the region recorded a damaging event.
   Reads generated/tapes.json (tools/build_site.py); draws nothing without it. */
(function () {
  "use strict";
  const canvas = document.getElementById("hero-canvas");
  if (!canvas || !canvas.getContext) return;
  const hero = canvas.closest(".hero");
  const legend = document.getElementById("hero-legend");
  const pauseBtn = document.getElementById("hero-pause");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  const ctx = canvas.getContext("2d");
  const FRAME_MS = 210, DECAY = 0.76;
  const BASE = "#e6eaef", TINT = "#c3d1ea", LIT = "#2b5aa8";
  const SPLIT_INK = { train: "rgba(43,90,168,.30)", validate: "rgba(199,150,32,.38)", test: "rgba(176,58,46,.34)" };
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  let tape = null, bits = null, glow = null, frame = 0, playing = true, raf = 0, last = 0, acc = 0, onScreen = true;
  let L = null, W = 0, H = 0, dpr = 1, grid = null, bar = null;

  RL.fetchJSON("tapes.json").then((tapes) => {
    tape = tapes["tornado-ok"] || Object.values(tapes)[0];
    if (!tape) return;
    bits = RL.decodeBits(tape.bits);
    glow = new Float32Array(tape.n_regions);
    if (legend) {
      legend.textContent = `${tape.contract}, replayed: ${tape.n_regions.toLocaleString("en-US")} counties, one frame per ${tape.period}, ${tape.years[0]} to ${tape.years[1]}. A tile lights when its county records a damaging ${tape.hazard.replace(/_/g, " ")} event; the shading underneath is how often it has.`;
      legend.hidden = false;
    }
    if (pauseBtn) {
      pauseBtn.hidden = false;
      pauseBtn.addEventListener("click", () => (playing ? pause() : play()));
    }
    window.addEventListener("resize", resize);
    document.addEventListener("visibilitychange", () => (document.hidden ? stop() : (playing && onScreen && start())));
    if ("IntersectionObserver" in window && hero) {
      new IntersectionObserver((entries) => {
        onScreen = entries.some((e) => e.isIntersecting);
        if (onScreen && playing) start(); else stop();
      }, { threshold: 0.05 }).observe(hero);
    }
    resize();
    if (reduced.matches) { playing = false; pauseBtn && pauseBtn.classList.add("is-paused"); draw(); }
    else start();
  }).catch(() => {});

  function pause() { playing = false; stop(); pauseBtn.classList.add("is-paused"); pauseBtn.setAttribute("aria-label", "Play the animation"); }
  function play() { playing = true; pauseBtn.classList.remove("is-paused"); pauseBtn.setAttribute("aria-label", "Pause the animation"); start(); }
  function start() { if (!raf) { last = 0; raf = requestAnimationFrame(tick); } }
  function stop() { if (raf) { cancelAnimationFrame(raf); raf = 0; } }

  function resize() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    const r = canvas.getBoundingClientRect();
    W = Math.max(1, Math.round(r.width)); H = Math.max(1, Math.round(r.height));
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const narrow = W < 600;
    // the tiles sit to the right of the title on wide screens, across the band on phones
    grid = narrow
      ? { x: 24, y: 22, w: W - 48, h: H - 74 }
      : { x: Math.round(W * 0.40), y: Math.round(H * 0.11), w: Math.round(W * 0.54), h: Math.round(H * 0.62) };
    bar = { x: grid.x, y: grid.y + grid.h + (narrow ? 18 : 40), w: grid.w, h: 6 };
    L = RL.tapeLayout(tape.n_regions, grid.w, grid.h, 0);
    draw();
  }

  function tick(ts) {
    raf = 0;
    if (!last) last = ts;
    acc += ts - last; last = ts;
    let stepped = false;
    while (acc >= FRAME_MS) { acc -= FRAME_MS; step(); stepped = true; }
    if (stepped) draw();
    if (playing && onScreen) raf = requestAnimationFrame(tick);
  }

  function step() {
    frame = (frame + 1) % tape.n_periods;
    const base = frame * tape.n_regions;
    for (let r = 0; r < tape.n_regions; r++) {
      glow[r] = RL.tapeBit(bits, base + r) ? 1 : glow[r] * DECAY;
    }
  }

  function periodLabel(p) {
    const ppy = tape.periods_per_year, year = tape.years[0] + Math.floor(p / ppy), k = p % ppy;
    if (ppy === 4) return `${year} · Q${k + 1}`;
    if (ppy === 12) return `${year} · ${MONTHS[k]}`;
    return String(year);
  }

  function roundRect(x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y); ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r); ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h); ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r); ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
  }

  function draw() {
    if (!tape || !L) return;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#f4f6f8"; ctx.fillRect(0, 0, W, H);
    const maxFreq = Math.max(1, ...tape.freq);
    const side = Math.max(1, L.cell - L.gap), rad = Math.min(4, side * 0.22);
    for (let r = 0; r < tape.n_regions; r++) {
      const [x, y] = L.at(r);
      const rest = RL.mix(BASE, TINT, Math.sqrt(tape.freq[r] / maxFreq));
      ctx.fillStyle = glow[r] > 0.02 ? RL.mix(rest, LIT, glow[r]) : rest;
      roundRect(grid.x + x, grid.y + y, side, side, rad);
      ctx.fill();
    }
    // the timeline: split-tinted, with the count of lit counties per period as a sparkline
    const n = tape.n_periods, px = (p) => bar.x + (p / n) * bar.w;
    const ppy = tape.periods_per_year, y0 = tape.years[0];
    for (const name of ["train", "validate", "test"]) {
      const s = tape.splits[name]; if (!s) continue;
      const a = (s[0] - y0) * ppy, b = (s[1] - y0 + 1) * ppy;
      ctx.fillStyle = SPLIT_INK[name];
      roundRect(px(a), bar.y, px(b) - px(a) - 1, bar.h, 2); ctx.fill();
    }
    const maxCount = Math.max(1, ...tape.counts), spark = 26;
    for (let p = 0; p < n; p++) {
      const h = Math.max(0, (tape.counts[p] / maxCount) * spark);
      if (!h) continue;
      ctx.fillStyle = p === frame ? LIT : "rgba(43,90,168,.28)";
      ctx.fillRect(px(p), bar.y - 4 - h, Math.max(1, bar.w / n - 0.6), h);
    }
    // split labels: the first left-aligned, the last right-aligned, the middle
    // centred; a label that would collide with a neighbour is shortened, then
    // dropped, so a narrow band never overprints
    ctx.font = "500 12px " + getComputedStyle(document.body).fontFamily;
    ctx.fillStyle = "#6b7079"; ctx.textBaseline = "top"; ctx.textAlign = "left";
    const names = ["train", "validate", "test"].filter((k) => tape.splits[k]);
    const boxes = [];
    names.forEach((name, i) => {
      const s = tape.splits[name];
      const a = px((s[0] - y0) * ppy), b = px((s[1] - y0 + 1) * ppy);
      for (const text of [`${name} ${s[0]}–${s[1]}`, name, ""]) {
        if (!text) break;
        const w = ctx.measureText(text).width;
        let x = i === 0 ? a : i === names.length - 1 ? b - w : (a + b - w) / 2;
        x = Math.max(bar.x, Math.min(bar.x + bar.w - w, x));
        if (!boxes.some(([l, r]) => x < r + 8 && x + w > l - 8)) {
          boxes.push([x, x + w]);
          ctx.fillText(text, x, bar.y + bar.h + 6);
          break;
        }
      }
    });
    if (playing || reduced.matches === false) {
      const x = px(frame);
      ctx.fillStyle = "#15171b";
      ctx.fillRect(x - 0.5, bar.y - 6 - spark, 1.5, spark + bar.h + 8);
      ctx.font = "500 13px " + getComputedStyle(document.body).fontFamily;
      ctx.textAlign = x > bar.x + bar.w * 0.8 ? "right" : "left";
      ctx.fillText(periodLabel(frame), x + (ctx.textAlign === "left" ? 6 : -6), bar.y - 8 - spark - 16);
    }
  }
})();
