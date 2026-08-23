"""`readiness` — the command line for the Phase 0 loop.

    readiness contract          print the pre-registered contract and its hash
    readiness models            list proposable models
    readiness snapshot          pull and pin the data, print the manifest
    readiness panel             build the labelled panel, print split coverage
    readiness score MODEL       fit and score one model
    readiness loop              run the full experimental loop
    readiness canary            demonstrate the harness rejecting a leaked model
    readiness ledger            show and verify the experiment ledger
    readiness verify            check the Phase 0 exit criteria
    readiness report            rebuild the static research report
    readiness mcp               run the read-only MCP data server on stdio
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

from readiness import config, data as data_mod
from readiness.connectors.base import ConnectorError
from readiness.engine import build_model, describe_registry
from readiness.harness import canary as canary_mod
from readiness.harness import contract as contract_mod
from readiness.harness import scoring, splits
from readiness.harness.ledger import Ledger

EXPECTED_PATH = data_mod.EXPECTED_DIR / "phase0.json"

#: Scorecard fields that must reproduce exactly. Reliability bins are included
#: via a hash so a bin-level difference cannot hide behind matching aggregates.
_REPRO_FIELDS = (
    "n_units",
    "n_positive",
    "base_rate",
    "brier_score",
    "brier_score_reference",
    "brier_skill_score",
    "auc",
    "sharpness",
    "reliability",
    "resolution",
    "uncertainty",
    "panel_digest",
    "train_digest",
    "contract_digest",
)


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _rule(title: str) -> None:
    _p()
    _p(title)
    _p("-" * max(len(title), 60))


def _dataset(args) -> data_mod.Dataset:
    return data_mod.build(
        keep_raw=getattr(args, "keep_raw", False),
        refresh=getattr(args, "refresh", False),
        progress=_p if not getattr(args, "quiet", False) else (lambda _m: None),
    )


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_contract(args) -> int:
    _rule("pre-registered contract")
    _p(config.describe())
    if args.json:
        _p()
        _p(json.dumps(config.CONTRACT.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_models(args) -> int:
    _rule("proposable models")
    _p(describe_registry())
    return 0


def cmd_snapshot(args) -> int:
    _rule("data snapshot")
    ds = _dataset(args)
    _p()
    _p(ds.manifest.summary())
    return 0


def cmd_panel(args) -> int:
    _rule("county-quarter panel")
    ds = _dataset(args)
    _p()
    _p(f"  {ds.panel.summary()}")
    _p(f"  counties: {len(ds.counties)}  ({ds.counties[0]} ... {ds.counties[-1]})")
    _p()
    _p("split coverage")
    splits.assert_disjoint()
    _p(splits.coverage_report(ds.panel))
    return 0


def cmd_score(args) -> int:
    ds = _dataset(args)
    split = splits.get_split(args.split)
    needs_panel = args.model == "leaky-oracle"
    model = build_model(args.model, panel=ds.panel if needs_panel else None)

    if split.name == "test":
        budget = splits.TouchBudget(data_mod.TOUCH_BUDGET_PATH)
        budget.check(model.name, model.version)
        if not args.spend_test_touch:
            _p()
            _p(
                f"refusing to score against the test split without "
                f"--spend-test-touch.\nThis is the {config.CONTRACT.test_touch_budget}-shot "
                f"holdout ({split.years[0]}-{split.years[-1]}); once spent for "
                f"{model.name}@{model.version} it cannot be spent again."
            )
            return 2

    _rule(f"score  {model.name}@{model.version}  on {split}")
    card = scoring.score(model, ds.panel, split)
    _p(card.format())
    _p()
    _p(contract_mod.evaluate(card).format())

    _units, probs, outcomes = scoring.predictions_for(model, ds.panel, split)
    view = splits.TrainingView(splits.split_panel(ds.panel, splits.TRAIN), splits.TRAIN)
    report = canary_mod.run(
        probs=probs,
        outcomes=outcomes,
        brier_skill_score=card.brier_skill_score,
        auc=card.auc,
        view=view,
        declared_train_digest=getattr(model, "training_digest", None),
    )
    _p()
    _p(report.format())

    if split.name == "test" and args.spend_test_touch:
        spent = splits.TouchBudget(data_mod.TOUCH_BUDGET_PATH).spend(
            model.name, model.version
        )
        _p()
        _p(f"test touch {spent}/{config.CONTRACT.test_touch_budget} spent for "
           f"{model.name}@{model.version}")
    # Exit non-zero only when the canary rejects: a contract failure is a
    # legitimate experimental outcome, not a tool error.
    return 1 if report.rejected else 0


def cmd_loop(args) -> int:
    from readiness.agent import orchestrator

    if args.backend == "claude":
        orchestrator.run_claude(split_name=args.split, progress=_p)
        return 0

    _rule(f"experimental loop  ({args.backend} backend, split={args.split})")
    result = orchestrator.run_local(
        split_name=args.split,
        include_canary=not args.no_canary,
        progress=_p,
    )
    _p()
    _p(result.format())
    _p()
    _p(f"ledger: {data_mod.LEDGER_PATH.relative_to(data_mod.REPO_ROOT)}")
    return 0


def cmd_canary(args) -> int:
    ds = _dataset(args)
    split = splits.get_split(args.split)
    _rule(f"leakage canary  (target: leaky-oracle, split={split})")
    _p("A model with direct access to the outcomes it is scored on. The Phase 0")
    _p("exit criterion is that the harness rejects it.")
    _p()

    model = build_model("leaky-oracle", panel=ds.panel)
    card = scoring.score(model, ds.panel, split)
    _p(card.format())
    _p()
    _p("contract, if it were honoured:")
    _p(contract_mod.evaluate(card).format())

    _units, probs, outcomes = scoring.predictions_for(model, ds.panel, split)
    view = splits.TrainingView(splits.split_panel(ds.panel, splits.TRAIN), splits.TRAIN)
    report = canary_mod.run(
        probs=probs,
        outcomes=outcomes,
        brier_skill_score=card.brier_skill_score,
        auc=card.auc,
        view=view,
        declared_train_digest=getattr(model, "training_digest", None),
    )
    _p()
    _p(report.format())
    _p()
    if report.rejected:
        _p("PASS: the harness rejected a leaked model.")
        return 0
    _p("FAIL: the harness accepted a leaked model. Phase 0 does not exit.")
    return 1


def cmd_ledger(args) -> int:
    ledger = Ledger(data_mod.LEDGER_PATH)
    _rule("experiment ledger")
    if args.show:
        for card in ledger.read():
            if args.id and card.experiment_id != args.id:
                continue
            _p(json.dumps(card.payload() | {"card_hash": card.card_hash}, indent=2))
            _p()
        return 0
    _p(ledger.summary())
    return 0 if ledger.verify().valid else 1


def _repro_fingerprint(card: scoring.Scorecard) -> dict:
    import hashlib

    bins = json.dumps(card.reliability_bins, sort_keys=True, separators=(",", ":"))
    out = {f: getattr(card, f) for f in _REPRO_FIELDS}
    out["reliability_bins_sha256"] = hashlib.sha256(bins.encode()).hexdigest()[:16]
    return out


def cmd_verify(args) -> int:
    """Check the Phase 0 exit criteria (report §6, Phase 0).

        Exit when the agent reproduces the climatology baseline's scores
        bit-for-bit from a clean clone, and the harness rejects a deliberately
        leaked model (a canary test).
    """
    _rule("Phase 0 exit criteria")
    failures: list[str] = []

    splits.assert_disjoint()
    _p("[ok]   splits are disjoint")

    ds = _dataset(args)
    split = splits.get_split("validate")

    # --- criterion 1: bit-for-bit reproducibility of the baselines ----------
    observed = {}
    for name in ("climatology-global", "climatology-county-quarter"):
        card = scoring.score(build_model(name), ds.panel, split)
        observed[name] = _repro_fingerprint(card)
    observed["_data_version"] = ds.data_version
    observed["_contract"] = config.CONTRACT.digest()

    if args.bless:
        EXPECTED_PATH.parent.mkdir(parents=True, exist_ok=True)
        EXPECTED_PATH.write_text(json.dumps(observed, indent=2, sort_keys=True) + "\n")
        _p(f"[ok]   blessed baseline fingerprints -> "
           f"{EXPECTED_PATH.relative_to(data_mod.REPO_ROOT)}")
    elif not EXPECTED_PATH.exists():
        failures.append(
            f"no blessed baseline at {EXPECTED_PATH.relative_to(data_mod.REPO_ROOT)}; "
            "run `readiness verify --bless` once, then commit it"
        )
        _p("[FAIL] reproducibility: nothing to compare against")
    else:
        expected = json.loads(EXPECTED_PATH.read_text())
        diffs = []
        for key in sorted(set(expected) | set(observed)):
            if expected.get(key) != observed.get(key):
                diffs.append(key)
        if diffs:
            failures.append(f"baseline scores changed: {diffs}")
            _p(f"[FAIL] reproducibility: {len(diffs)} field group(s) differ")
            for key in diffs:
                _p(f"         {key}")
                _p(f"           expected {json.dumps(expected.get(key), sort_keys=True)}")
                _p(f"           observed {json.dumps(observed.get(key), sort_keys=True)}")
        else:
            _p("[ok]   climatology baselines reproduce bit-for-bit")

    # --- criterion 2: the canary rejects a leaked model ---------------------
    oracle = build_model("leaky-oracle", panel=ds.panel)
    ocard = scoring.score(oracle, ds.panel, split)
    _units, probs, outcomes = scoring.predictions_for(oracle, ds.panel, split)
    view = splits.TrainingView(splits.split_panel(ds.panel, splits.TRAIN), splits.TRAIN)
    report = canary_mod.run(
        probs=probs,
        outcomes=outcomes,
        brier_skill_score=ocard.brier_skill_score,
        auc=ocard.auc,
        view=view,
        declared_train_digest=getattr(oracle, "training_digest", None),
    )
    if report.rejected:
        tripped = [f.check for f in report.findings if f.tripped]
        _p(f"[ok]   leakage canary rejected leaky-oracle (tripped: {', '.join(tripped)})")
    else:
        failures.append("leakage canary did NOT reject leaky-oracle")
        _p("[FAIL] leakage canary accepted a leaked model")

    # --- criterion 3: the ledger has not been rewritten ---------------------
    status = Ledger(data_mod.LEDGER_PATH).verify()
    if status.valid:
        _p(f"[ok]   {status.format()}")
    else:
        failures.append("experiment ledger chain is broken")
        _p(f"[FAIL] {status.format()}")

    _p()
    if failures:
        _p(f"Phase 0 NOT met — {len(failures)} failure(s):")
        for f in failures:
            _p(f"  - {f}")
        return 1
    _p("Phase 0 exit criteria met.")
    return 0


def cmd_report(args) -> int:
    script = data_mod.REPO_ROOT / "tools" / "build_report.py"
    return subprocess.call([sys.executable, str(script)])


def cmd_mcp(args) -> int:
    from readiness.connectors.mcp_server import serve

    serve()
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="readiness",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    def data_flags(sp):
        sp.add_argument("--refresh", action="store_true",
                        help="re-download sources even if a snapshot exists")
        sp.add_argument("--keep-raw", action="store_true",
                        help="mirror the raw .csv.gz pulls (report §7 recommends this)")
        sp.add_argument("--quiet", action="store_true")
        return sp

    sp = sub.add_parser("contract", help="print the pre-registered contract")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_contract)

    sub.add_parser("models", help="list proposable models").set_defaults(func=cmd_models)

    data_flags(sub.add_parser("snapshot", help="pull and pin source data")).set_defaults(
        func=cmd_snapshot
    )
    data_flags(sub.add_parser("panel", help="build the labelled panel")).set_defaults(
        func=cmd_panel
    )

    sp = data_flags(sub.add_parser("score", help="fit and score one model"))
    sp.add_argument("model")
    sp.add_argument("--split", default="validate", choices=["train", "validate", "test"])
    sp.add_argument("--spend-test-touch", action="store_true",
                    help="required to score against the one-shot test split")
    sp.set_defaults(func=cmd_score)

    sp = sub.add_parser("loop", help="run the experimental loop")
    sp.add_argument("--backend", default="local", choices=["local", "claude"])
    sp.add_argument("--split", default="validate", choices=["train", "validate", "test"])
    sp.add_argument("--no-canary", action="store_true",
                    help="skip the leaked-model demonstration")
    sp.add_argument("--quiet", action="store_true", help="suppress data-plane chatter")
    sp.set_defaults(func=cmd_loop)

    sp = data_flags(sub.add_parser("canary", help="demonstrate leakage rejection"))
    sp.add_argument("--split", default="validate", choices=["train", "validate", "test"])
    sp.set_defaults(func=cmd_canary)

    sp = sub.add_parser("ledger", help="show and verify the experiment ledger")
    sp.add_argument("--show", action="store_true", help="print full cards as JSON")
    sp.add_argument("--id", help="only this experiment id")
    sp.set_defaults(func=cmd_ledger)

    sp = data_flags(sub.add_parser("verify", help="check the Phase 0 exit criteria"))
    sp.add_argument("--bless", action="store_true",
                    help="record current baseline scores as the reproducibility target")
    sp.set_defaults(func=cmd_verify)

    sub.add_parser("report", help="rebuild the static research report").set_defaults(
        func=cmd_report
    )
    sub.add_parser("mcp", help="run the read-only MCP data server").set_defaults(
        func=cmd_mcp
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        _p("\ninterrupted")
        return 130
    except (splits.SplitViolation, ConnectorError) as exc:
        # These are the harness and the data plane refusing to do something,
        # not crashes. A traceback would suggest the tool is broken when it is
        # in fact working exactly as designed.
        _p()
        _p(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
