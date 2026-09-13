/* The browser sandbox's worker: Pyodide plus the real `readiness` package.

   Protocol (main thread -> worker):
     {type:"init", zipURL, pyURL}            load Pyodide, unpack the repo snapshot at /repo
     {type:"cli", id, argv:[...]}            run `readiness <argv>`; stdout streams back as "out"
     {type:"call", id, fn, args:{...}}       call a sandbox.py entry point with a JSON argument
   (worker -> main thread):
     {type:"status", stage, detail} {type:"ready", info, pyodide}
     {type:"out", id, text, stream?} {type:"done", id, code} {type:"result", id, value}
     {type:"error", id?, message}
*/
"use strict";

const PYODIDE_VERSION = "0.28.3";
const INDEX_URL = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;

let pyodide = null;
let current = null; // the id of the command whose stdout is streaming

const post = (msg) => self.postMessage(msg);
const status = (stage, detail) => post({ type: "status", stage, detail });

async function fetchOk(url, label) {
  const r = await fetch(url, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${label}: HTTP ${r.status} — run \`make site\` to generate it`);
  return r;
}

async function init(cfg) {
  status("pyodide", `loading Pyodide ${PYODIDE_VERSION} (a Python runtime, ~12 MB) from cdn.jsdelivr.net`);
  importScripts(INDEX_URL + "pyodide.js");
  pyodide = await self.loadPyodide({ indexURL: INDEX_URL });
  // Pyodide unvendors a few standard-library modules. The data plane defines an
  // HTTPS connection class at import time, so `ssl` has to be present even
  // though nothing in the sandbox ever opens a socket.
  status("stdlib", "loading the ssl module Pyodide ships separately");
  await pyodide.loadPackage("ssl");
  pyodide.setStdout({ batched: (s) => post({ type: "out", id: current, text: s }) });
  pyodide.setStderr({ batched: (s) => post({ type: "out", id: current, text: s, stream: "err" }) });
  status("archive", "fetching the repository snapshot: package, contracts, ledgers, fingerprints, pinned data");
  const zip = await (await fetchOk(cfg.zipURL, "sandbox.zip")).arrayBuffer();
  status("unpack", `unpacking ${(zip.byteLength / 1e6).toFixed(2)} MB into the browser's in-memory file system at /repo`);
  pyodide.unpackArchive(zip, "zip", { extractDir: "/repo" });
  status("boot", "importing readiness");
  const py = await (await fetchOk(cfg.pyURL, "sandbox.py")).text();
  pyodide.runPython(py);
  const info = JSON.parse(pyodide.runPython("sandbox_info()"));
  post({ type: "ready", info, pyodide: PYODIDE_VERSION });
}

self.onmessage = async (e) => {
  const m = e.data;
  try {
    if (m.type === "init") { await init(m); return; }
    if (!pyodide) throw new Error("the sandbox is not ready");
    if (m.type === "cli") {
      current = m.id;
      const run = pyodide.globals.get("run_cli");
      let code;
      try { code = run(JSON.stringify(m.argv)); } finally { current = null; if (run.destroy) run.destroy(); }
      post({ type: "done", id: m.id, code });
    } else if (m.type === "call") {
      current = m.id;
      const fn = pyodide.globals.get(m.fn);
      if (!fn) throw new Error(`no sandbox entry point named ${m.fn}`);
      let res;
      try { res = fn(JSON.stringify(m.args || {})); } finally { current = null; if (fn.destroy) fn.destroy(); }
      post({ type: "result", id: m.id, value: JSON.parse(res) });
    }
  } catch (err) {
    current = null;
    post({ type: "error", id: m.id, message: String((err && err.message) || err) });
  }
};
