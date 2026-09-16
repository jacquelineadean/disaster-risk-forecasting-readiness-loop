"""The Python side of the browser sandbox.

Runs inside Pyodide after `sandbox.zip` — the `readiness` package, the
registered contracts, the committed ledgers, the blessed fingerprints and the
pinned extracts — has been unpacked at /repo. Everything here is a thin
adapter over the package: the walkthrough commands go through `readiness.cli`
unchanged, and the guided demos call the same harness functions the CLI calls.
Each entry point takes one JSON string and returns one JSON string, which is
the whole protocol between the worker and this module.

Nothing here is imported by the package or its tests as part of the loop; the
site's tests import it natively to check the adapters against a synthetic
dataset.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import shutil
import sys
import traceback

REPO = pathlib.Path(os.environ.get("SANDBOX_REPO", "/repo"))
SANDBOX = pathlib.Path(os.environ.get("SANDBOX_ROOT", "/sandbox"))

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
os.chdir(REPO)
os.environ.setdefault("READINESS_EXPERIMENTS_DIR", str(SANDBOX / "experiments"))
(SANDBOX / "experiments").mkdir(parents=True, exist_ok=True)

from readiness import __version__, config, contracts, dashboard, verify  # noqa: E402
from readiness import cli as _cli  # noqa: E402
from readiness import data as data_mod  # noqa: E402
from readiness.connectors.base import ConnectorError  # noqa: E402
from readiness.contracts import ContractError  # noqa: E402
from readiness.engine import build_model  # noqa: E402
from readiness.engine.features import FEATURE_SETS  # noqa: E402
from readiness.engine.registry import REGISTRY  # noqa: E402
from readiness.harness import contract as contract_mod  # noqa: E402
from readiness.harness import scoring  # noqa: E402
from readiness.harness.metrics import MetricError  # noqa: E402
from readiness.harness.ledger import GENESIS, Ledger  # noqa: E402
from readiness.harness.splits import SplitViolation  # noqa: E402

TREES = {"fresh": SANDBOX / "experiments", "committed": REPO / "experiments"}


def _args(raw: str | None) -> dict:
    return json.loads(raw) if raw else {}


def _fail(exc: BaseException) -> str:
    return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------------------
# the command line, unchanged
# ---------------------------------------------------------------------------


#: Commands the browser refuses outright, and why. `promote` is the one
#: atomic test touch and writes a card the repository is meant to commit; a
#: touch spent in a tab is a spent budget with no record, so it is refused
#: here rather than budgeted. `backtest` reads only committed files, but its
#: report is the published record of that touch and belongs beside the ledger
#: in git, not in a browser's memory.
NOT_IN_BROWSER = {
    "snapshot": "a network connection",
    "mcp": "a process",
    "report": "a process",
    "promote": "the committed test-touch budget; a test touch spent from a browser "
               "would be a spent budget with no card in the repository",
    "backtest": "the committed ledger and backtest report of a real-data run; "
                "there is nothing to publish from a browser",
}


def _refuse(command: str) -> int:
    print(f"`readiness {command}` is not available in the browser sandbox: "
          f"it needs {NOT_IN_BROWSER[command]}.")
    return 2


def _requested_features(argv: list[str]) -> list[str]:
    """The connectors `--features a,b` names, or every connector by default."""
    for i, arg in enumerate(argv):
        if arg == "--features" and i + 1 < len(argv):
            return [x for x in argv[i + 1].split(",") if x]
        if arg.startswith("--features="):
            return [x for x in arg.split("=", 1)[1].split(",") if x]
    return list(data_mod.FEATURE_CONNECTORS)


def _features_in_browser(argv: list[str]) -> int:
    """What `readiness features` can say here: the archive packs no feature data.

    The sandbox carries Storm Events extracts only, so instead of failing on a
    missing pinned file it reports, per connector, that nothing is packed,
    and lists the feature sets that would need each. Admission verdicts and
    the audit are what the real command prints on a machine with the data.
    """
    names = _requested_features(argv)
    unknown = sorted(set(names) - set(data_mod.FEATURE_CONNECTORS))
    if unknown:
        print(f"unknown feature connector(s) {unknown}; "
              f"known: {list(data_mod.FEATURE_CONNECTORS)}")
        return 2
    print()
    print("feature sources  (browser sandbox)")
    print("-" * 60)
    for name in names:
        print(f"  {name:<10} no feature sources packed in the browser sandbox")
    print()
    print("feature sets in the catalogue (none can be built here):")
    for set_name, specs in FEATURE_SETS.items():
        sources = sorted({spec.source for spec in specs})
        print(f"  {set_name:<18} {', '.join(s.column for s in specs)}  "
              f"[sources: {', '.join(sources)}]")
    print()
    print("Run `readiness features -c <contract>` on a machine with the pinned "
          "feature data (`readiness snapshot --features era5,terrain`) for the "
          "admission verdicts and the audit.")
    return 0


def run_cli(argv_json: str) -> int:
    """Run `readiness <argv>` exactly as the console script would."""
    argv = json.loads(argv_json)
    if argv and argv[0] in NOT_IN_BROWSER:
        return _refuse(argv[0])
    if argv and argv[0] == "features":
        return _features_in_browser(argv)
    try:
        return int(_cli.main(argv) or 0)
    except SystemExit as exc:  # argparse errors and explicit exits
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    except (ConnectorError, ContractError, SplitViolation, MetricError) as exc:
        print()
        print(str(exc))
        return 2
    except Exception:  # noqa: BLE001 - shown to the visitor, not swallowed
        traceback.print_exc()
        return 1


# ---------------------------------------------------------------------------
# what is in the sandbox
# ---------------------------------------------------------------------------


def _state_fips() -> dict[str, str]:
    return data_mod.state_fips()


def _packed_types() -> dict[str, list[str] | None]:
    """Per state FIPS, the event types the sandbox archive packed (None = all)."""
    path = data_mod.SNAPSHOT_DIR / "sandbox_coverage.json"
    if not path.exists():
        return {}
    states = json.loads(path.read_text())["states"]
    return {k: v.get("event_types") for k, v in states.items()}


def _data_gap(c: contracts.Contract, fips_of: dict[str, str]) -> str | None:
    """Why the sandbox cannot build this contract's panel, or None if it can."""
    if not c.states:
        return "the sandbox packs per-state extracts; a national scope is not among them"
    if not fips_of:
        return "the sandbox has no pinned data at all"
    packed = _packed_types()
    for state in c.states:
        fips = fips_of.get(state)
        if fips is None:
            return f"{state!r} is not a state in the Census county file"
        for year in c.all_years():
            part = data_mod.SNAPSHOT_DIR / "storm_events" / f"{fips}_{year}.jsonl"
            if not part.exists():
                return f"no pinned Storm Events extract for {state} {year} in the sandbox"
        types = packed.get(fips, None) if packed else None
        if types is not None:
            missing = sorted(set(c.event_types) - set(types))
            if missing:
                return (
                    f"the sandbox packed {state} with {', '.join(types)} rows only; "
                    f"{', '.join(missing)} events for it are not on board"
                )
    crosswalk = data_mod.SNAPSHOT_DIR / "nws" / "zone_county.dbx"
    if c.zone_policy == "expand" and not crosswalk.exists():
        return "the sandbox has no NWS zone crosswalk"
    return None


def _has_data(c: contracts.Contract, fips_of: dict[str, str]) -> bool:
    return _data_gap(c, fips_of) is None


def sandbox_info(_raw: str | None = None) -> str:
    fips_of = _state_fips()
    registry = contracts.registered()
    tree = "committed" if data_mod.experiments_root() == TREES["committed"] else "fresh"
    return json.dumps(
        {
            "package_version": __version__,
            "python": sys.version.split()[0],
            "tree": tree,
            "experiments_dir": str(data_mod.experiments_root()),
            "contracts": {
                name: {
                    "digest": c.digest(),
                    "hazard": c.hazard,
                    "scope": c.scope_label,
                    "period": c.period,
                    "states": list(c.states),
                    "has_data": _has_data(c, fips_of),
                    "data_gap": _data_gap(c, fips_of),
                    "has_ledger": data_mod.paths(c).ledger.exists(),
                }
                for name, c in registry.items()
            },
            "hazards": {
                name: {"event_types": list(h.event_types), "coding": h.coding}
                for name, h in config.HAZARDS.items()
            },
            "models": list(REGISTRY),
            # The archive packs Storm Events extracts only: no ERA5, terrain,
            # NRI or CLIMADA layer. The page says so before a visitor asks a
            # feature model for a non-empty feature-set list.
            "feature_sources_packed": [],
            "feature_connectors": list(data_mod.FEATURE_CONNECTORS),
        }
    )


def set_tree(raw: str) -> str:
    which = _args(raw).get("tree", "fresh")
    if which not in TREES:
        return json.dumps({"error": f"unknown tree {which!r}"})
    os.environ["READINESS_EXPERIMENTS_DIR"] = str(TREES[which])
    TREES[which].mkdir(parents=True, exist_ok=True)
    return sandbox_info()


# ---------------------------------------------------------------------------
# datasets, cached per contract digest for the guided demos
# ---------------------------------------------------------------------------

#: Keyed by contract digest, not name: `register --force` keeps the name and
#: changes the criteria, and a panel built under the old criteria must not be
#: served for the new ones. The digest is a function of exactly the fields
#: the panel depends on.
_DATASETS: dict[str, data_mod.Dataset] = {}


def _dataset(c: contracts.Contract) -> data_mod.Dataset:
    key = c.digest()
    if key not in _DATASETS:
        gap = _data_gap(c, _state_fips())
        if gap is not None:
            raise ConnectorError(
                f"cannot build the panel for {c.name} in the browser: {gap}. The "
                "contract registers and reads here; building its panel needs "
                "`readiness snapshot` on a machine with a network connection."
            )
        _DATASETS[key] = data_mod.build(c)
    return _DATASETS[key]


# ---------------------------------------------------------------------------
# the calibration playground
# ---------------------------------------------------------------------------


def _logit(p: float) -> float:
    p = min(1 - 1e-9, max(1e-9, p))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


class Recalibrated:
    """A proposable model's forecasts, passed through a monotone recalibration.

    `q = sigmoid(scale * logit(p) + shift)`: scale > 1 sharpens, scale < 1
    flattens toward 0.5, shift moves every forecast up or down in logit
    space. The base model is fitted through the same TrainingView as always;
    only its output is transformed. The training digest is the base model's,
    so the canary's provenance check sees exactly what it would see for the
    base model.
    """

    def __init__(self, base, scale: float = 1.0, shift: float = 0.0) -> None:
        self.base = base
        self.scale = scale
        self.shift = shift
        self.name = base.name if (scale == 1.0 and shift == 0.0) else f"{base.name}+recal"
        self.version = base.version
        self.training_digest = None

    def fit(self, view) -> None:
        self.base.fit(view)
        self.training_digest = getattr(self.base, "training_digest", None)

    def predict(self, request):
        probs = self.base.predict(request)
        if self.scale == 1.0 and self.shift == 0.0:
            return list(probs)
        return [
            min(1.0, max(0.0, _sigmoid(self.scale * _logit(p) + self.shift)))
            for p in probs
        ]


#: Base (unrecalibrated) scorecards, keyed by contract digest like the datasets.
_BASE_CARDS: dict[tuple, dict] = {}

#: The only split the browser may score. The test split is a one-shot holdout
#: whose touches are budgeted on disk by `readiness promote`; the playground
#: has no budget, so it has no business there, and the training split is not
#: a holdout at all.
PLAYGROUND_SPLIT = "validate"


def score_playground(raw: str) -> str:
    """Fit a model, recalibrate its forecasts, and re-score with the real harness."""
    a = _args(raw)
    try:
        name = a["contract"]
        model_name = a.get("model", "climatology-seasonal")
        params = {k: v for k, v in (a.get("params") or {}).items() if v is not None}
        scale = float(a.get("scale", 1.0))
        shift = float(a.get("shift", 0.0))
        split = a.get("split", PLAYGROUND_SPLIT)
        if split != PLAYGROUND_SPLIT:
            raise SplitViolation(
                f"the playground scores the {PLAYGROUND_SPLIT} split only; "
                f"{split!r} is refused. The test split is spent through "
                "`readiness promote MODEL --spend-test-touch`, the one atomic "
                "test touch, which charges the budget and writes the card together."
            )
        c = contracts.load(name)
        ds = _dataset(c)

        def build():
            return build_model(model_name, canary_panel=ds.panel, **params)

        key = (c.name, c.digest(), model_name, json.dumps(params, sort_keys=True), split)
        if key not in _BASE_CARDS:
            base_card = scoring.score(build(), ds.panel, c, split)
            _BASE_CARDS[key] = base_card.to_dict()
        model = Recalibrated(build(), scale, shift)
        card, report = scoring.screen(model, ds.panel, c, split)
        verdict = contract_mod.evaluate(card, c)
        return json.dumps(
            {
                "contract": name,
                "model": model.name,
                "scorecard": card.to_dict(),
                "verdict": verdict.to_dict(),
                "canary": report.to_dict(),
                "base_scorecard": _BASE_CARDS[key],
                "text": "\n".join(
                    [card.format(), "", verdict.format(), "", report.format()]
                ),
                "tolerance": c.reliability_tolerance_pp,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ---------------------------------------------------------------------------
# tampering with a ledger
# ---------------------------------------------------------------------------

TAMPER_ACTIONS = {
    "edit": "rewrite scorecard.brier_skill_score on exp-0002 (+0.25) without re-sealing",
    "swap": "swap the lines for exp-0002 and exp-0003",
    "delete-middle": "delete the line for exp-0003",
    "truncate": "delete the last line — a perfectly self-consistent prefix remains",
    "no-anchor": "delete ledger.jsonl.anchor.json",
    "forge": "delete the last line AND rewrite the anchor to match",
}


def ledger_state(raw: str) -> str:
    a = _args(raw)
    try:
        c = contracts.load(a["contract"])
        where = data_mod.paths(c)
        ledger = Ledger(where.ledger)
        status = ledger.verify()
        return json.dumps(
            {
                "contract": c.name,
                "path": str(where.ledger),
                "exists": where.ledger.exists(),
                "n_cards": len(ledger) if where.ledger.exists() else 0,
                "valid": status.valid,
                "status": status.format(),
                "backup": where.ledger.with_suffix(".jsonl.orig").exists(),
                "actions": TAMPER_ACTIONS,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def tamper(raw: str) -> str:
    a = _args(raw)
    try:
        c = contracts.load(a["contract"])
        action = a["action"]
        if action not in TAMPER_ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        where = data_mod.paths(c)
        ledger = Ledger(where.ledger)
        if not where.ledger.exists():
            raise FileNotFoundError(
                f"{c.name} has no ledger in this experiments tree yet; run the loop first"
            )
        backup = where.ledger.with_suffix(".jsonl.orig")
        anchor_backup = ledger.anchor_path.with_suffix(".json.orig")
        if not backup.exists():
            shutil.copyfile(where.ledger, backup)
            if ledger.anchor_path.exists():
                shutil.copyfile(ledger.anchor_path, anchor_backup)
        text = where.ledger.read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if line.strip()]

        # Each action needs a minimum number of cards to make sense against;
        # a ledger shorter than that cannot be tampered with the way the demo
        # describes, and must say so rather than silently do nothing and
        # report success.
        needs = {"edit": 2, "swap": 3, "delete-middle": 3, "truncate": 1, "forge": 1}
        minimum = needs.get(action, 0)
        if len(lines) < minimum:
            status = ledger.verify()
            return json.dumps(
                {
                    "contract": c.name,
                    "action": action,
                    "description": (
                        f"could not apply — {c.name}'s ledger has only "
                        f"{len(lines)} card(s), and {action!r} needs at least "
                        f"{minimum}. Run the loop again first. The ledger was "
                        "not touched."
                    ),
                    "applied": False,
                    "valid": status.valid,
                    "status": status.format(),
                    "forged": False,
                }
            )

        if action == "edit":
            import re

            def bump(m: "re.Match[str]") -> str:
                return f'"brier_skill_score":{float(m.group(1)) + 0.25:.4f}'

            lines[1] = re.sub(
                r'"brier_skill_score":(-?[0-9.eE+-]+)', bump, lines[1], count=1
            )
        elif action == "swap":
            lines[1], lines[2] = lines[2], lines[1]
        elif action == "delete-middle":
            del lines[2]
        elif action in ("truncate", "forge"):
            lines.pop()
        elif action == "no-anchor":
            if ledger.anchor_path.exists():
                ledger.anchor_path.unlink()
        where.ledger.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        if action == "forge":
            head = json.loads(lines[-1])["card_hash"] if lines else GENESIS
            ledger._write_anchor(len(lines), head)
        status = Ledger(where.ledger).verify()
        return json.dumps(
            {
                "contract": c.name,
                "action": action,
                "description": TAMPER_ACTIONS[action],
                "applied": True,
                "valid": status.valid,
                "status": status.format(),
                "forged": action == "forge",
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def restore_ledger(raw: str) -> str:
    a = _args(raw)
    try:
        c = contracts.load(a["contract"])
        where = data_mod.paths(c)
        ledger = Ledger(where.ledger)
        backup = where.ledger.with_suffix(".jsonl.orig")
        anchor_backup = ledger.anchor_path.with_suffix(".json.orig")
        if backup.exists():
            shutil.move(backup, where.ledger)
        if anchor_backup.exists():
            shutil.move(anchor_backup, ledger.anchor_path)
        status = Ledger(where.ledger).verify()
        return json.dumps(
            {"contract": c.name, "valid": status.valid, "status": status.format()}
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ---------------------------------------------------------------------------
# the dashboard, and the fingerprints
# ---------------------------------------------------------------------------


def dashboard_html(raw: str) -> str:
    a = _args(raw)
    try:
        c = contracts.load(a["contract"])
        where = data_mod.paths(c)
        html = dashboard.render(c, Ledger(where.ledger))
        return json.dumps({"contract": c.name, "html": html})
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def fingerprints(raw: str) -> str:
    """Recompute the Phase 0 fingerprints and diff them against the blessed file."""
    a = _args(raw)
    try:
        c = contracts.load(a["contract"])
        observed = verify.fingerprints(_dataset(c))
        expected_path = data_mod.paths(c).expected
        expected = None
        if expected_path.exists():
            expected = json.loads(expected_path.read_text())
        rows = []
        if expected is not None:
            for key in sorted(set(expected) | set(observed)):
                e, o = expected.get(key), observed.get(key)
                if isinstance(e, dict) or isinstance(o, dict):
                    for field in sorted(set(e or {}) | set(o or {})):
                        ev, ov = (e or {}).get(field), (o or {}).get(field)
                        rows.append({"group": key, "field": field, "expected": ev,
                                     "observed": ov, "match": ev == ov})
                else:
                    rows.append({"group": key, "field": "", "expected": e,
                                 "observed": o, "match": e == o})
        return json.dumps(
            {
                "contract": c.name,
                "expected_path": data_mod.relative(expected_path),
                "has_expected": expected is not None,
                "rows": rows,
                "all_match": bool(rows) and all(r["match"] for r in rows),
                "observed": observed,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def read_file(raw: str) -> str:
    """A small file from the sandbox file system, for showing what a command wrote."""
    a = _args(raw)
    try:
        path = pathlib.Path(a["path"])
        text = path.read_text(encoding="utf-8")
        limit = int(a.get("limit", 200_000))
        return json.dumps({"path": str(path), "text": text[:limit]})
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)
