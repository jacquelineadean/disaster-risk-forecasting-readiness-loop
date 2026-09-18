"""The feature sets a Phase 1 model may ask the harness for, and how it reads them.

Report §6, Phase 1: "feature construction from NRI, historical frequencies,
terrain and precipitation reanalysis". A model does not construct features:
it *declares* them as `FeatureSpec`s from the harness's closed vocabulary, and
the harness builds and audits every row under its own cutoffs. This module
is the engine's side of that bargain — a catalogue of named spec bundles, and
the two functions that turn a view or a request into a plain matrix.

The "nri" set is deliberately present although the harness refuses FEMA's
National Risk Index under every current contract (its v1.20 layer encodes
data through 2023). The refusal is the firewall's real-data demonstration;
requesting the set is how a run makes it visible.
"""

from __future__ import annotations

from typing import Sequence

from readiness.engine.history import HistoryFeatures
from readiness.harness.features import FeatureFrame, FeatureSpec
from readiness.harness.labels import Unit
from readiness.harness.splits import PredictionRequest, TrainingView

__all__ = ["FEATURE_SETS", "specs_for", "training_matrix", "request_matrix"]


def _series(column: str, variable: str, transform: str, months: int) -> FeatureSpec:
    return FeatureSpec(column, "era5", variable, transform, months, lag_months=1)


def _static(column: str, source: str, variable: str) -> FeatureSpec:
    return FeatureSpec(column, source, variable)


#: Named bundles of specs. Column names are short and unique across bundles so
#: any combination can be concatenated into one frame.
FEATURE_SETS: dict[str, tuple[FeatureSpec, ...]] = {
    # Antecedent conditions from the ERA5 monthly extract: how wet the last
    # month, quarter and year were, how wet this period of the year usually is
    # (ten prior years), and how warm the last quarter was. All lag one month.
    "era5-antecedent": (
        _series("precip_1m", "precip_mm", "trailing_sum", 1),
        _series("precip_3m", "precip_mm", "trailing_sum", 3),
        _series("precip_12m", "precip_mm", "trailing_sum", 12),
        _series("precip_same_10y", "precip_mm", "same_period_mean", 120),
        _series("tmean_3m", "tmean_c", "trailing_mean", 3),
    ),
    # Timeless geometry: the two sources on the harness's allow-list.
    "terrain": (
        _static("elevation_m", "elevation", "elevation_m"),
        _static("water_share", "gazetteer", "water_share"),
        _static("lat", "gazetteer", "lat"),
    ),
    # FEMA NRI: refused by admission under every current contract, on purpose.
    "nri": (
        _static("nri_eal", "nri", "EAL_SCORE"),
        _static("nri_risk", "nri", "RISK_SCORE"),
    ),
    # A pinned CLIMADA event-set layer: admissible only when its event set
    # ends before the contract's first validate year.
    "climada-prior": (
        _static("climada_rp10", "climada", "rp10"),
        _static("climada_rp50", "climada", "rp50"),
        _static("climada_rp100", "climada", "rp100"),
    ),
}


def specs_for(feature_sets: Sequence[str]) -> tuple[FeatureSpec, ...]:
    """The specs of the named sets, concatenated in the order given."""
    specs: list[FeatureSpec] = []
    for name in feature_sets:
        if name not in FEATURE_SETS:
            raise KeyError(
                f"unknown feature set {name!r}; known: {sorted(FEATURE_SETS)}"
            )
        specs.extend(FEATURE_SETS[name])
    return tuple(specs)


# -- reading the frame the harness handed over ------------------------------


def _feature_row(frame: FeatureFrame | None, unit: Unit, n_columns: int) -> list[float]:
    """The unit's feature values, or all-missing when the frame has none."""
    if n_columns == 0:
        return []
    if frame is None:
        raise ValueError(
            f"the model declares {n_columns} feature column(s) but the harness handed "
            "over no frame; score it with the sources loaded"
        )
    return [float(v) for v in frame.row(unit)]


def training_matrix(
    view: TrainingView, specs: Sequence[FeatureSpec], history: HistoryFeatures | None
) -> tuple[list[str], list[list[float]], list[int]]:
    """Column names, one raw row per training unit in `view.units()` order, labels.

    Feature columns come from the view's frame; the history column, when a
    model asks for one, is the leave-one-year-out logit for each training
    row (see `history.py` for why). NaN stays NaN: how to treat a missing
    value is the model's decision, not the matrix builder's.
    """
    rows = view.rows()
    frame = view.features if specs else None
    matrix, labels = [], []
    for unit, label in rows:
        row = _feature_row(frame, unit, len(specs))
        if history is not None:
            row.append(history.logit(unit, in_sample=True))
        matrix.append(row)
        labels.append(label)
    return _column_names(specs, history), matrix, labels


def request_matrix(
    request: PredictionRequest,
    specs: Sequence[FeatureSpec],
    history: HistoryFeatures | None,
) -> list[list[float]]:
    """One raw row per requested unit, in request order, history out-of-sample."""
    frame = request.features if specs else None
    matrix = []
    for unit in request:
        row = _feature_row(frame, unit, len(specs))
        if history is not None:
            row.append(history.logit(unit, in_sample=False))
        matrix.append(row)
    return matrix


def _column_names(specs: Sequence[FeatureSpec], history: HistoryFeatures | None) -> list[str]:
    names = [s.column for s in specs]
    if history is not None:
        names.append("history")
    return names
