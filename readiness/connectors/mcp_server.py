"""A read-only MCP server over the pinned data and the experiment ledger.

Report §5, data plane: "MCP servers / tools per source: storm-events,
openfema-nri, footprints, forecast-grids."

Phase 0 ships the `storm-events` side of that plus read access to the harness's
own outputs, which is what an agent actually needs to run the loop: see the
panel, read past experiments, read score reports. Every tool here is a read.
There is deliberately no tool that writes a label, edits the contract, or
returns a holdout outcome — an agent connected to this server cannot cheat
through it.

Implemented against the stdio transport with the standard library only, so it
runs from a clean clone with nothing installed:

    readiness mcp

or, in an MCP client config:

    {"command": "python3", "args": ["-m", "readiness.cli", "mcp"]}
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from readiness import config, data as data_mod
from readiness.connectors.base import Manifest
from readiness.harness.ledger import Ledger

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "readiness-storm-events", "version": "0.1.0"}

_cache: dict[str, Any] = {}


def _dataset():
    if "dataset" not in _cache:
        _cache["dataset"] = data_mod.build()
    return _cache["dataset"]


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def tool_contract(_args: dict) -> str:
    return config.describe()


def tool_manifest(_args: dict) -> str:
    return Manifest.load(data_mod.MANIFEST_PATH).summary()


def tool_panel_summary(_args: dict) -> str:
    ds = _dataset()
    from readiness.harness import splits

    return "\n".join(
        [
            ds.panel.summary(),
            f"counties: {len(ds.counties)}",
            f"data version: sha256:{ds.data_version}",
            "",
            "split coverage:",
            splits.coverage_report(ds.panel),
        ]
    )


def tool_county_history(args: dict) -> str:
    """Training-split label history for one county. Never returns holdout years."""
    fips = str(args.get("county_fips", "")).strip()
    if not fips:
        return "error: county_fips is required"
    ds = _dataset()
    train_years = set(config.CONTRACT.train_years)
    rows = [
        (u, y)
        for u, y in zip(ds.panel.units, ds.panel.labels)
        if u[0] == fips and u[1] in train_years
    ]
    if not rows:
        return (
            f"no training-split rows for county {fips}. "
            f"Note: only train years ({min(train_years)}-{max(train_years)}) are "
            "exposed through this tool; validate and test labels are not "
            "available through any tool on this server."
        )
    lines = [f"county {fips}: {len(rows)} training county-quarters, "
             f"{sum(y for _, y in rows)} positive"]
    by_quarter: dict[int, list[int]] = {}
    for (_f, _y, q), label in rows:
        by_quarter.setdefault(q, []).append(label)
    for q in sorted(by_quarter):
        vals = by_quarter[q]
        lines.append(
            f"  Q{q}: {sum(vals)}/{len(vals)} years with a damaging event "
            f"({sum(vals) / len(vals):.3f})"
        )
    return "\n".join(lines)


def tool_ledger(args: dict) -> str:
    ledger = Ledger(data_mod.LEDGER_PATH)
    if args.get("experiment_id"):
        for card in ledger.read():
            if card.experiment_id == args["experiment_id"]:
                return json.dumps(
                    card.payload() | {"card_hash": card.card_hash}, indent=2
                )
        return f"no experiment {args['experiment_id']!r}"
    return ledger.summary()


def tool_models(_args: dict) -> str:
    from readiness.engine import describe_registry

    return describe_registry()


TOOLS: dict[str, tuple[dict, Callable[[dict], str]]] = {
    "get_contract": (
        {
            "description": (
                "The pre-registered acceptance contract: forecast unit, damage "
                "threshold, locked splits, and the thresholds a model must clear. "
                "Read this before proposing anything."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        tool_contract,
    ),
    "get_data_manifest": (
        {
            "description": (
                "Checksums and provenance for every pinned source file. Identifies "
                "the exact data version an experiment ran against."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        tool_manifest,
    ),
    "get_panel_summary": (
        {
            "description": (
                "Size, base rate, digest and per-split coverage of the labelled "
                "county-quarter panel."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        tool_panel_summary,
    ),
    "get_county_history": (
        {
            "description": (
                "Per-quarter damaging-event frequency for one county, over the "
                "TRAINING years only. Holdout labels are not available from this "
                "server."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "county_fips": {
                        "type": "string",
                        "description": "5-digit state+county FIPS, e.g. 22005",
                    }
                },
                "required": ["county_fips"],
            },
        },
        tool_county_history,
    ),
    "read_ledger": (
        {
            "description": (
                "The append-only experiment ledger: what has been tried, what "
                "scored, and whether the hash chain is intact."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "experiment_id": {
                        "type": "string",
                        "description": "e.g. exp-0002; omit for the summary table",
                    }
                },
            },
        },
        tool_ledger,
    ),
    "list_models": (
        {
            "description": "Models that can be proposed and scored.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        tool_models,
    ),
}


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------


def _result(request_id: Any, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message: dict) -> dict | None:
    """Dispatch one JSON-RPC message. Returns None for notifications."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(
            request_id,
            {
                "tools": [
                    {"name": name, **schema} for name, (schema, _fn) in TOOLS.items()
                ]
            },
        )

    if method == "tools/call":
        name = params.get("name")
        entry = TOOLS.get(name)
        if entry is None:
            return _error(request_id, -32602, f"unknown tool {name!r}")
        _schema, fn = entry
        try:
            text = fn(params.get("arguments") or {})
        except Exception as exc:  # surface the failure to the client, don't die
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                },
            )
        return _result(request_id, {"content": [{"type": "text", "text": text}]})

    if request_id is None:
        return None
    return _error(request_id, -32601, f"method not found: {method}")


def serve(stdin=None, stdout=None) -> None:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            stdout.write(json.dumps(_error(None, -32700, f"parse error: {exc}")) + "\n")
            stdout.flush()
            continue
        response = handle(message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
