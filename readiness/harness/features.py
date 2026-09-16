"""The feature channel, and the temporal firewall around it.

Phase 0 models see nothing but units. Phase 1 models (report §6: "feature
construction from NRI, historical frequencies, terrain and precipitation
reanalysis") need covariates, and every covariate is a new way for the future
to leak into a forecast. So the harness owns the channel:

* A *source* hands over raw material only: a monthly series per region, or a
  static table per region. It never sees a unit and never decides a cutoff.
* The harness computes the cutoff for every unit itself — the first month of
  the forecast period minus the spec's lag — and hands the transform nothing
  later than that. Transforms come from a closed vocabulary; they are the only
  code that touches a series, and they only ever see `Series.before(cutoff)`.
* A static layer declares the last year of data it encodes. If that year is
  not before the contract's first validate year, the layer is refused: a risk
  index published in 2024 knows about the 2016–2023 floods it would be asked to
  forecast. FEMA's National Risk Index is refused under every current contract
  for exactly this reason, and that refusal is a finding, not a bug.
* A source built from the ground truth (Storm Events, or any records file) is
  refused as a feature outright. History features that a model derives from its
  own training labels are computed inside `fit()` from the `TrainingView`,
  where the split already protects them.
* Before anything is fitted, `audit_frame` rebuilds every value from a series
  in which every month at or after the cutoff has been replaced by a poison
  value. A transform that reads past the cutoff changes its answer and is
  caught; one that does not is bit-identical. The audit is mechanical and runs
  on every scoring call.

What the harness proves: temporal precedence (from cutoffs it computed) and
label origin (from manifest keys). What it trusts: the `derived_through` year a
static connector declares, which is a reviewed constant pinned into the
manifest record. That is the honest extent of "the harness can check it".
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from readiness.contracts import Contract
from readiness.harness.labels import Unit

__all__ = [
    "MonthIndex",
    "NAN",
    "POISON",
    "Series",
    "FeatureSource",
    "FeatureSpec",
    "FeatureFrame",
    "FeatureAdmissionError",
    "FeatureAudit",
    "AuditFinding",
    "TRANSFORMS",
    "LABEL_SOURCE_PREFIXES",
    "TIMELESS_STATIC_SOURCES",
    "month_index",
    "period_start",
    "period_length",
    "build_frame",
    "admit",
    "audit_frame",
]

#: Months since year 0: `year * 12 + (month - 1)`. Integer arithmetic only, so
#: a cutoff is a comparison, never a date library's idea of a boundary.
MonthIndex = int

NAN = float("nan")

#: What the audit writes over every month at or after the cutoff. Any transform
#: that reads such a month produces a visibly absurd number instead of its
#: honest one, and the audit compares the two.
POISON = 1e15

#: Manifest-key prefixes of ground-truth sources. Nothing pinned under these
#: may be a feature: it *is* the label, or is derived from it.
LABEL_SOURCE_PREFIXES: tuple[str, ...] = (
    "noaa/storm_events/",
    "records/",
    "emdat/",
    "desinventar/",
)

#: Static sources allowed to declare no `derived_through` year, because they
#: encode physical geometry that no event changes: where a county is, how high
#: it sits, how much of it is water. Everything else must declare a year.
TIMELESS_STATIC_SOURCES: frozenset[str] = frozenset({"gazetteer", "elevation"})


def month_index(year: int, month: int) -> MonthIndex:
    if not 1 <= month <= 12:
        raise ValueError(f"month out of range: {month}")
    return year * 12 + (month - 1)


def period_length(periods_per_year: int) -> int:
    """Months in one period: 12 for a year, 3 for a quarter, 1 for a month."""
    if periods_per_year not in (1, 2, 4, 12):
        raise ValueError(f"unsupported periods per year: {periods_per_year}")
    return 12 // periods_per_year


def period_start(year: int, period: int, periods_per_year: int) -> MonthIndex:
    """The month index of a period's first month. `period` is 1-based."""
    length = period_length(periods_per_year)
    if not 1 <= period <= periods_per_year:
        raise ValueError(f"period {period} out of range for {periods_per_year}/year")
    return month_index(year, (period - 1) * length + 1)


@dataclass(frozen=True)
class Series:
    """One region's monthly values, contiguous from `start`. NaN is missing."""

    region: str
    start: MonthIndex
    values: tuple[float, ...]

    @property
    def end(self) -> MonthIndex:
        """One past the last month held (exclusive)."""
        return self.start + len(self.values)

    def before(self, cutoff: MonthIndex) -> "Series":
        """Strictly earlier months only. The one slice a transform ever sees."""
        if cutoff <= self.start:
            return Series(self.region, self.start, ())
        return Series(self.region, self.start, self.values[: cutoff - self.start])

    def poisoned_from(self, cutoff: MonthIndex) -> "Series":
        """Every month at or after `cutoff` replaced by `POISON`, for the audit."""
        keep = max(0, min(len(self.values), cutoff - self.start))
        return Series(
            self.region,
            self.start,
            self.values[:keep] + (POISON,) * (len(self.values) - keep),
        )

    def value(self, month: MonthIndex) -> float:
        if self.start <= month < self.end:
            return self.values[month - self.start]
        return NAN

    def window(self, cutoff: MonthIndex, months: int) -> tuple[float, ...]:
        """The `months` months ending just before `cutoff`, NaN-padded."""
        return tuple(self.value(m) for m in range(cutoff - months, cutoff))


@runtime_checkable
class FeatureSource(Protocol):
    """What a connector hands the harness. Raw material, no cutoffs.

    `kind` is "series" or "static". `manifest_keys` names the pinned inputs the
    source was built from (the label-origin rule reads them). `derived_through`
    is the last calendar year of data a static layer encodes, or None for the
    timeless geometry sources; it is ignored for series sources, whose months
    speak for themselves.
    """

    name: str
    kind: str
    manifest_keys: tuple[str, ...]
    derived_through: int | None
    global_coverage: bool

    def series(self, region: str, variable: str) -> Series | None: ...

    def static(self, region: str) -> Mapping[str, float] | None: ...


def _clean(values: Iterable[float]) -> list[float]:
    return [v for v in values if not math.isnan(v)]


def _trailing_sum(series: Series, cutoff: MonthIndex, spec: "FeatureSpec", ppy: int) -> float:
    xs = _clean(series.window(cutoff, spec.window_months))
    return sum(xs) if len(xs) == spec.window_months else NAN


def _trailing_mean(series: Series, cutoff: MonthIndex, spec: "FeatureSpec", ppy: int) -> float:
    xs = _clean(series.window(cutoff, spec.window_months))
    return sum(xs) / len(xs) if len(xs) == spec.window_months else NAN


def _trailing_max(series: Series, cutoff: MonthIndex, spec: "FeatureSpec", ppy: int) -> float:
    xs = _clean(series.window(cutoff, spec.window_months))
    return max(xs) if len(xs) == spec.window_months else NAN


def _same_period_mean(
    series: Series, cutoff: MonthIndex, spec: "FeatureSpec", ppy: int
) -> float:
    """Mean over prior years of the series summed over this period's months.

    The period is the one whose start is `cutoff + spec.lag_months`; the
    window is `window_months // 12` prior years. A prior year's period that is
    not entirely before the cutoff (a yearly period with a one-month lag) is
    missing, not truncated: the firewall decides, not the transform.
    """
    length = period_length(ppy)
    start = cutoff + spec.lag_months
    years = spec.window_months // 12
    totals = []
    for back in range(1, years + 1):
        months = [series.value(m) for m in range(start - 12 * back, start - 12 * back + length)]
        if start - 12 * back + length > cutoff or any(math.isnan(m) for m in months):
            continue
        totals.append(sum(months))
    return sum(totals) / len(totals) if len(totals) == years else NAN


Transform = Callable[[Series, MonthIndex, "FeatureSpec", int], float]

#: The closed vocabulary. Every callable receives a series that has already been
#: cut at the cutoff; the audit checks that it makes no difference either way.
TRANSFORMS: dict[str, Transform] = {
    "trailing_sum": _trailing_sum,
    "trailing_mean": _trailing_mean,
    "trailing_max": _trailing_max,
    "same_period_mean": _same_period_mean,
}

STATIC = "static"


@dataclass(frozen=True)
class FeatureSpec:
    """One column: which source, which variable, which transform, which lag.

    `lag_months` is how many whole months before the period's first month are
    withheld. The cutoff is `period_start - lag_months`, and only months
    strictly before it reach a transform, so with the minimum lag of one a
    July forecast is built from data through May: the month just before the
    period is never assumed to be complete by the time the period starts,
    which is the rule an operational issuance has to live by (reanalysis and
    reporting both run late). Lag zero is refused for that reason, not
    because it would admit the period's own months — those are always out.
    """

    column: str
    source: str
    variable: str
    transform: str = STATIC
    window_months: int = 0
    lag_months: int = 1

    def __post_init__(self) -> None:
        if not self.column or not self.source or not self.variable:
            raise ValueError("a feature spec needs a column, a source and a variable")
        if self.transform != STATIC and self.transform not in TRANSFORMS:
            raise ValueError(
                f"unknown transform {self.transform!r}; known: "
                f"{sorted(TRANSFORMS)} or {STATIC!r}"
            )
        if self.transform != STATIC:
            if self.window_months < 1:
                raise ValueError(f"{self.column}: window_months must be >= 1")
            if self.lag_months < 1:
                raise ValueError(
                    f"{self.column}: lag_months must be >= 1; the month just before a "
                    "period is not complete when the period starts"
                )
            if self.transform == "same_period_mean" and self.window_months % 12:
                raise ValueError(f"{self.column}: same_period_mean needs whole years")

    @property
    def is_static(self) -> bool:
        return self.transform == STATIC

    def cutoff(self, unit: Unit, ppy: int) -> MonthIndex:
        """Harness-computed: the period's first month minus the lag."""
        _region, year, period = unit
        return period_start(year, period, ppy) - self.lag_months

    def to_dict(self) -> dict:
        return {
            "column": self.column,
            "source": self.source,
            "variable": self.variable,
            "transform": self.transform,
            "window_months": self.window_months,
            "lag_months": self.lag_months,
        }


@dataclass(frozen=True)
class FeatureFrame:
    """Feature values per unit, in spec order. NaN means missing, never zero."""

    columns: tuple[str, ...]
    rows: Mapping[Unit, tuple[float, ...]]
    specs: tuple[FeatureSpec, ...]
    source_keys: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.rows)

    def row(self, unit: Unit) -> tuple[float, ...]:
        return self.rows[unit]

    @property
    def units(self) -> tuple[Unit, ...]:
        return tuple(sorted(self.rows))

    def restrict(self, units: Sequence[Unit]) -> "FeatureFrame":
        keep = {u: self.rows[u] for u in units if u in self.rows}
        return FeatureFrame(self.columns, keep, self.specs, self.source_keys)

    def missing_share(self) -> dict[str, float]:
        if not self.rows:
            return {c: 0.0 for c in self.columns}
        out = {}
        for i, column in enumerate(self.columns):
            missing = sum(1 for r in self.rows.values() if math.isnan(r[i]))
            out[column] = missing / len(self.rows)
        return out

    def digest(self) -> str:
        """Content hash over columns, specs and every row, in unit order."""
        h = hashlib.sha256()
        h.update("|".join(self.columns).encode() + b"\n")
        for spec in self.specs:
            h.update(repr(sorted(spec.to_dict().items())).encode() + b"\n")
        for unit in self.units:
            region, year, period = unit
            values = ",".join(repr(v) for v in self.rows[unit])
            h.update(f"{region}|{year}|{period}|{values}\n".encode())
        return h.hexdigest()[:16]


class FeatureAdmissionError(ValueError):
    """A source or a frame that the firewall refuses to hand to a model."""


def admit(source: FeatureSource, contract: Contract) -> None:
    """Refuse a source that could carry the outcome into the features.

    Three rules, in order: nothing built from a ground-truth source; a static
    layer must encode data from before the first validate year; a static layer
    that claims to be timeless must be one of the geometry sources.
    """
    origin = [k for k in source.manifest_keys if k.startswith(LABEL_SOURCE_PREFIXES)]
    if origin:
        raise FeatureAdmissionError(
            f"source {source.name!r} is built from the ground truth ({', '.join(origin)}); "
            "a feature derived from the labels is the leak the harness exists to stop"
        )
    if source.kind == STATIC:
        first_holdout = contract.validate_years[0]
        if source.derived_through is None:
            if source.name not in TIMELESS_STATIC_SOURCES:
                raise FeatureAdmissionError(
                    f"static source {source.name!r} declares no derived_through year "
                    f"and is not a timeless geometry source ({sorted(TIMELESS_STATIC_SOURCES)})"
                )
        elif source.derived_through >= first_holdout:
            raise FeatureAdmissionError(
                f"static source {source.name!r} encodes data through "
                f"{source.derived_through}, but contract {contract.name!r} starts "
                f"validating in {first_holdout}; a layer that has seen the holdout "
                "years cannot be a feature for them"
            )
    elif source.kind != "series":
        raise FeatureAdmissionError(f"source {source.name!r} has unknown kind {source.kind!r}")


def _value(
    spec: FeatureSpec,
    source: FeatureSource,
    unit: Unit,
    ppy: int,
    *,
    poison: bool = False,
) -> float:
    region = unit[0]
    if spec.is_static:
        table = source.static(region)
        if table is None:
            return NAN
        v = table.get(spec.variable, NAN)
        return float(v) if v is not None else NAN
    series = source.series(region, spec.variable)
    if series is None:
        return NAN
    cutoff = spec.cutoff(unit, ppy)
    if poison:
        # The audit's run: the transform is handed the *uncut* series with
        # every month at or after the cutoff poisoned. A transform that reads
        # any of them cannot produce the same value as the honest run below.
        return TRANSFORMS[spec.transform](series.poisoned_from(cutoff), cutoff, spec, ppy)
    return TRANSFORMS[spec.transform](series.before(cutoff), cutoff, spec, ppy)


def _resolve(specs: Sequence[FeatureSpec], sources: Mapping[str, FeatureSource]) -> None:
    for spec in specs:
        if spec.source not in sources:
            raise FeatureAdmissionError(
                f"column {spec.column!r} names source {spec.source!r}, which was not loaded"
            )
        source = sources[spec.source]
        if spec.is_static and source.kind != STATIC:
            raise FeatureAdmissionError(
                f"column {spec.column!r} is static but source {spec.source!r} is a series"
            )
        if not spec.is_static and source.kind != "series":
            raise FeatureAdmissionError(
                f"column {spec.column!r} uses {spec.transform!r} but source "
                f"{spec.source!r} is static"
            )


def build_frame(
    specs: Sequence[FeatureSpec],
    sources: Mapping[str, FeatureSource],
    units: Sequence[Unit],
    periods_per_year: int,
    *,
    _poison: bool = False,
) -> FeatureFrame:
    """One row per unit. Takes units, never a panel: labels cannot get in here."""
    specs = tuple(specs)
    _resolve(specs, sources)
    columns = tuple(s.column for s in specs)
    if len(set(columns)) != len(columns):
        raise FeatureAdmissionError(f"duplicate feature columns: {columns}")
    rows = {
        unit: tuple(
            _value(spec, sources[spec.source], unit, periods_per_year, poison=_poison)
            for spec in specs
        )
        for unit in units
    }
    keys = sorted({k for s in specs for k in sources[s.source].manifest_keys})
    return FeatureFrame(columns, rows, specs, tuple(keys))


@dataclass(frozen=True)
class AuditFinding:
    check: str
    passed: bool
    detail: str

    def format(self) -> str:
        return f"  [{'ok' if self.passed else 'REFUSED':>7}] {self.check:<18} {self.detail}"


@dataclass(frozen=True)
class FeatureAudit:
    findings: tuple[AuditFinding, ...]

    @property
    def clean(self) -> bool:
        return all(f.passed for f in self.findings)

    def to_dict(self) -> dict:
        return {
            "clean": self.clean,
            "findings": [
                {"check": f.check, "passed": f.passed, "detail": f.detail}
                for f in self.findings
            ],
        }

    def format(self) -> str:
        head = "feature audit -> " + ("clean" if self.clean else "REFUSED")
        return "\n".join([head, *(f.format() for f in self.findings)])


def _same(a: float, b: float) -> bool:
    return (math.isnan(a) and math.isnan(b)) or a == b


def audit_frame(
    specs: Sequence[FeatureSpec],
    sources: Mapping[str, FeatureSource],
    units: Sequence[Unit],
    contract: Contract,
    frame: FeatureFrame,
) -> FeatureAudit:
    """Everything the harness can check about a frame before a model sees it."""
    findings: list[AuditFinding] = []
    used = sorted({s.source for s in specs})

    # 1. Admission of every source used: label origin and static vintage.
    for name in used:
        try:
            admit(sources[name], contract)
        except FeatureAdmissionError as exc:
            findings.append(AuditFinding("admission", False, str(exc)))
        else:
            src = sources[name]
            when = (
                "series" if src.kind == "series"
                else f"static through {src.derived_through}"
                if src.derived_through is not None
                else "static, timeless geometry"
            )
            findings.append(AuditFinding("admission", True, f"{name}: {when}"))

    # 2. Timestamp bound: rebuild every value from the uncut series with every
    #    month at or after the unit's cutoff poisoned. The honest build only
    #    ever hands a transform the cut series, so a transform that survives
    #    this is one that would not have read past the cutoff even if it could.
    poisoned = build_frame(specs, sources, units, contract.periods_per_year, _poison=True)
    leaks = [
        (unit, column)
        for unit in units
        for column, a, b in zip(frame.columns, frame.rows[unit], poisoned.rows[unit])
        if not _same(a, b)
    ]
    if leaks:
        unit, column = leaks[0]
        findings.append(
            AuditFinding(
                "timestamp bound",
                False,
                f"{len(leaks)} value(s) change when months at or after the cutoff are "
                f"poisoned; first: column {column!r} for unit {unit}",
            )
        )
    else:
        findings.append(
            AuditFinding(
                "timestamp bound",
                True,
                f"{len(units):,} units x {len(frame.columns)} columns unchanged under "
                "poisoning at the cutoff",
            )
        )

    # 3. Coverage, for the reader: a column that is mostly missing is a finding
    #    about the data, not a refusal.
    share = frame.missing_share()
    worst = max(share.values()) if share else 0.0
    findings.append(
        AuditFinding(
            "coverage",
            True,
            ", ".join(f"{c} {share[c]:.1%} missing" for c in frame.columns)
            if worst > 0
            else "no missing values",
        )
    )
    return FeatureAudit(tuple(findings))
