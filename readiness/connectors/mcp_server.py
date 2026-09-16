"""A read-only MCP server over the pinned data and one contract's experiment ledger.

Report §5, data plane: "MCP servers / tools per source: storm-events,
openfema-nri, footprints, forecast-grids."

Phase 0 ships the `storm-events` side of that plus read access to the harness's
own outputs, which is what an agent actually needs to run the loop: see the
contract, see the panel, read past experiments, read score reports. Every tool
here is a read. There is deliberately no tool that writes a label, edits a
contract, or returns a holdout outcome — an agent connected to this server
cannot cheat through it.

The server is bound to one registered contract, chosen the same way the CLI
chooses (`readiness mcp -c NAME`). Implemented against the stdio transport with
the standard library only, so it runs from a clean clone with nothing
installed:

    readiness mcp -c NAME

or, in an MCP client config:

    {"command": "python3", "args": ["-m", "readiness.cli", "mcp", "-c", "NAME"]}
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from readiness import __version__, contracts, data as data_mod
from readiness.connectors.base import Manifest
from readiness.contracts import Contract
from readiness.harness.ledger import Ledger

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "readiness-data", "version": __version__}

_state: dict[str, Any] = {"contract": None, "dataset": None}


def configure(contract: Contract | None) -> None:
    """Bind the server to a contract (or reset it, so the next call resolves anew)."""
    _state["contract"] = contract
    _state["dataset"] = None


def _contract() -> Contract:
    if _state["contract"] is None:
        _state["contract"] = contracts.resolve()
    return _state["contract"]


def _dataset() -> data_mod.Dataset:
    if _state["dataset"] is None:
        _state["dataset"] = data_mod.build(_contract())
    return _state["dataset"]


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def tool_contract(_args: dict) -> str:
    return _contract().describe()


def tool_contracts(_args: dict) -> str:
    return contracts.describe_registry()


def tool_manifest(_args: dict) -> str:
    return Manifest.load(data_mod.MANIFEST_PATH).summary()


def tool_panel_summary(_args: dict) -> str:
    ds = _dataset()
    from readiness.harness import splits

    return "\n".join(
        [
            ds.panel.summary(),
            f"regions: {len(ds.regions)}",
            f"data version: sha256:{ds.data_version}",
            "",
            "split coverage:",
            splits.coverage_report(ds.panel, ds.contract.splits),
            "",
            "event coverage:",
            ds.diagnostics.format(),
        ]
    )


def tool_region_history(args: dict) -> str:
    """Training-split label history for one region. Never returns holdout years.

    An optional `year` narrows the rows to one year; a year outside the
    training split is refused before any row is read, so the refusal carries
    no label and does not say which holdout split the year belongs to.
    """
    region = str(args.get("region_id", "")).strip()
    if not region:
        return "error: region_id is required"
    ds = _dataset()
    contract = ds.contract
    train_years = set(contract.train_years)
    unavailable = (
        f"only train years ({min(train_years)}-{max(train_years)}) are exposed "
        "through this tool; validate and test labels are not available through "
        "any tool on this server."
    )
    year = args.get("year")
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError):
            return f"error: year must be an integer, got {year!r}"
        if year not in train_years:
            return f"refused: year {year} is outside the training split. {unavailable}"
    rows = [
        (u, y)
        for u, y in zip(ds.panel.units, ds.panel.labels)
        if u[0] == region and u[1] in train_years and (year is None or u[1] == year)
    ]
    if not rows:
        return f"no training-split rows for region {region}. Note: {unavailable}"
    lines = [f"region {region}: {len(rows)} training region-{contract.period}s, "
             f"{sum(y for _, y in rows)} positive"]
    by_period: dict[int, list[int]] = {}
    for (_r, _y, p), label in rows:
        by_period.setdefault(p, []).append(label)
    for p in sorted(by_period):
        vals = by_period[p]
        lines.append(
            f"  {contract.period} {p}: {sum(vals)}/{len(vals)} years with a damaging "
            f"event ({sum(vals) / len(vals):.3f})"
        )
    return "\n".join(lines)


def tool_ledger(args: dict) -> str:
    ledger = Ledger(data_mod.paths(_contract()).ledger)
    if args.get("experiment_id"):
        for card in ledger.read():
            if card.experiment_id == args["experiment_id"]:
                return json.dumps(card.record(), indent=2)
        return f"no experiment {args['experiment_id']!r}"
    return ledger.summary()


def tool_models(_args: dict) -> str:
    from readiness.engine import describe_registry

    return describe_registry()


TOOLS: dict[str, tuple[dict, Callable[[dict], str]]] = {
    "get_contract": (
        {
            "description": (
                "The pre-registered acceptance contract this server is bound to: "
                "forecast unit, damage threshold, locked splits, and the "
                "thresholds a model must clear. Read this before proposing anything."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        tool_contract,
    ),
    "list_contracts": (
        {
            "description": (
                "Every registered contract, with its hazard, scope and digest."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        tool_contracts,
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
                "Size, base rate, digest, per-split coverage and event coverage of "
                "the contract's labelled region-period panel."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        tool_panel_summary,
    ),
    "get_region_history": (
        {
            "description": (
                "Per-period damaging-event frequency for one region, over the "
                "TRAINING years only. Holdout labels are not available from this "
                "server."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "region_id": {
                        "type": "string",
                        "description": (
                            "region identifier; a 5-digit county FIPS for US contracts"
                        ),
                    },
                    "year": {
                        "type": "integer",
                        "description": (
                            "optional: one training year only; any other year is "
                            "refused"
                        ),
                    },
                },
                "required": ["region_id"],
            },
        },
        tool_region_history,
    ),
    "read_ledger": (
        {
            "description": (
                "The contract's append-only experiment ledger: what has been "
                "tried, what scored, and whether the hash chain is intact."
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
            "description": "Models that can be proposed and scored against any contract.",
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
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle(message: object) -> dict | None:
    """Dispatch one JSON-RPC message. Returns None for notifications."""
    if not isinstance(message, dict):
        # Valid JSON that is not an object: a bare list, number or string. Such
        # a message has no id to answer to, but it must be answered rather than
        # allowed to raise, or one stray line would take the server down.
        return _error(
            None,
            -32600,
            f"invalid request: a JSON-RPC message is an object, got "
            f"{type(message).__name__} (batches are not supported)",
        )
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


def serve(stdin=None, stdout=None, *, contract: Contract | None = None) -> None:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    if contract is not None:
        configure(contract)
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
