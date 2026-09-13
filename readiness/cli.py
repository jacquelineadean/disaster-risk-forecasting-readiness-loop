"""`readiness` — the command line for the loop.

Every command that touches data or scores runs against one registered
contract, chosen with `-c/--contract NAME` (a path to a contract JSON also
works). If the flag is omitted, `READINESS_CONTRACT` is consulted, then the
sole registered contract if there is exactly one; otherwise the command
refuses and lists the choices.

    readiness contracts         list the registered contracts
    readiness contract          print one contract and its hash
    readiness register NAME     pre-register a new contract from options
    readiness hazards           list the hazard catalogue
    readiness models            list proposable models
    readiness snapshot          pull and pin the data, print the manifest
    readiness panel             build the labelled panel, print split coverage
    readiness score MODEL       fit and score one model
    readiness loop              run the full experimental loop
    readiness canary            demonstrate the harness rejecting a leaked model
    readiness ledger            show and verify the experiment ledger
    readiness verify            check the Phase 0 exit criteria
    readiness dashboard         render a contract's ledger as a static HTML page
    readiness report            rebuild the static research report
    readiness mcp               run the read-only MCP data server on stdio
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

from readiness import config, contracts, data as data_mod
from readiness.connectors.base import ConnectorError
from readiness.contracts import Contract, ContractError
from readiness.engine import build_model, describe_registry, needs_panel
from readiness.harness import contract as contract_mod
from readiness.harness import scoring, splits
from readiness.harness.ledger import Ledger

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

#: The baselines whose scores must reproduce bit-for-bit from a clean clone.
_REPRO_MODELS = ("climatology-pooled", "climatology-seasonal")


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _rule(title: str) -> None:
    _p()
    _p(title)
    _p("-" * max(len(title), 60))


def _contract(args) -> Contract:
    return contracts.resolve(getattr(args, "contract", None))


def _dataset(args, contract: Contract) -> data_mod.Dataset:
    return data_mod.build(
        contract,
        keep_raw=getattr(args, "keep_raw", False),
        refresh=getattr(args, "refresh", False),
        progress=_p if not getattr(args, "quiet", False) else (lambda _m: None),
    )


# ---------------------------------------------------------------------------
# contracts
# ---------------------------------------------------------------------------


def cmd_contracts(args) -> int:
    _rule(f"registered contracts  ({data_mod.relative(contracts.contracts_dir())})")
    _p(contracts.describe_registry())
    return 0


def cmd_contract(args) -> int:
    c = _contract(args)
    _rule("pre-registered contract")
    _p(c.describe())
    if args.json:
        _p()
        _p(json.dumps(c.to_spec(), indent=2))
    return 0


def cmd_register(args) -> int:
    c = contracts.new(
        args.name,
        hazard=args.hazard,
        states=args.state or (),
        period=args.period,
        event_types=args.event_type,
        property_usd_min=args.damage_usd,
        count_casualties=not args.no_casualties,
        zone_policy=args.zone_policy,
        train=args.train,
        validate=args.validate,
        test=args.test,
        description=args.description or "",
        version=args.version,
        min_brier_skill_score=args.min_bss,
        reliability_tolerance_pp=args.tolerance,
        reliability_min_bin_count=args.min_bin_count,
        min_auc=args.min_auc,
        n_reliability_bins=args.bins,
    )
    if args.dry_run:
        _p(json.dumps(c.to_spec(), indent=2))
        return 0
    path = c.save(contracts.contracts_dir(), force=args.force)
    _rule(f"registered  {data_mod.relative(path)}")
    _p(c.describe())
    hazard = config.HAZARDS.get(c.hazard)
    if hazard is not None and hazard.coding != "county" and c.zone_policy == "drop":
        _p()
        _p(
            f"note: {c.hazard} is {hazard.coding}-coded in Storm Events and this "
            "contract drops zone-coded events, so the panel will under-count it. "
            "Consider registering with --zone-policy expand, which maps each zone "
            "event to every county in its zone via the NWS crosswalk "
            "(docs/contracts.md). `readiness panel` reports the counts either way."
        )
    _p()
    _p("next:")
    _p(f"  readiness panel -c {c.name}     # pull the data and inspect the panel")
    _p(f"  readiness loop  -c {c.name}     # run the baselines and write the ledger")
    return 0


def cmd_hazards(args) -> int:
    _rule("hazard catalogue  (name, Storm Events coding, event types)")
    _p(config.describe_hazards())
    _p()
    _p("A contract may also name a hazard outside the catalogue by listing its")
    _p("event types explicitly (`readiness register ... --event-type ...`).")
    return 0


def cmd_models(args) -> int:
    _rule("proposable models")
    _p(describe_registry())
    return 0


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


def cmd_snapshot(args) -> int:
    c = _contract(args)
    _rule(f"data snapshot  ({c.name}: {c.scope_label})")
    ds = _dataset(args, c)
    _p()
    _p(ds.manifest.summary())
    return 0


def cmd_panel(args) -> int:
    c = _contract(args)
    _rule(f"region-{c.period} panel  ({c.name})")
    ds = _dataset(args, c)
    _p()
    _p(f"  {ds.panel.summary()}")
    _p(f"  regions: {len(ds.regions)}  ({ds.regions[0]} ... {ds.regions[-1]})")
    _p()
    _p("split coverage")
    _p(splits.coverage_report(ds.panel, c.splits))
    _p()
    _p("event coverage")
    _p(ds.diagnostics.format())
    return 0


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def cmd_score(args) -> int:
    c = _contract(args)
    ds = _dataset(args, c)
    split = splits.get_split(c, args.split)
    model = build_model(args.model, panel=ds.panel if needs_panel(args.model) else None)
    where = data_mod.paths(c)

    if split.name == "test":
        budget = splits.TouchBudget(where.touch_budget, c.test_touch_budget)
        budget.check(model.name, model.version)
        if not args.spend_test_touch:
            _p()
            _p(
                f"refusing to score against the test split without "
                f"--spend-test-touch.\nThis is the {c.test_touch_budget}-shot "
                f"holdout ({split.years[0]}-{split.years[-1]}); once spent for "
                f"{model.name}@{model.version} it cannot be spent again."
            )
            return 2

    _rule(f"score  {model.name}@{model.version}  on {split}  ({c.name})")
    card, report = scoring.screen(model, ds.panel, c, split)
    _p(card.format())
    _p()
    _p(contract_mod.evaluate(card, c).format())
    _p()
    _p(report.format())

    if split.name == "test" and args.spend_test_touch:
        spent = splits.TouchBudget(where.touch_budget, c.test_touch_budget).spend(
            model.name, model.version
        )
        _p()
        _p(f"test touch {spent}/{c.test_touch_budget} spent for "
           f"{model.name}@{model.version}")
    # Exit non-zero only when the canary rejects: a contract failure is a
    # legitimate experimental outcome, not a tool error.
    return 1 if report.rejected else 0


def cmd_loop(args) -> int:
    from readiness.agent import orchestrator

    c = _contract(args)
    if args.backend == "claude":
        orchestrator.run_claude(c, split_name=args.split, progress=_p)
        return 0

    _rule(f"experimental loop  ({c.name}, {args.backend} backend, split={args.split})")
    result = orchestrator.run_local(
        c,
        split_name=args.split,
        include_canary=not args.no_canary,
        progress=_p,
    )
    _p()
    _p(result.format())
    _p()
    where = data_mod.paths(c)
    _p(f"ledger: {where.relative(where.ledger)}")
    return 0


def cmd_canary(args) -> int:
    c = _contract(args)
    ds = _dataset(args, c)
    split = splits.get_split(c, args.split)
    _rule(f"leakage canary  (target: leaky-oracle, split={split}, {c.name})")
    _p("A model with direct access to the outcomes it is scored on. The Phase 0")
    _p("exit criterion is that the harness rejects it.")
    _p()

    model = build_model("leaky-oracle", panel=ds.panel)
    card, report = scoring.screen(model, ds.panel, c, split)
    _p(card.format())
    _p()
    _p("contract, if it were honoured:")
    _p(contract_mod.evaluate(card, c).format())
    _p()
    _p(report.format())
    _p()
    if report.rejected:
        _p("PASS: the harness rejected a leaked model.")
        return 0
    _p("FAIL: the harness accepted a leaked model. Phase 0 does not exit.")
    return 1


def cmd_ledger(args) -> int:
    c = _contract(args)
    where = data_mod.paths(c)
    ledger = Ledger(where.ledger)
    _rule(f"experiment ledger  ({c.name}: {where.relative(where.ledger)})")
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
    """Check the Phase 0 exit criteria (report §6, Phase 0) for one contract.

        Exit when the agent reproduces the climatology baseline's scores
        bit-for-bit from a clean clone, and the harness rejects a deliberately
        leaked model (a canary test).
    """
    c = _contract(args)
    where = data_mod.paths(c)
    _rule(f"Phase 0 exit criteria  ({c.name})")
    failures: list[str] = []

    _p(f"[ok]   contract {c.name} validates (sha256:{c.digest()}); splits are disjoint")

    ds = _dataset(args, c)
    split = c.splits.validate

    # --- criterion 1: bit-for-bit reproducibility of the baselines ----------
    observed = {}
    for name in _REPRO_MODELS:
        card = scoring.score(build_model(name), ds.panel, c, split)
        observed[name] = _repro_fingerprint(card)
    observed["_data_version"] = ds.data_version
    observed["_contract"] = c.digest()

    expected_path = where.expected
    if args.bless:
        expected_path.parent.mkdir(parents=True, exist_ok=True)
        expected_path.write_text(json.dumps(observed, indent=2, sort_keys=True) + "\n")
        _p(f"[ok]   blessed baseline fingerprints -> {where.relative(expected_path)}")
    elif not expected_path.exists():
        failures.append(
            f"no blessed baseline at {where.relative(expected_path)}; "
            f"run `readiness verify -c {c.name} --bless` once, then commit it"
        )
        _p("[FAIL] reproducibility: nothing to compare against")
    else:
        expected = json.loads(expected_path.read_text())
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
    _ocard, report = scoring.screen(oracle, ds.panel, c, split)
    if report.rejected:
        tripped = [f.check for f in report.findings if f.tripped]
        _p(f"[ok]   leakage canary rejected leaky-oracle (tripped: {', '.join(tripped)})")
    else:
        failures.append("leakage canary did NOT reject leaky-oracle")
        _p("[FAIL] leakage canary accepted a leaked model")

    # --- criterion 3: the ledger has not been rewritten ---------------------
    status = Ledger(where.ledger).verify()
    if status.valid:
        _p(f"[ok]   {status.format()}")
    else:
        failures.append("experiment ledger chain is broken")
        _p(f"[FAIL] {status.format()}")

    _p()
    if failures:
        _p(f"Phase 0 NOT met for {c.name} — {len(failures)} failure(s):")
        for f in failures:
            _p(f"  - {f}")
        return 1
    _p(f"Phase 0 exit criteria met for {c.name}.")
    return 0


def cmd_dashboard(args) -> int:
    from readiness import dashboard

    if args.all:
        written = dashboard.write_all()
    else:
        c = _contract(args)
        out = pathlib.Path(args.output) if args.output else None
        written = [dashboard.write(c, out)]
    for path in written:
        _p(f"wrote {data_mod.relative(path)}")
    return 0


def cmd_report(args) -> int:
    script = data_mod.REPO_ROOT / "tools" / "build_report.py"
    return subprocess.call([sys.executable, str(script)])


def cmd_mcp(args) -> int:
    from readiness.connectors.mcp_server import serve

    serve(contract=_contract(args))
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="readiness",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    def contract_flag(sp):
        sp.add_argument(
            "-c", "--contract",
            help=(
                "registered contract name, or a path to a contract JSON "
                f"(default: ${contracts.CONTRACT_ENV}, else the sole registered contract)"
            ),
        )
        return sp

    def data_flags(sp):
        contract_flag(sp)
        sp.add_argument("--refresh", action="store_true",
                        help="re-download sources even if a snapshot exists")
        sp.add_argument("--keep-raw", action="store_true",
                        help="mirror the raw .csv.gz pulls (report §7 recommends this)")
        sp.add_argument("--quiet", action="store_true")
        return sp

    sub.add_parser("contracts", help="list the registered contracts").set_defaults(
        func=cmd_contracts
    )

    sp = contract_flag(sub.add_parser("contract", help="print one contract and its hash"))
    sp.add_argument("--json", action="store_true", help="also print the JSON spec")
    sp.set_defaults(func=cmd_contract)

    sp = sub.add_parser(
        "register",
        help="pre-register a new contract from options",
        description=(
            "Write contracts/NAME.json. Everything not given takes the documented "
            "default (quarterly, $10,000 property damage or any casualty, "
            "1996-2015 / 2016-2020 / 2021-2025, BSS > 0, +/-5pp, AUC >= 0.70)."
        ),
    )
    sp.add_argument("name", help="lowercase letters, digits and hyphens")
    sp.add_argument("--hazard", required=True,
                    help="a catalogue hazard (see `readiness hazards`) or a new name")
    sp.add_argument("--state", action="append", metavar="XX",
                    help="two-letter state; repeatable; omit for the whole country")
    sp.add_argument("--period", default="quarter", choices=sorted(contracts.PERIODS))
    sp.add_argument("--event-type", action="append", metavar="TYPE",
                    help="Storm Events EVENT_TYPE; repeatable; required for an "
                         "uncatalogued hazard")
    sp.add_argument("--damage-usd", type=float, default=10_000.0,
                    help="property damage at or above which an event is damaging")
    sp.add_argument("--no-casualties", action="store_true",
                    help="do not count injuries or deaths as damaging")
    sp.add_argument("--zone-policy", default="drop",
                    choices=list(contracts.ZONE_POLICIES),
                    help="what to do with zone-coded events: drop them, or expand each "
                         "to every county in its NWS zone (default: drop)")
    sp.add_argument("--train", default="1996-2015", metavar="YYYY-YYYY")
    sp.add_argument("--validate", default="2016-2020", metavar="YYYY-YYYY")
    sp.add_argument("--test", default="2021-2025", metavar="YYYY-YYYY")
    sp.add_argument("--min-bss", type=float, default=None, help="default 0.0")
    sp.add_argument("--tolerance", type=float, default=None,
                    help="reliability tolerance in probability units, default 0.05")
    sp.add_argument("--min-bin-count", type=int, default=None, help="default 30")
    sp.add_argument("--min-auc", type=float, default=None, help="default 0.70")
    sp.add_argument("--bins", type=int, default=None,
                    help="reliability bins, default 10")
    sp.add_argument("--description", default="")
    sp.add_argument("--version", default="1.0.0")
    sp.add_argument("--force", action="store_true", help="overwrite an existing file")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the JSON, write nothing")
    sp.set_defaults(func=cmd_register)

    sub.add_parser("hazards", help="list the hazard catalogue").set_defaults(
        func=cmd_hazards
    )
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

    sp = contract_flag(sub.add_parser("loop", help="run the experimental loop"))
    sp.add_argument("--backend", default="local", choices=["local", "claude"])
    sp.add_argument("--split", default="validate", choices=["train", "validate", "test"])
    sp.add_argument("--no-canary", action="store_true",
                    help="skip the leaked-model demonstration")
    sp.add_argument("--quiet", action="store_true", help="suppress data-plane chatter")
    sp.set_defaults(func=cmd_loop)

    sp = data_flags(sub.add_parser("canary", help="demonstrate leakage rejection"))
    sp.add_argument("--split", default="validate", choices=["train", "validate", "test"])
    sp.set_defaults(func=cmd_canary)

    sp = contract_flag(
        sub.add_parser("ledger", help="show and verify the experiment ledger")
    )
    sp.add_argument("--show", action="store_true", help="print full cards as JSON")
    sp.add_argument("--id", help="only this experiment id")
    sp.set_defaults(func=cmd_ledger)

    sp = data_flags(sub.add_parser("verify", help="check the Phase 0 exit criteria"))
    sp.add_argument("--bless", action="store_true",
                    help="record current baseline scores as the reproducibility target")
    sp.set_defaults(func=cmd_verify)

    sp = contract_flag(
        sub.add_parser(
            "dashboard", help="render a contract's ledger as a static HTML page"
        )
    )
    sp.add_argument("--all", action="store_true",
                    help="every registered contract, plus experiments/index.html")
    sp.add_argument("-o", "--output",
                    help="write the page here instead of the ledger's directory")
    sp.set_defaults(func=cmd_dashboard)

    sub.add_parser("report", help="rebuild the static research report").set_defaults(
        func=cmd_report
    )
    contract_flag(
        sub.add_parser("mcp", help="run the read-only MCP data server")
    ).set_defaults(func=cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        _p("\ninterrupted")
        return 130
    except (splits.SplitViolation, ConnectorError, ContractError) as exc:
        # These are the harness, the data plane and the registry refusing to
        # do something, not crashes. A traceback would suggest the tool is
        # broken when it is in fact working exactly as designed.
        _p()
        _p(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
