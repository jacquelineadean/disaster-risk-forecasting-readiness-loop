"""Post-hoc calibration fitted on the last training years, never on a holdout.

The contract's reliability clause is the hard one (report §4: "reliability
within ±5 points per populated bin"), and a discriminating model is usually
over-confident. The standard remedy is a monotone map from the model's
forecasts to observed frequencies, fitted on forecasts the model made for
rows it was not fitted on. The temptation is to fit that map on the validate
split — which would spend the holdout on the model. This wrapper does it the
only permitted way:

1. Split the training years: `early` = all but the last `holdout_years`,
   `late` = those last years. Both come from `TrainingView.restrict`, which
   can only narrow a training view, so the calibrator cannot construct a
   view over a holdout year even by accident.
2. Fit the inner model on `early`; ask it for forecasts on the `late` units
   through an ordinary `PredictionRequest` (still training data: the labels
   used for the map are training labels).
3. Fit the map on (forecast, label) pairs: isotonic regression by
   pool-adjacent-violators, or a Platt sigmoid on the logit.
4. Refit the inner model on the full training view, so its forecasts on the
   holdout come from every training year.

`predict` is `inner.predict` followed by the map. The wrapped model's name is
`<inner>+iso` or `<inner>+platt`, its version and feature specs are the
inner's, so the harness treats the pair as one candidate.
"""

from __future__ import annotations

import bisect
from typing import Sequence

from readiness.engine.base import FittedModel
from readiness.engine.history import clip, logit
from readiness.engine.linear import PROB_EPS, sigmoid
from readiness.harness.splits import PredictionRequest, TrainingView

__all__ = ["Calibrated", "IsotonicMap", "PlattMap"]

METHODS = ("isotonic", "platt")


class IsotonicMap:
    """Non-decreasing step function from pool-adjacent-violators.

    Fitting sorts the pairs by forecast, pools identical forecasts, then merges
    adjacent blocks whose means decrease until the sequence is monotone. Each
    block keeps the mean forecast of the rows it pooled, so at predict time a
    forecast is mapped by linear interpolation between neighbouring block
    means (a step function would jump at arbitrary points between blocks);
    beyond the outermost blocks the map is flat.
    """

    def __init__(self) -> None:
        self.centres: list[float] = []
        self.values: list[float] = []

    def fit(self, forecasts: Sequence[float], labels: Sequence[int]) -> "IsotonicMap":
        pairs = sorted(zip(forecasts, labels))
        blocks: list[list[float]] = []  # [sum of x, sum of y, count]
        last_x = None
        for x, y in pairs:
            if x == last_x:
                blocks[-1][0] += x
                blocks[-1][1] += y
                blocks[-1][2] += 1
            else:
                blocks.append([x, float(y), 1])
                last_x = x
        pooled: list[list[float]] = []
        for block in blocks:
            pooled.append(block)
            while len(pooled) > 1 and _mean(pooled[-2]) > _mean(pooled[-1]):
                last = pooled.pop()
                for k in range(3):
                    pooled[-1][k] += last[k]
        blocks = pooled
        self.centres = [b[0] / b[2] for b in blocks]
        self.values = [_mean(b) for b in blocks]
        return self

    def __call__(self, x: float) -> float:
        if not self.centres:
            return x
        if x <= self.centres[0]:
            return self.values[0]
        if x >= self.centres[-1]:
            return self.values[-1]
        k = bisect.bisect_right(self.centres, x)
        x0, x1 = self.centres[k - 1], self.centres[k]
        y0, y1 = self.values[k - 1], self.values[k]
        return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def _mean(block: Sequence[float]) -> float:
    return block[1] / block[2]


class PlattMap:
    """`sigmoid(a · logit(p) + b)`, fitted by a fixed number of Newton steps.

    A tiny ridge keeps the 2x2 Hessian invertible when the late years hold a
    single class; the step count is fixed so the fit is a function of the
    data alone.
    """

    STEPS = 25
    RIDGE = 1e-6

    def __init__(self) -> None:
        self.a = 1.0
        self.b = 0.0

    def fit(self, forecasts: Sequence[float], labels: Sequence[int]) -> "PlattMap":
        zs = [logit(clip(p, PROB_EPS, 1.0 - PROB_EPS)) for p in forecasts]
        a, b = 1.0, 0.0
        for _ in range(self.STEPS):
            ga = gb = 0.0
            haa = hab = hbb = 0.0
            for z, y in zip(zs, labels):
                p = sigmoid(a * z + b)
                r, w = p - y, p * (1.0 - p)
                ga += r * z
                gb += r
                haa += w * z * z
                hab += w * z
                hbb += w
            haa += self.RIDGE
            hbb += self.RIDGE
            det = haa * hbb - hab * hab
            a -= (hbb * ga - hab * gb) / det
            b -= (haa * gb - hab * ga) / det
        self.a, self.b = a, b
        return self

    def __call__(self, p: float) -> float:
        return sigmoid(self.a * logit(clip(p, PROB_EPS, 1.0 - PROB_EPS)) + self.b)


class Calibrated(FittedModel):
    """An inner model plus a monotone map fitted on its late-training forecasts."""

    def __init__(
        self, inner: FittedModel, method: str = "isotonic", holdout_years: int = 3
    ) -> None:
        super().__init__()
        if method not in METHODS:
            raise ValueError(f"unknown calibration {method!r}; known: {METHODS}")
        if holdout_years < 1:
            raise ValueError("holdout_years must be at least 1")
        self.inner = inner
        self.method = method
        self.holdout_years = holdout_years
        self.name = f"{inner.name}+{'iso' if method == 'isotonic' else 'platt'}"
        self.version = inner.version
        self.feature_specs = inner.feature_specs
        self.map: IsotonicMap | PlattMap | None = None

    def _fit(self, view: TrainingView) -> None:
        years = sorted({unit[1] for unit in view.units()})
        if len(years) <= self.holdout_years:
            raise ValueError(
                f"{self.name}: {len(years)} training year(s) cannot spare "
                f"{self.holdout_years} for calibration"
            )
        early = view.restrict(years[: -self.holdout_years])
        late = view.restrict(years[-self.holdout_years :])
        self.inner.fit(early)
        request = PredictionRequest(
            units=tuple(late.units()), split_name=view.split.name, features=late.features
        )
        forecasts = [float(p) for p in self.inner.predict(request)]
        labels = [label for _unit, label in late.rows()]
        self.map = (IsotonicMap() if self.method == "isotonic" else PlattMap()).fit(
            forecasts, labels
        )
        self.inner.fit(view)

    def _predict(self, request: PredictionRequest) -> Sequence[float]:
        assert self.map is not None
        return [clip(self.map(float(p)), 0.0, 1.0) for p in self.inner.predict(request)]
