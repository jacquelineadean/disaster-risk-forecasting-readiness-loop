"""Logistic regression on harness-built features, fitted without a random number.

The first real candidate of Phase 1. It is deliberately the dullest possible
learner — full-batch gradient descent on the L2-regularised log-loss for a
fixed number of steps — because the loop's value is in the harness, not the
model, and a model whose fit depends on a seed or a convergence tolerance is a
model whose fingerprint depends on the interpreter's mood. Every reduction
over floats here is `math.fsum`, which is exactly rounded and therefore the
same number on every interpreter; the builtin `sum` is not (3.12 compensates
where 3.10 folds left), and a model fitted through 400 steps of it drifts
visibly. So the same view and the same frame give byte-identical
probabilities twice, on 3.10 and on 3.12 alike.

Columns, in order: each declared feature standardised with the training
mean and standard deviation (a zero deviation is treated as one), then a
missing indicator for every feature that had any NaN in training (NaN itself
is imputed to the training mean, so the indicator carries the information
"this was missing" on its own), then the standardised history logit when the
model asks for one, then an intercept. The intercept is not penalised.
"""

from __future__ import annotations

import math
from typing import Sequence

from readiness.engine.base import FittedModel
from readiness.engine.features import request_matrix, specs_for, training_matrix
from readiness.engine.history import HistoryFeatures, clip
from readiness.harness.splits import PredictionRequest, TrainingView

__all__ = ["LogisticRegression", "Standardiser", "sigmoid", "log_loss"]

#: Forecasts are kept strictly inside (0, 1): a probability of exactly one
#: that misses is an infinite log-loss and a reliability bin with no slack.
PROB_EPS = 1e-6


def sigmoid(z: float) -> float:
    """Numerically stable: never evaluates exp of a large positive number."""
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def log_loss(z: float, y: int) -> float:
    """Log-loss of a logit `z` against label `y`, stable for any magnitude of z."""
    return max(z, 0.0) - z * y + math.log1p(math.exp(-abs(z)))


class Standardiser:
    """Training mean/std per column, NaN policy included; applied identically twice.

    `fit(matrix)` learns the statistics from training rows; `transform(row)`
    turns one raw row into the design row described in the module docstring
    (without the intercept). Prediction rows go through the same object, so a
    holdout row can only ever be standardised with training statistics.
    """

    def __init__(self) -> None:
        self.means: list[float] = []
        self.stds: list[float] = []
        self.indicator_for: list[int] = []

    def fit(self, matrix: Sequence[Sequence[float]]) -> "Standardiser":
        n_columns = len(matrix[0]) if matrix else 0
        self.means, self.stds, self.indicator_for = [], [], []
        for j in range(n_columns):
            present = [row[j] for row in matrix if not math.isnan(row[j])]
            mean = math.fsum(present) / len(present) if present else 0.0
            var = (
                math.fsum((x - mean) ** 2 for x in present) / len(present)
                if present
                else 0.0
            )
            std = math.sqrt(var)
            self.means.append(mean)
            self.stds.append(std if std > 0.0 else 1.0)
            if len(present) < len(matrix):
                self.indicator_for.append(j)
        return self

    def transform(self, row: Sequence[float]) -> list[float]:
        out = []
        for j, x in enumerate(row):
            value = self.means[j] if math.isnan(x) else x
            out.append((value - self.means[j]) / self.stds[j])
        for j in self.indicator_for:
            out.append(1.0 if math.isnan(row[j]) else 0.0)
        return out

    def names(self, columns: Sequence[str]) -> list[str]:
        return list(columns) + [f"{columns[j]}:missing" for j in self.indicator_for]


class LogisticRegression(FittedModel):
    """L2-regularised logistic regression by full-batch gradient descent.

    `feature_sets` names bundles from `engine.features.FEATURE_SETS`; an empty
    tuple gives a history-only model that asks the harness for no frame at
    all. `l2` is the penalty in pseudo-observations (it is divided by the
    number of training rows, like the loss), `iters` and `lr` are the fixed
    step count and step size, and `shrinkage` is handed to the history
    feature.
    """

    name = "logistic"
    version = "1.0.0"

    def __init__(
        self,
        feature_sets: Sequence[str] = ("era5-antecedent", "terrain"),
        history: bool = True,
        l2: float = 1.0,
        iters: int = 400,
        lr: float = 0.1,
        shrinkage: float = 10.0,
    ) -> None:
        super().__init__()
        self.feature_sets = tuple(feature_sets)
        self.feature_specs = specs_for(self.feature_sets)
        self.history = history
        self.l2 = l2
        self.iters = iters
        self.lr = lr
        self.shrinkage = shrinkage
        self._history: HistoryFeatures | None = None
        self._scale = Standardiser()
        self.columns: list[str] = []
        self.weights: list[float] = []
        self.train_loss: float | None = None

    def _fit(self, view: TrainingView) -> None:
        self._history = (
            HistoryFeatures(self.shrinkage).fit(view.rows()) if self.history else None
        )
        raw_columns, raw, labels = training_matrix(view, self.feature_specs, self._history)
        self._scale.fit(raw)
        design = [self._scale.transform(row) + [1.0] for row in raw]
        self.columns = self._scale.names(raw_columns) + ["intercept"]
        self.weights = _gradient_descent(design, labels, self.l2, self.iters, self.lr)
        self.train_loss = math.fsum(
            log_loss(_dot(self.weights, x), y) for x, y in zip(design, labels)
        ) / len(labels)

    def _predict(self, request: PredictionRequest) -> Sequence[float]:
        raw = request_matrix(request, self.feature_specs, self._history)
        return [
            clip(sigmoid(_dot(self.weights, self._scale.transform(row) + [1.0])),
                 PROB_EPS, 1.0 - PROB_EPS)
            for row in raw
        ]


def _dot(w: Sequence[float], x: Sequence[float]) -> float:
    return math.fsum(wi * xi for wi, xi in zip(w, x))


def _gradient_descent(
    design: Sequence[Sequence[float]], labels: Sequence[int], l2: float, iters: int, lr: float
) -> list[float]:
    """Exactly `iters` steps from zero on the mean log-loss plus l2/(2n)·|w|².

    The last column is the intercept and is left out of the penalty. No line
    search, no early stopping: a fixed schedule is what makes the result a
    function of the data alone.
    """
    n = len(labels)
    k = len(design[0])
    w = [0.0] * k
    for _ in range(iters):
        residuals = [sigmoid(_dot(w, x)) - y for x, y in zip(design, labels)]
        # One exactly-rounded reduction per component, so the gradient is a
        # function of the residuals and nothing else — not of the order the
        # rows happen to arrive in, and not of the interpreter's `sum`.
        grad = [
            math.fsum(r * x[j] for r, x in zip(residuals, design)) for j in range(k)
        ]
        for j in range(k - 1):
            grad[j] += l2 * w[j]
        for j in range(k):
            w[j] -= lr * grad[j] / n
    return w
