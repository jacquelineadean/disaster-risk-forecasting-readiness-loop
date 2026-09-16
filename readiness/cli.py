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
    readiness features          load the feature sources, print admission and audit
    readiness score MODEL       fit and score one model on train or validate
    readiness loop              run the full experimental loop
    readiness promote MODEL     the one test touch, after a validate pass
    readiness canary            demonstrate the harness rejecting a leaked model
    readiness ledger            show and verify the experiment ledger
    readiness verify            check the Phase 0 or Phase 1 exit criteria
    readiness backtest          write the backtest report from committed files
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

from readiness import config, contracts, data as data_mod, verify
from readiness.connectors.base import ConnectorError
from readiness.contracts import Contract, ContractError
from readiness.engine import REGISTRY, build_model, describe_registry
from readiness.engine.features import FEATURE_SETS
from readiness.harness import canary as canary_mod
from readiness.harness import contract as contract_mod
from readiness.harness import features as features_mod
from readiness.harness import scoring, splits
from readiness.harness.contract import Check
from readiness.harness.ledger import Ledger

#: The Phase 0 fingerprint definition lives in `readiness.verify`; these names
#: stay for anything that still reaches it through the CLI.
_REPRO_FIELDS = verify.REPRO_FIELDS
_REPRO_MODELS = verify.REPRO_MODELS
_repro_fingerprint = verify.repro_fingerprint


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _rule(title: str) -> None:
    _p()
    _p(title)
    _p("-" * max(len(title), 60))


class UsageError(Exception):
    """A command refused on its arguments; printed without a traceback, exit 2."""


def _contract(args) -> Contract:
    return contracts.resolve(getattr(args, "contract", None))


def _features(args, contract: Contract) -> list[str]:
    """The feature connectors a command loads.

    `--features a,b` names them; left out, every connector whose pinned data
    is already present is loaded, so a command never pulls a feature source
    the user did not ask for and never silently ignores one that is there.
    """
    given = getattr(args, "features", None)
    if given is not None:
        names = [f.strip() for f in given.split(",") if f.strip()]
        unknown = sorted(set(names) - set(data_mod.FEATURE_CONNECTORS))
        if unknown:
            raise UsageError(
                f"unknown feature connector(s) {unknown}; known: "
                f"{', '.join(data_mod.FEATURE_CONNECTORS)}"
            )
        return names
    return [
        f for f in data_mod.FEATURE_CONNECTORS if data_mod.pinned(contract, features=[f])
    ]


def _dataset(args, contract: Contract, features=()) -> data_mod.Dataset:
    return data_mod.build(
        contract,
        keep_raw=getattr(args, "keep_raw", False),
        refresh=getattr(args, "refresh", False),
        progress=_p if not getattr(args, "quiet", False) else (lambda _m: None),
        features=features,
    )


_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


def _coerce(key: str, type_: str, raw: str) -> object:
    """One `--param` value in the type the registry declares for it."""
    try:
        if type_ == "float":
            return float(raw)
        if type_ == "int":
            return int(raw)
        if type_ == "bool":
            if raw.lower() in _TRUE:
                return True
            if raw.lower() in _FALSE:
                return False
            raise ValueError(raw)
        if type_ == "list":
            return [item.strip() for item in raw.split(",") if item.strip()]
        return raw
    except ValueError:
        raise UsageError(f"--param {key}: {raw!r} is not a {type_}") from None


def parse_params(model: str, pairs) -> dict:
    """`--param key=value` pairs as constructor keywords, typed by the registry.

    The registry's params schema is the one statement of a model's knobs, so
    a key it does not list is refused with the ones it does, and every value
    is converted to the declared type (lists are comma-separated strings).
    """
    if model not in REGISTRY:
        raise UsageError(f"unknown model {model!r}; known: {', '.join(REGISTRY)}")
    schema = REGISTRY[model].params
    kwargs: dict = {}
    for pair in pairs or ():
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise UsageError(f"--param expects key=value, got {pair!r}")
        if key not in schema:
            known = ", ".join(schema) or "none"
            raise UsageError(f"{model} has no parameter {key!r}; known: {known}")
        kwargs[key] = _coerce(key, schema[key]["type"], raw)
    return kwargs


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
    features = [f.strip() for f in args.features.split(",") if f.strip()]
    unknown = sorted(set(features) - set(data_mod.FEATURE_CONNECTORS))
    if unknown:
        raise UsageError(
            f"unknown feature connector(s) {unknown}; known: "
            f"{', '.join(data_mod.FEATURE_CONNECTORS)}"
        )
    ds = _dataset(args, c, features)
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


def cmd_features(args) -> int:
    """Load the feature sources and show what the firewall makes of them.

    Every loaded source is put through `admit()`; a refusal (FEMA NRI under
    every current contract) is printed as a finding and is not an error —
    the refusal is the point. Then every feature set the loaded sources can
    serve is built over the validate units and audited, exactly as a scoring
    run would do before fitting.
    """
    c = _contract(args)
    features = _features(args, c)
    _rule(f"feature sources  ({c.name}: {', '.join(features) or 'none'})")
    if not features:
        _p("no feature connector has pinned data for this contract; pass --features "
           f"({', '.join(data_mod.FEATURE_CONNECTORS)}) to pull some")
    ds = _dataset(args, c, features)
    _p()
    _p("admission  (static layers must predate the first validate year, "
       f"{c.validate_years[0]}; nothing built from the ground truth)")
    for name in sorted(ds.sources):
        src = ds.sources[name]
        try:
            features_mod.admit(src, c)
        except features_mod.FeatureAdmissionError as exc:
            _p(f"  [REFUSED] {name:<12} {exc}")
        else:
            when = (
                "series" if src.kind == "series"
                else f"static through {src.derived_through}"
                if src.derived_through is not None
                else "static, timeless geometry"
            )
            _p(f"  [admitted] {name:<11} {when}  (pins: {', '.join(src.manifest_keys)})")
    if ds.feature_version:
        _p(f"  feature version sha256:{ds.feature_version}")

    served = {
        name: specs
        for name, specs in FEATURE_SETS.items()
        if all(spec.source in ds.sources for spec in specs)
    }
    _p()
    _p("feature sets the loaded sources can serve")
    if not served:
        _p("  none")
    for name, specs in served.items():
        _p(f"  {name}")
        for spec in specs:
            how = (
                "static" if spec.is_static
                else f"{spec.transform} over {spec.window_months} month(s), "
                f"lag {spec.lag_months}"
            )
            _p(f"    {spec.column:<18} {spec.source}.{spec.variable}  {how}")
    if not served:
        return 0

    specs = tuple(spec for group in served.values() for spec in group)
    units = list(splits.split_panel(ds.panel, c.splits.validate).units)
    frame = features_mod.build_frame(specs, ds.sources, units, c.periods_per_year)
    audit = features_mod.audit_frame(specs, ds.sources, units, c, frame)
    _p()
    _p(f"audit over {len(units):,} validate units ({c.splits.validate})")
    _p(audit.format())
    return 0


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def _print_screen(card, report, contract: Contract) -> int:
    """The scorecard, the verdict and the canary; exit 1 only on a rejection."""
    _p(card.format())
    _p()
    _p(contract_mod.evaluate(card, contract).format())
    _p()
    _p(report.format())
    # Exit non-zero only when the canary rejects: a contract failure is a
    # legitimate experimental outcome, not a tool error.
    return 1 if report.rejected else 0


def cmd_score(args) -> int:
    c = _contract(args)
    kwargs = parse_params(args.model, args.param)
    split = splits.get_split(c, args.split)
    if split.name == "test":
        # The touch and the card are one atomic step: a test score with no
        # ledger card behind it is a spent budget with no record of what it
        # bought. `promote` is the only way onto the test split.
        _p()
        _p(
            f"refusing to score against the test split: a test touch without a "
            f"ledger card is a spent budget with no record.\nThe test split "
            f"({split.years[0]}-{split.years[-1]}) is touched only through\n"
            f"    readiness promote {args.model} -c {c.name} --spend-test-touch\n"
            "which refuses without a prior validate pass for the same model, "
            "version and arguments, and writes the card in the same step."
        )
        return 2
    ds = _dataset(args, c, _features(args, c))
    model = build_model(args.model, canary_panel=ds.panel, **kwargs)
    _rule(f"score  {model.name}@{model.version}  on {split}  ({c.name})")
    if kwargs:
        _p(f"  arguments        {json.dumps(kwargs, sort_keys=True)}")
    card, report = scoring.screen(model, ds.panel, c, split, sources=ds.sources)
    return _print_screen(card, report, c)


def cmd_loop(args) -> int:
    from readiness.agent import orchestrator

    c = _contract(args)
    # `--quiet` silences the loop's running commentary (and the data plane's
    # progress underneath it); the result and the ledger path still print.
    progress = (lambda _m: None) if args.quiet else _p
    if args.backend == "claude":
        orchestrator.run_claude(c, split_name=args.split, progress=progress)
        return 0

    features = _features(args, c)
    _rule(
        f"experimental loop  ({c.name}, {args.backend} backend, split={args.split}, "
        f"queue={args.queue})"
    )
    result = orchestrator.run_local(
        c,
        split_name=args.split,
        queue=orchestrator.QUEUES[args.queue],
        include_canary=not args.no_canary,
        features=features,
        promote=args.promote,
        progress=progress,
    )
    _p()
    _p(result.format())
    _p()
    where = data_mod.paths(c)
    _p(f"ledger: {where.relative(where.ledger)}")
    return 0


def cmd_promote(args) -> int:
    """The one test touch, refused unless it is earned and unless it is asked for."""
    from readiness.agent import orchestrator

    c = _contract(args)
    kwargs = parse_params(args.model, args.param)
    if not args.spend_test_touch:
        _p()
        _p(
            f"refusing to promote {args.model} without --spend-test-touch.\n"
            f"This is the {c.test_touch_budget}-shot holdout "
            f"({c.test_years[0]}-{c.test_years[-1]}); promoting spends the touch and "
            "writes the test card in one step, and once spent for a model version "
            "it cannot be spent again."
        )
        return 2
    ds = _dataset(args, c, _features(args, c))
    _rule(f"promote  {args.model}  to test  ({c.name})")
    progress = (lambda _m: None) if args.quiet else _p
    try:
        card = orchestrator.promote(c, args.model, kwargs, ds, progress=progress)
    except orchestrator.PromotionRefused as exc:
        _p()
        _p(str(exc))
        return 2
    _p()
    _p(f"  card             {card.experiment_id}  ({card.status})  {card.card_hash}")
    report = canary_mod.CanaryReport(**_canary_dict(card))
    code = _print_screen(verify.stored_scorecard(card), report, c)
    _p()
    _p(f"next: readiness backtest -c {c.name} && readiness verify -c {c.name} --phase 1")
    return code


def _canary_dict(card) -> dict:
    canary = card.canary or {"rejected": False, "findings": []}
    findings = tuple(canary_mod.CanaryFinding(**f) for f in canary["findings"])
    return {"rejected": canary["rejected"], "findings": findings}


def cmd_canary(args) -> int:
    c = _contract(args)
    ds = _dataset(args, c)
    split = splits.get_split(c, args.split)
    _rule(f"leakage canary  (target: leaky-oracle, split={split}, {c.name})")
    _p("A model with direct access to the outcomes it is scored on. The Phase 0")
    _p("exit criterion is that the harness rejects it.")
    _p()

    model = build_model("leaky-oracle", canary_panel=ds.panel)
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
    if args.id or args.show:
        # Asking for one card by id only makes sense as a request to see it,
        # so --id implies --show rather than silently printing the table.
        return _show_cards(ledger, args.id)
    _p(ledger.summary())
    return 0 if ledger.verify().valid else 1


def _show_cards(ledger: Ledger, experiment_id: str | None) -> int:
    """Print full cards as JSON — every card, or just the one with this id."""
    shown = 0
    for card in ledger.read():
        if experiment_id and card.experiment_id != experiment_id:
            continue
        _p(json.dumps(card.record(), indent=2))
        _p()
        shown += 1
    if experiment_id and not shown:
        _p(f"no experiment {experiment_id!r} in this ledger")
        return 1
    return 0


def _print_check(check: Check) -> None:
    """One `[ok]`/`[FAIL]` line; any further detail lines sit indented under it."""
    first, *rest = check.detail.splitlines()
    _p(f"{'[ok]  ' if check.passed else '[FAIL]'} {first}")
    for line in rest:
        _p(f"         {line}")


def cmd_verify(args) -> int:
    """Check one contract's exit criteria: Phase 0 (report §6) or Phase 1 (plan §2).

    Phase 0 scores the baselines against the pinned data and the blessed
    fingerprints. Phase 1 reads the ledger, the touch file and the backtest
    report and builds nothing, unless `--replay` asks for the promoted model
    to be rebuilt from its card and rescored on test, which spends no touch.
    The checks are `readiness.verify`'s; this prints them.
    """
    c = _contract(args)
    where = data_mod.paths(c)
    if args.phase == 1:
        return _verify_phase1(args, c, where)
    _rule(f"Phase 0 exit criteria  ({c.name})")
    ds = _dataset(args, c)
    result = verify.phase0(
        c,
        ds,
        ledger_path=where.ledger,
        expected_path=where.expected,
        bless=args.bless,
    )
    return _print_phase(0, c, result.checks)


def _print_phase(phase: int, contract: Contract, checks) -> int:
    for check in checks:
        _print_check(check)
    _p()
    failures = [c.detail.splitlines()[0] for c in checks if not c.passed]
    if failures:
        _p(f"Phase {phase} NOT met for {contract.name} — {len(failures)} failure(s):")
        for f in failures:
            _p(f"  - {f}")
        return 1
    _p(f"Phase {phase} exit criteria met for {contract.name}.")
    return 0


def _verify_phase1(args, c: Contract, where: data_mod.Paths) -> int:
    _rule(f"Phase 1 exit criteria  ({c.name})")
    result = verify.phase1(
        c,
        ledger_path=where.ledger,
        touch_path=where.touch_budget,
        backtest_path=where.directory / "backtest.html",
    )
    checks = list(result.checks)
    if args.replay and result.card is not None:
        ds = _dataset(args, c, _features(args, c))
        for field, expected, observed, ok in verify.replay(c, ds, result.card):
            detail = f"replay {field}: card {verify.sig(expected)}"
            if not ok:
                detail += f", refit {verify.sig(observed)}  <- differs"
            checks.append(Check(f"replay {field}", ok, detail))
    elif args.replay:
        checks.append(Check("replay", False, "replay: no test card to replay"))
    return _print_phase(1, c, checks)


def cmd_backtest(args) -> int:
    from readiness import backtest

    c = _contract(args)
    out = pathlib.Path(args.output) if args.output else None
    path = backtest.write(c, out)
    _p(f"wrote {data_mod.relative(path)}")
    _p(f"wrote {data_mod.relative(path.with_suffix('.json'))}")
    return 0


def cmd_dashboard(args) -> int:
    from readiness import dashboard

    if args.all and (args.contract or args.output):
        # --all renders every contract to its own directory; a contract or an
        # output path would be ignored, and an ignored option is a lie.
        _p("dashboard: --all cannot be combined with -c/--contract or -o/--output")
        return 2
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

    def features_flag(sp):
        sp.add_argument(
            "--features", metavar="A,B",
            help="feature connectors to load, comma-separated "
                 f"({','.join(data_mod.FEATURE_CONNECTORS)}); default: every one "
                 "whose pinned data is present",
        )
        return sp

    def param_flag(sp):
        sp.add_argument(
            "--param", action="append", metavar="KEY=VALUE",
            help="constructor argument, typed by `readiness models`; repeatable; "
                 "lists are comma-separated",
        )
        return sp

    sub.add_parser("contracts", help="list the registered contracts").set_defaults(
        func=cmd_contracts
    )

    sp = contract_flag(sub.add_parser("contract", help="print one contract and its hash"))
    sp.add_argument("--json", action="store_true", help="also print the JSON spec")
    sp.set_defaults(func=cmd_contract)

    d = contracts.DEFAULTS
    sp = sub.add_parser(
        "register",
        help="pre-register a new contract from options",
        description=(
            "Write contracts/NAME.json. Everything not given takes the documented "
            f"default ({d['period']}ly, ${d['property_usd_min']:,.0f} property damage"
            f"{' or any casualty' if d['count_casualties'] else ''}, "
            f"{d['train']} / {d['validate']} / {d['test']}, "
            f"BSS > {d['min_brier_skill_score']:g}, "
            f"+/-{d['reliability_tolerance_pp'] * 100:g}pp, AUC >= {d['min_auc']:.2f})."
        ),
    )
    sp.add_argument("name", help="lowercase letters, digits and hyphens")
    sp.add_argument("--hazard", required=True,
                    help="a catalogue hazard (see `readiness hazards`) or a new name")
    sp.add_argument("--state", action="append", metavar="XX",
                    help="two-letter state; repeatable; omit for the whole country")
    sp.add_argument("--period", default=d["period"], choices=sorted(contracts.PERIODS))
    sp.add_argument("--event-type", action="append", metavar="TYPE",
                    help="Storm Events EVENT_TYPE; repeatable; required for an "
                         "uncatalogued hazard")
    sp.add_argument("--damage-usd", type=float, default=d["property_usd_min"],
                    help="property damage at or above which an event is damaging "
                         f"(default {d['property_usd_min']:,.0f})")
    sp.add_argument("--no-casualties", action="store_true",
                    help="do not count injuries or deaths as damaging")
    sp.add_argument("--zone-policy", default=d["zone_policy"],
                    choices=list(contracts.ZONE_POLICIES),
                    help="what to do with zone-coded events: drop them, or expand each "
                         f"to every county in its NWS zone (default: {d['zone_policy']})")
    sp.add_argument("--train", default=d["train"], metavar="YYYY-YYYY",
                    help=f"default {d['train']}")
    sp.add_argument("--validate", default=d["validate"], metavar="YYYY-YYYY",
                    help=f"default {d['validate']}")
    sp.add_argument("--test", default=d["test"], metavar="YYYY-YYYY",
                    help=f"default {d['test']}")
    sp.add_argument("--min-bss", type=float, default=d["min_brier_skill_score"],
                    help=f"default {d['min_brier_skill_score']}")
    sp.add_argument("--tolerance", type=float, default=d["reliability_tolerance_pp"],
                    help="reliability tolerance in probability units, "
                         f"default {d['reliability_tolerance_pp']}")
    sp.add_argument("--min-bin-count", type=int, default=d["reliability_min_bin_count"],
                    help=f"default {d['reliability_min_bin_count']}")
    sp.add_argument("--min-auc", type=float, default=d["min_auc"],
                    help=f"default {d['min_auc']:.2f}")
    sp.add_argument("--bins", type=int, default=d["n_reliability_bins"],
                    help=f"reliability bins, default {d['n_reliability_bins']}")
    sp.add_argument("--description", default="")
    sp.add_argument("--version", default=d["version"])
    sp.add_argument("--force", action="store_true", help="overwrite an existing file")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the JSON, write nothing")
    sp.set_defaults(func=cmd_register)

    sub.add_parser("hazards", help="list the hazard catalogue").set_defaults(
        func=cmd_hazards
    )
    sub.add_parser("models", help="list proposable models").set_defaults(func=cmd_models)

    sp = data_flags(sub.add_parser("snapshot", help="pull and pin source data"))
    sp.add_argument(
        "--features", metavar="A,B", default="",
        help="feature connectors to pull as well, comma-separated "
             f"({','.join(data_mod.FEATURE_CONNECTORS)}); default: none",
    )
    sp.set_defaults(func=cmd_snapshot)
    data_flags(sub.add_parser("panel", help="build the labelled panel")).set_defaults(
        func=cmd_panel
    )
    sp = features_flag(data_flags(sub.add_parser(
        "features",
        help="load the feature sources; print admission verdicts and the audit",
    )))
    sp.set_defaults(func=cmd_features)

    sp = param_flag(features_flag(data_flags(
        sub.add_parser("score", help="fit and score one model on train or validate")
    )))
    sp.add_argument("model")
    sp.add_argument("--split", default="validate", choices=["train", "validate", "test"],
                    help="test is refused: use `readiness promote`")
    sp.set_defaults(func=cmd_score)

    sp = features_flag(
        contract_flag(sub.add_parser("loop", help="run the experimental loop"))
    )
    sp.add_argument("--backend", default="local", choices=["local", "claude"])
    sp.add_argument("--split", default="validate", choices=["train", "validate"])
    sp.add_argument("--queue", default="baseline", choices=["baseline", "phase1"],
                    help="phase1 runs the baselines and then the Phase 1 candidates")
    sp.add_argument("--promote", action="store_true",
                    help="after the queue, spend the test touch on the first validate "
                         "pass in queue order")
    sp.add_argument("--no-canary", action="store_true",
                    help="skip the leaked-model demonstration")
    sp.add_argument("--quiet", action="store_true", help="suppress data-plane chatter")
    sp.set_defaults(func=cmd_loop)

    sp = param_flag(features_flag(data_flags(sub.add_parser(
        "promote", help="spend the one test touch on a model that passed on validate"
    ))))
    sp.add_argument("model")
    sp.add_argument("--spend-test-touch", action="store_true",
                    help="required: the touch and the test card are one step")
    sp.set_defaults(func=cmd_promote)

    sp = data_flags(sub.add_parser("canary", help="demonstrate leakage rejection"))
    sp.add_argument("--split", default="validate", choices=["train", "validate", "test"])
    sp.set_defaults(func=cmd_canary)

    sp = contract_flag(
        sub.add_parser("ledger", help="show and verify the experiment ledger")
    )
    sp.add_argument("--show", action="store_true", help="print full cards as JSON")
    sp.add_argument("--id", help="only this experiment id, as JSON (implies --show)")
    sp.set_defaults(func=cmd_ledger)

    sp = features_flag(data_flags(
        sub.add_parser("verify", help="check the Phase 0 or Phase 1 exit criteria")
    ))
    sp.add_argument("--phase", type=int, default=0, choices=[0, 1],
                    help="0 scores the baselines against the pinned data; 1 reads the "
                         "ledger only (default 0)")
    sp.add_argument("--replay", action="store_true",
                    help="with --phase 1: rebuild the promoted model from its card and "
                         "rescore it on test without spending a touch")
    sp.add_argument("--bless", action="store_true",
                    help="record current baseline scores as the reproducibility target")
    sp.set_defaults(func=cmd_verify)

    sp = contract_flag(sub.add_parser(
        "backtest", help="write the backtest report from committed files only"
    ))
    sp.add_argument("-o", "--output",
                    help="write the page here instead of "
                         "experiments/<name>/backtest.html")
    sp.set_defaults(func=cmd_backtest)

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
    except (
        splits.SplitViolation,
        ConnectorError,
        ContractError,
        features_mod.FeatureAdmissionError,
        UsageError,
    ) as exc:
        # These are the harness, the data plane, the firewall and the registry
        # refusing to do something, not crashes. A traceback would suggest the
        # tool is broken when it is in fact working exactly as designed.
        _p()
        _p(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
