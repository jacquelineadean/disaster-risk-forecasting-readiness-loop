"""`readiness` — the command line for the loop.

Every command that touches data or scores runs against one registered
contract, chosen with `-c/--contract NAME` (a path to a contract JSON also
works). If the flag is omitted, `READINESS_CONTRACT` is consulted, then the
sole registered contract if there is exactly one; otherwise the command
refuses and lists the choices.

    readiness contracts         list the registered contracts (--names: one per line)
    readiness contract          print one contract and its hash
    readiness register NAME     pre-register a new contract from options
    readiness hazards           list the hazard catalogue
    readiness models            list proposable models
    readiness snapshot          pull and pin the data, print the manifest
    readiness panel             build the labelled panel, print split coverage
    readiness features          load the feature sources, print admission and audit
    readiness score MODEL       fit and score one model on train or validate
    readiness loop              run the full experimental loop
    readiness fleet             run the loop over many contracts in turn, or --status
    readiness promote MODEL     the one test touch, after a validate pass
    readiness canary            demonstrate the harness rejecting a leaked model
    readiness ledger            show and verify the experiment ledger
    readiness exposure          pull, show and spot-check the USA Structures counts
    readiness issue MODEL       refit the promoted model and write one period's file
    readiness brief             one cited, validated brief per county
    readiness scenarios         list the scenario library, or run the case studies
    readiness gap-report        one cited, validated gap report for a facility
    readiness review            record a rating of a blinded gap report
    readiness verify            check the Phase 0, 1, 2 or 3 exit criteria
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
from readiness.connectors import usa_structures
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
from readiness.plans import gap_report as plans_gap_report
from readiness.plans import reviews as plans_reviews
from readiness.plans import scenarios as plans_scenarios

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
    if args.names:
        # One name per line and nothing else, so a shell loop can read it:
        #     for c in $(readiness contracts --names); do ...; done
        for name in sorted(_registry(args)):
            _p(name)
        return 0
    _rule(f"registered contracts  ({data_mod.relative(contracts.contracts_dir())})")
    _p(contracts.describe_registry())
    return 0


def _registry(args) -> dict[str, Contract]:
    """The registered contracts a fleet command runs over, after its filters.

    `--national` keeps the contracts whose scope is the whole country;
    `--contracts a,b` keeps the named ones and refuses an unknown name rather
    than running the rest, because a fleet that silently dropped a contract
    would report a status for a set nobody asked for.
    """
    from readiness import fleet

    known = contracts.registered()
    if getattr(args, "national", False):
        return fleet.national(known)
    named = getattr(args, "contracts", None)
    if named:
        wanted = [n.strip() for n in named.split(",") if n.strip()]
        unknown = sorted(set(wanted) - set(known))
        if unknown:
            raise UsageError(
                f"no registered contract named {unknown}; registered: {sorted(known)}"
            )
        return {n: known[n] for n in wanted}
    return known


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
    # A refused promotion is a verdict, not a crash: the queue's cards are
    # written and worth printing, and the reason the test split was not
    # touched is the thing the operator needs to read. Exit 2, like every
    # other refusal the CLI reports.
    refusal: Exception | None = None
    try:
        result = orchestrator.run_local(
            c,
            split_name=args.split,
            queue=orchestrator.QUEUES[args.queue],
            include_canary=not args.no_canary,
            features=features,
            promote=args.promote,
            progress=progress,
        )
    except orchestrator.PromotionRefused as exc:
        refusal, result = exc, exc.result
    _p()
    if result is not None:
        _p(result.format())
        _p()
    where = data_mod.paths(c)
    _p(f"ledger: {where.relative(where.ledger)}")
    if refusal is not None:
        _p()
        _p(str(refusal))
        return 2
    return 0


def cmd_fleet(args) -> int:
    """Run the loop over a set of contracts in turn, then show where each stands.

    `--status` alone reads the ledgers and prints the table without running
    anything. Without it the fleet runs first; a contract whose data could
    not be built is reported and the exit status is 1, but the other
    contracts still run and still appear in the table. A contract whose
    promotion was refused ran: its summary is printed with the refusal under
    it, and the exit status is 0, because the fleet did what it could — the
    gate on a phase is `readiness verify`, not this command's exit code.
    """
    from readiness import fleet

    registry = _registry(args)
    if args.status:
        _rule(f"fleet status  ({len(registry)} contract(s))")
        _p(fleet.format_status(fleet.status(registry)))
        return 0
    progress = (lambda _m: None) if args.quiet else _p
    _rule(f"fleet  ({len(registry)} contract(s), queue={args.queue})")
    results = fleet.run_fleet(
        list(registry.values()),
        queue=args.queue,
        features=lambda c: _features(args, c),
        promote=args.promote,
        progress=progress,
    )
    _p()
    _p(fleet.format_results(results))
    _rule(f"fleet status  ({len(registry)} contract(s))")
    _p(fleet.format_status(fleet.status(registry)))
    return 1 if any(isinstance(r, Exception) for r in results.values()) else 0


def cmd_promote(args) -> int:
    """The one test touch, refused unless it is earned and unless it is asked for.

    Without `--param` the arguments are not the registry's defaults but the
    ones on the validate card that earned the pass: `readiness promote
    logistic` promotes what was actually validated, and prints what it
    adopted. With `--param` they are taken as given, and must match a validate
    card exactly like everything else.
    """
    from readiness.agent import orchestrator

    c = _contract(args)
    given = parse_params(args.model, args.param)  # also refuses an unknown model
    kwargs = given if args.param else None
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
    _p(
        "  arguments        "
        + json.dumps(card.data_snapshot.get("model_kwargs", {}), sort_keys=True)
    )
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
    if args.phase == 3:
        # Phase 3 is a statement about gap reports and their reviews, so like
        # Phase 2 it takes no contract.
        return _verify_phase3(args)
    if args.phase == 2:
        # Phase 2 is a statement about the whole registry ("at least four
        # hazards pass nationally"), so it takes no contract at all.
        return _verify_phase2(args)
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
    return _print_phase(0, c.name, result.checks)


def _print_phase(phase: int, label: str, checks) -> int:
    """Every check, then the verdict. `label` is a contract name, or empty for
    Phase 2, whose criteria are about the registry rather than one contract."""
    for check in checks:
        _print_check(check)
    _p()
    where = f" for {label}" if label else ""
    failures = [c.detail.splitlines()[0] for c in checks if not c.passed]
    if failures:
        _p(f"Phase {phase} NOT met{where} — {len(failures)} failure(s):")
        for f in failures:
            _p(f"  - {f}")
        return 1
    _p(f"Phase {phase} exit criteria met{where}.")
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
            checks.append(
                Check(f"replay {field}", ok, _replay_detail(field, expected, observed, ok))
            )
    elif args.replay:
        checks.append(Check("replay", False, "replay: no test card to replay"))
    return _print_phase(1, c.name, checks)


def _verify_phase2(args) -> int:
    """The registry-wide Phase 2 criteria: the fleet, the exposure join, the brief."""
    if getattr(args, "contract", None):
        # An ignored option is a lie: Phase 2 asks whether four hazards pass
        # nationally, which no single contract can answer.
        raise UsageError(
            f"`verify --phase 2` takes no contract (got -c {args.contract}): its "
            "criteria are about the whole registry — four national contracts "
            "passing, the exposure spot-check, an issued file each and one brief."
        )
    _rule("Phase 2 exit criteria  (the registry)")
    result = verify.phase2(registry=contracts.registered())
    return _print_phase(2, "", result.checks)


def _verify_phase3(args) -> int:
    """The Phase 3 criteria: the blinded reviews, the reports they name, the library.

    Real facility files, gap reports and review records never enter git, so
    `--reports` and `--reviews` point at the planner's own tree; the case
    studies and the no-coordinates scan read `plans/` itself.
    """
    if getattr(args, "contract", None):
        raise UsageError(
            f"`verify --phase 3` takes no contract (got -c {args.contract}): its "
            "criteria are about gap reports and the blinded reviews of them, which "
            "no single contract can answer."
        )
    _rule("Phase 3 exit criteria  (the gap reports and their reviews)")
    result = verify.phase3(
        reports_dir=pathlib.Path(args.reports) if args.reports else None,
        reviews_dir=pathlib.Path(args.reviews) if args.reviews else None,
    )
    return _print_phase(3, "", result.checks)


# ---------------------------------------------------------------------------
# exposure
# ---------------------------------------------------------------------------


def _state_fips_of(name: str) -> str:
    """A two-digit state FIPS from a postal code or a FIPS, or a usage error."""
    text = str(name).strip().upper()
    if text.isdigit() and len(text) == 2:
        return text
    fips_of = data_mod.state_fips()
    if not fips_of:
        raise UsageError(
            "no pinned Census county file, so state codes cannot be resolved; run "
            "`readiness snapshot` first, or name states by two-digit FIPS"
        )
    if text not in fips_of:
        raise UsageError(f"{text!r} is not a state in the Census county file")
    return fips_of[text]


def _exposure_states(args) -> list[str]:
    """The state FIPS an exposure command works over, in a stable order."""
    if getattr(args, "all_states", False):
        fips_of = data_mod.state_fips()
        if not fips_of:
            raise UsageError(
                "--all-states needs the pinned Census county file; run "
                "`readiness snapshot` first"
            )
        return sorted(set(fips_of.values()))
    given = getattr(args, "states", None)
    if not given:
        raise UsageError("name the states with --states A,B or ask for --all-states")
    return sorted({_state_fips_of(s) for s in given.split(",") if s.strip()})


def _pinned_exposure_states(manifest) -> list[str]:
    """Every state whose USA Structures counts are recorded in the manifest."""
    prefix = usa_structures.KEY_PREFIX
    return sorted(k[len(prefix):] for k in manifest.records if k.startswith(prefix))


def _exposure_table(states, manifest, *, what: str):
    """Load the pinned counts for `states`, or refuse naming what is not pinned."""
    from readiness.exposure.table import ExposureError, ExposureTable

    try:
        return ExposureTable.load(data_mod.SNAPSHOT_DIR, manifest, list(states))
    except ExposureError as exc:
        raise UsageError(
            f"{exc}\n{what}: `readiness exposure snapshot --states "
            f"{','.join(states) or 'XX'}` pins the counts this needs."
        ) from None


def cmd_exposure_snapshot(args) -> int:
    """Pull and pin one counts extract per state. Counts only: no footprint is fetched."""
    from readiness.connectors.base import Manifest

    states = _exposure_states(args)
    _rule(f"USA Structures county counts  ({len(states)} state(s))")
    manifest = Manifest.load(data_mod.MANIFEST_PATH)
    usa_structures.snapshot(
        states,
        data_mod.SNAPSHOT_DIR,
        manifest,
        layer_url=args.layer_url or usa_structures.LAYER_URL,
        refresh=args.refresh,
        progress=_p if not args.quiet else (lambda _m: None),
    )
    manifest.save()
    _p()
    _p(_exposure_table(states, manifest, what="exposure snapshot").summary())
    _p()
    _p(f"pinned under {usa_structures.KEY_PREFIX}<st> in "
       f"{data_mod.relative(data_mod.MANIFEST_PATH)}")
    return 0


def cmd_exposure_show(args) -> int:
    """Print the county rows the pinned extracts hold. The county is the finest key."""
    from readiness.connectors.base import Manifest

    manifest = Manifest.load(data_mod.MANIFEST_PATH)
    if args.county:
        states, wanted = [args.county[:2]], [args.county]
    elif args.state:
        states, wanted = [_state_fips_of(args.state)], None
    else:
        states, wanted = _pinned_exposure_states(manifest), None
    if not states:
        _p("no USA Structures counts are pinned; run `readiness exposure snapshot`")
        return 1
    table = _exposure_table(states, manifest, what="exposure show")
    rows = [table.rows[f] for f in sorted(table.rows) if wanted is None or f in wanted]
    _rule(f"county exposure  ({len(rows)} county/counties from state(s) "
          f"{', '.join(states)})")
    if not rows:
        _p(f"no row for {args.county}; the pinned extract for state "
           f"{states[0]} does not hold it")
        return 1
    _p(f"  {'fips':<8}{'total':>12}{'unclass.':>10}{'school':>9}{'hospital':>10}"
       f"{'medical':>9}  vintage  source")
    for row in rows:
        _p(f"  {row.fips:<8}{row.total:>12,}{row.unclassified_share:>10.1%}"
           f"{row.by_class.get('school', 0):>9,}{row.by_class.get('hospital', 0):>10,}"
           f"{row.by_class.get('medical', 0):>9,}  {row.vintage}     {row.source_key}")
    _p()
    _p(table.summary())
    return 0


def cmd_exposure_spot_check(args) -> int:
    """Our county totals over a person's assessor counts, every ratio printed."""
    from readiness.connectors.base import Manifest
    from readiness.exposure import spotcheck

    path = pathlib.Path(args.counts) if args.counts else verify.COUNTS_PATH
    if not path.exists():
        raise UsageError(f"no assessor counts file at {data_mod.relative(path)}")
    try:
        counts = spotcheck.load(path)
    except spotcheck.SpotCheckError as exc:
        # A malformed counts file is a refusal with the row that broke it, not
        # a traceback: the file is written by a person, by hand.
        raise UsageError(str(exc)) from None
    manifest = Manifest.load(data_mod.MANIFEST_PATH)
    states = sorted({row.fips[:2] for row in counts})
    _rule(f"exposure spot-check  ({data_mod.relative(path)}: {len(counts)} county/counties)")
    table = _exposure_table(states, manifest, what="exposure spot-check")
    checks = spotcheck.run(table, counts)
    _p(spotcheck.format(checks))
    return 0 if spotcheck.summary(checks).within_bounds else 1


def cmd_exposure(args) -> int:
    return args.exposure_func(args)


# ---------------------------------------------------------------------------
# issuance and the brief
# ---------------------------------------------------------------------------


def cmd_issue(args) -> int:
    """Refit the promoted model and write one period's probabilities, or refuse.

    There is no flag here through which a label could arrive, and none through
    which a guard could be skipped: the ledger's test card, the digests, the
    audit and the data's reach decide whether anything is written. `--reissue`
    is not such a flag either — it replaces a published file on purpose, and
    every guard still runs.
    """
    from readiness import issue as issue_mod

    c = _contract(args)
    kwargs = parse_params(args.model, args.param)
    try:
        year, period = issue_mod.parse_period(args.period, c)
    except issue_mod.IssueRefused as exc:
        _p()
        _p(str(exc))
        return 2
    label = issue_mod.period_label(year, period, c)
    _rule(f"issue  {args.model}  for {label}  ({c.name})")
    if kwargs:
        _p(f"  arguments        {json.dumps(kwargs, sort_keys=True)}")
    ds = _dataset(args, c, _features(args, c))
    try:
        issued = issue_mod.issue(
            c, ds, args.model, kwargs, (year, period),
            reissue=args.reissue,
            progress=_p if not args.quiet else (lambda _m: None),
        )
    except issue_mod.IssueRefused as exc:
        _p()
        _p(str(exc))
        return 2
    _p()
    _p(f"  {issued.summary()}")
    _p(f"  data version     sha256:{issued.data_version}")
    if issued.feature_version:
        _p(f"  feature version  sha256:{issued.feature_version}")
    _p()
    _p(f"next: readiness brief --county {sorted(issued.probabilities)[0]} "
       f"--period {label}")
    return 0


def _counties_for(args, label: str, issued) -> list[str]:
    """The counties a brief run covers: one, or every one the state issued."""
    if args.county:
        return [args.county]
    state = _state_fips_of(args.state)
    return sorted({
        fips
        for one in issued
        if one.period_label == label
        for fips in one.probabilities
        if fips.startswith(state)
    })


def _county_names() -> dict[str, tuple[str, str]]:
    """FIPS -> (county name, postal code) from the pinned county file, if there is one."""
    from readiness.connectors import census
    from readiness.connectors.base import Manifest

    cache = data_mod.SNAPSHOT_DIR / "census" / "national_county2020.txt"
    if not cache.exists():
        return {}
    manifest = Manifest.load(data_mod.MANIFEST_PATH)
    try:
        counties = census.load(
            data_mod.SNAPSHOT_DIR / "census", manifest, allow_fetch=False
        )
    except (ConnectorError, OSError):
        return {}
    return {c.fips: (c.name, c.state) for c in counties}


def _brief_inputs(label: str, issued_dir=None):
    """Everything a brief run reads once: issued files, the registry, the cards."""
    from readiness import issue as issue_mod
    from readiness.harness.ledger import Ledger

    registry = contracts.registered()
    issued = [i for i in issue_mod.read_issued(None, issued_dir) if i.period_label == label]
    cards = {}
    for one in issued:
        contract = registry.get(one.contract)
        if contract is None:
            continue
        where = data_mod.paths(contract)
        for card in Ledger(where.ledger).read():
            if card.experiment_id == one.validated_by:
                cards[one.contract] = card
    return registry, issued, cards


def cmd_brief(args) -> int:
    """One cited, validated brief per county — written only when it validates."""
    from readiness import brief as brief_mod
    from readiness.connectors.base import Manifest
    from readiness.exposure.table import ExposureError, ExposureTable

    label = args.period
    registry, issued, cards = _brief_inputs(label)
    if not issued:
        _p()
        _p(f"nothing is issued for period {label}; run `readiness issue MODEL -c NAME "
           f"--period {label}` first")
        return 1
    counties = _counties_for(args, label, issued)
    if not counties:
        _p()
        _p(f"no issued file for {label} covers a county in that scope")
        return 1

    names = _county_names()
    manifest = Manifest.load(data_mod.MANIFEST_PATH)
    pinned = set(_pinned_exposure_states(manifest))
    tables: dict[str, object] = {}
    out = pathlib.Path(args.out) if args.out else None
    _rule(f"county brief  ({len(counties)} county/counties, {label})")

    written, refused = 0, []
    resolver = brief_mod.resolver(registry)
    for fips in counties:
        state = fips[:2]
        if state in pinned and state not in tables:
            try:
                tables[state] = ExposureTable.load(
                    data_mod.SNAPSHOT_DIR, manifest, [state]
                )
            except (ExposureError, ConnectorError) as exc:
                _p(f"  {state}: exposure counts unusable ({exc}); briefs for this "
                   "state will say no layer is pinned")
                tables[state] = None
        table = tables.get(state)
        name, postal = names.get(fips, ("", ""))
        try:
            doc = brief_mod.build(
                fips, label, issued,
                table.for_county(fips) if table is not None else None,
                cards, brief_mod.county_label(name, postal, fips), registry,
            )
            html_path, json_path = brief_mod.write_validated(
                doc, resolver, out,
                contracts=[registry[i.contract] for i in issued
                           if i.covers(fips) and i.contract in registry],
                exposure_joined=table is not None and table.for_county(fips) is not None,
            )
        except brief_mod.BriefError as exc:
            refused.append((fips, [str(exc)]))
            continue
        except brief_mod.BriefRefused as exc:
            refused.append((fips, [str(v) for v in exc.violations]))
            continue
        written += 1
        _p(f"  wrote {data_mod.relative(html_path)}")
        _p(f"  wrote {data_mod.relative(json_path)}")
    _p()
    _p(f"{written} brief(s) written, {len(refused)} refused")
    for fips, violations in refused:
        _p()
        _p(f"{fips}: not written")
        for violation in violations:
            _p(f"  {violation}")
    return 1 if refused else 0


def _replay_detail(field: str, expected, observed, ok: bool) -> str:
    """One replay line, which never quotes a score the refit produced on test.

    The refit rescores the one-shot holdout. Printing its Brier score, AUC or
    skill would put a second reading of the test split in the terminal of
    anyone who runs `--replay`, which is the thing the touch budget exists to
    ration — so for those fields the line says only whether it agrees with the
    card. Digests and the two counts identify the data rather than score it,
    and are already on the card and in the report, so they are shown.
    """
    if field in verify.REPLAY_SHOWN_FIELDS:
        detail = f"replay {field}: card {verify.sig(expected)}"
        return detail if ok else f"{detail}, refit {verify.sig(observed)}  <- differs"
    return f"replay {field}: " + ("agrees" if ok else "<- differs")


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


# ---------------------------------------------------------------------------
# the planning thought-partner (Phase 3)
# ---------------------------------------------------------------------------


def cmd_scenarios_list(args) -> int:
    """The scenario library: ids, titles and how many questions each asks."""
    from readiness.plans import scenarios as scenarios_mod

    library = scenarios_mod.load_all()
    _rule(f"scenario library  ({len(library)} scenario(s))")
    if not library:
        _p("  nothing in plans/scenarios/")
        return 0
    for scenario in library:
        _p(f"  {scenarios_mod.summarise(scenario)}")
        _p(f"    specification  {scenario.source_doc}")
        for inject in scenario.injects:
            _p(f"    T+{inject.hour:<3} {inject.kind}")
        for question in scenario.questions:
            _p(f"    {question.id}  {question.rule:<26} {question.text[:60]}...")
        _p()
    return 0


def cmd_scenarios_check(args) -> int:
    """Run every committed case study through its scenario's rules."""
    from readiness.plans import case_studies as case_studies_mod

    directory = pathlib.Path(args.case_studies) if args.case_studies else None
    root = case_studies_mod.case_studies_dir(directory)
    results = case_studies_mod.check_all(directory)
    _rule(f"case studies  ({data_mod.relative(root)}: {len(results)} study/studies)")
    if not results:
        _p("  none committed, which is the default: a case study is an example added")
        _p("  deliberately, and every fact in it cites a published investigation.")
        _p("  The mechanism is exercised by tests/fixtures_plans.py.")
        return 0
    for result in results:
        _p(result.format())
    failed = [r for r in results if not r.passed]
    _p()
    _p(f"{len(results) - len(failed)}/{len(results)} case study/studies reproduce "
       f"their expected findings")
    return 1 if failed else 0


def cmd_scenarios(args) -> int:
    return args.scenarios_func(args)


def cmd_gap_report(args) -> int:
    """One facility's gap report, written only when every citation validates."""
    from readiness.plans import facility as facility_mod
    from readiness.plans import gap_report as gap_report_mod
    from readiness.plans import scenarios as scenarios_mod

    path = pathlib.Path(args.facility)
    out = pathlib.Path(args.out) if args.out else None
    _rule(f"gap report  ({path.name}, {args.period}, {args.scenario})")
    try:
        report = gap_report_mod.run(
            path, args.period, scenario_id=args.scenario, out_dir=out,
            drafter=args.drafter,
        )
    except facility_mod.FacilityError as exc:
        # A record we will not read is a refusal with the field that broke it,
        # not a traceback: the file is written by a person, by hand.
        _p()
        _p(str(exc))
        return 2
    except (scenarios_mod.ScenarioError, gap_report_mod.GapReportError) as exc:
        _p()
        _p(str(exc))
        return 2
    except gap_report_mod.GapReportRefused as exc:
        _p()
        _p(f"{path.name}: not written")
        for violation in exc.violations:
            _p(f"  {violation}")
        return 1
    for question_id, status in report.statuses().items():
        _p(f"  {question_id}  {status}")
    _p()
    for written in (report.html_path, report.json_path, report.blind_path):
        _p(f"  wrote {data_mod.relative(written)}")
    _p(f"  blinded sha256   {report.blind_sha256}")
    _p()
    _p("A practising emergency manager reviews every finding above; this report "
       "exists to make that review cheap, not to take it over.")
    _p(f"next: readiness review record --report {data_mod.relative(report.blind_path)} "
       f'--rating useful --role "practising emergency manager" --org-type hospital '
       f"--years 10")
    return 0


def cmd_review_record(args) -> int:
    """Bind a rating to the sha256 of a blinded rendering, or refuse."""
    from readiness.plans import reviews as reviews_mod

    reviews_dir = pathlib.Path(args.reviews) if args.reviews else None
    _rule(f"review  ({pathlib.Path(args.report).name}, {args.rating!r})")
    try:
        path, review = reviews_mod.record(
            args.report, rating=args.rating, reviewer_role=args.role,
            organisation_type=args.org_type, years_in_role=args.years,
            comments=args.comments or "", reviews_dir=reviews_dir,
        )
    except reviews_mod.ReviewError as exc:
        _p()
        _p(str(exc))
        return 2
    _p(f"  facility         {review.facility_hash}")
    _p(f"  period           {review.period}")
    _p(f"  report sha256    {review.report_sha256}")
    _p(f"  rating           {review.rating}")
    _p(f"  reviewer         {review.reviewer_role} "
       f"({review.organisation_type}, {review.years_in_role} year(s))")
    _p()
    _p(f"  wrote {data_mod.relative(path)}")
    _p()
    _p("This record is an attestation bound to one blinded rendering: change the "
       "report and its sha moves, and `verify --phase 3` stops counting this review.")
    return 0


def cmd_review(args) -> int:
    return args.review_func(args)


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

    sp = sub.add_parser("contracts", help="list the registered contracts")
    sp.add_argument("--names", action="store_true",
                    help="print one name per line and nothing else, for shell loops")
    sp.add_argument("--national", action="store_true",
                    help="with --names: only contracts whose scope is the whole country")
    sp.set_defaults(func=cmd_contracts)

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

    from readiness.agent.orchestrator import QUEUES

    queue_names = list(QUEUES)
    sp = features_flag(
        contract_flag(sub.add_parser("loop", help="run the experimental loop"))
    )
    sp.add_argument("--backend", default="local", choices=["local", "claude"])
    sp.add_argument("--split", default="validate", choices=["train", "validate"])
    sp.add_argument("--queue", default="baseline", choices=queue_names,
                    help="phase1 runs the baselines and then the Phase 1 candidates; "
                         "phase2 the baselines and the capped national candidates")
    sp.add_argument("--promote", action="store_true",
                    help="after the queue, spend the test touch on the first validate "
                         "pass in queue order")
    sp.add_argument("--no-canary", action="store_true",
                    help="skip the leaked-model demonstration")
    sp.add_argument("--quiet", action="store_true", help="suppress data-plane chatter")
    sp.set_defaults(func=cmd_loop)

    sp = features_flag(sub.add_parser(
        "fleet", help="run the loop over many contracts in turn, or show their status"
    ))
    which = sp.add_mutually_exclusive_group()
    which.add_argument("--national", action="store_true",
                       help="every registered contract whose scope is the whole country")
    which.add_argument("--contracts", metavar="A,B",
                       help="these registered contracts, comma-separated "
                            "(default: every registered contract)")
    sp.add_argument("--queue", default="phase2", choices=queue_names,
                    help="the queue each contract runs (default phase2)")
    sp.add_argument("--promote", action="store_true",
                    help="per contract, spend the test touch on the first validate pass")
    sp.add_argument("--status", action="store_true",
                    help="print the status table from the ledgers and exit; run nothing")
    sp.add_argument("--quiet", action="store_true", help="suppress the loops' chatter")
    sp.set_defaults(func=cmd_fleet)

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

    sp = sub.add_parser(
        "exposure",
        help="pull, show and spot-check the USA Structures county counts",
        description=(
            "County counts by occupancy class, pinned per state. The connector asks "
            "the FeatureServer for counts grouped by county FIPS and never downloads "
            "a footprint, so nothing finer than a county exists to print."
        ),
    )
    esub = sp.add_subparsers(dest="exposure_command", required=True)
    sp.set_defaults(func=cmd_exposure)

    e = esub.add_parser("snapshot", help="pull and pin the counts for some states")
    which = e.add_mutually_exclusive_group()
    which.add_argument("--states", metavar="A,B",
                       help="two-letter codes or two-digit FIPS, comma-separated")
    which.add_argument("--all-states", action="store_true",
                       help="every state in the pinned Census county file")
    e.add_argument("--layer-url", help=f"override the layer URL "
                                       f"(default {usa_structures.LAYER_URL})")
    e.add_argument("--refresh", action="store_true",
                   help="re-pull even where an extract is already pinned")
    e.add_argument("--quiet", action="store_true")
    e.set_defaults(exposure_func=cmd_exposure_snapshot)

    e = esub.add_parser("show", help="print the county rows from the pinned extracts")
    which = e.add_mutually_exclusive_group()
    which.add_argument("--county", metavar="FIPS", help="one five-digit county")
    which.add_argument("--state", metavar="XX", help="every county of one state")
    e.set_defaults(exposure_func=cmd_exposure_show)

    e = esub.add_parser(
        "spot-check",
        help="our county totals over the committed assessor counts; every ratio printed",
    )
    e.add_argument("--counts", metavar="PATH",
                   help="assessor counts CSV (default "
                        f"{data_mod.relative(verify.COUNTS_PATH)})")
    e.set_defaults(exposure_func=cmd_exposure_spot_check)

    sp = param_flag(features_flag(data_flags(sub.add_parser(
        "issue",
        help="refit the promoted model and write issued/<contract>/<period>.json",
        description=(
            "Refits the model a passing test card names, through TrainingView on the "
            "training years only, and forecasts one future period under the same "
            "firewall the backtest used. There is no parameter through which a label "
            "could arrive, and no flag that skips a guard."
        ),
    ))))
    sp.add_argument("model")
    sp.add_argument("--period", required=True, metavar="YYYY-Qn|YYYY-Mnn|YYYY",
                    help="the period to issue, in the contract's own shape")
    sp.add_argument("--reissue", action="store_true",
                    help="replace an issued file that already exists for this period")
    sp.set_defaults(func=cmd_issue)

    sp = sub.add_parser(
        "brief",
        help="one cited, validated brief per county from the issued files",
        description=(
            "Builds briefs/<fips>/<period>.html and .json from every issued file "
            "covering the county, the pinned USA Structures counts and the ledgers. "
            "Every sentence cites a claim that resolves; a document with a violation "
            "is not written, and the violations are printed."
        ),
    )
    which = sp.add_mutually_exclusive_group(required=True)
    which.add_argument("--county", metavar="FIPS", help="one five-digit county")
    which.add_argument("--state", metavar="XX",
                       help="every county of one state that the issued files cover")
    sp.add_argument("--period", required=True, metavar="YYYY-Qn",
                    help="the period label the issued files carry")
    sp.add_argument("--out", metavar="DIR",
                    help="write under this directory instead of briefs/")
    sp.set_defaults(func=cmd_brief)

    sp = sub.add_parser(
        "scenarios",
        help="list the scenario library, or run the committed case studies",
        description=(
            "The scenarios a blessed plan must survive. Each is a markdown "
            "specification written before anything executed it and a JSON "
            "transcription beside it; a test keeps the two in step. `check` runs "
            "every committed case study through its scenario's rules and compares "
            "the findings with the ones the study records."
        ),
    )
    ssub = sp.add_subparsers(dest="scenarios_command", required=True)
    sp.set_defaults(func=cmd_scenarios)

    s = ssub.add_parser("list", help="ids, titles and question counts")
    s.set_defaults(scenarios_func=cmd_scenarios_list)

    s = ssub.add_parser(
        "check",
        help="run every committed case study through its scenario's rules",
    )
    s.add_argument("--case-studies", metavar="DIR",
                   help="read the studies from here instead of plans/case-studies/")
    s.set_defaults(scenarios_func=cmd_scenarios_check)

    sp = sub.add_parser(
        "gap-report",
        help="one cited, validated gap report for one facility record",
        description=(
            "Runs a scenario's rules over a facility JSON and the issued risk layer "
            "for that facility's county and period, and writes "
            "<out>/<slug>/<period>.html, .json and .blind.html — but only when every "
            "sentence cites a claim that resolves. A missing design intensity is a "
            "fail-closed finding naming the document that would supply it; no county "
            "probability is ever substituted for it."
        ),
    )
    sp.add_argument("--facility", required=True, metavar="PATH",
                    help="the facility record JSON (never committed: see plans/facilities/)")
    sp.add_argument("--period", required=True, metavar="YYYY-Qn",
                    help="the period label the issued files carry")
    sp.add_argument("--scenario", default=plans_scenarios.DEFAULT_SCENARIO,
                    metavar="ID",
                    help=f"scenario id (default {plans_scenarios.DEFAULT_SCENARIO})")
    sp.add_argument("--out", metavar="DIR",
                    help="write under this directory instead of plans/reports/")
    sp.add_argument("--drafter", default="local", choices=list(plans_gap_report.DRAFTERS),
                    help="local is deterministic and needs no API key; claude rewrites "
                         "the same sentences and drops any that stop validating "
                         "(default local)")
    sp.set_defaults(func=cmd_gap_report)

    sp = sub.add_parser(
        "review",
        help="record a practising emergency manager's rating of a blinded report",
        description=(
            "A review record binds a rating to the sha256 of one blinded rendering, "
            "recomputed from the file. Change the report and the sha moves, and "
            "`verify --phase 3` stops counting the review."
        ),
    )
    rsub = sp.add_subparsers(dest="review_command", required=True)
    sp.set_defaults(func=cmd_review)

    r = rsub.add_parser("record", help="write plans/reviews/<sha256 of the report>.json")
    r.add_argument("--report", required=True, metavar="PATH.blind.html",
                   help="the blinded rendering the reviewer read")
    r.add_argument("--rating", required=True, choices=list(plans_reviews.RATINGS))
    r.add_argument("--role", required=True, metavar="TEXT",
                   help='what the reviewer does, in their words (the exit criterion '
                        'looks for "emergency manager")')
    r.add_argument("--org-type", required=True, dest="org_type",
                   choices=list(plans_reviews.ORG_TYPES))
    r.add_argument("--years", required=True, type=int, metavar="N",
                   help="years in the role")
    r.add_argument("--comments", metavar="TEXT")
    r.add_argument("--reviews", metavar="DIR",
                   help="write under this directory instead of plans/reviews/")
    r.set_defaults(review_func=cmd_review_record)

    sp = features_flag(data_flags(
        sub.add_parser("verify", help="check the Phase 0, 1, 2 or 3 exit criteria")
    ))
    sp.add_argument("--phase", type=int, default=0, choices=[0, 1, 2, 3],
                    help="0 scores the baselines against the pinned data; 1 reads one "
                         "contract's ledger; 2 reads the whole registry and 3 the gap "
                         "reports and their reviews, both taking no -c (default 0)")
    sp.add_argument("--reports", metavar="DIR",
                    help="with --phase 3: the blinded reports the reviews name "
                         "(default plans/reports/; real reports are never committed)")
    sp.add_argument("--reviews", metavar="DIR",
                    help="with --phase 3: the review records "
                         "(default plans/reviews/)")
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
