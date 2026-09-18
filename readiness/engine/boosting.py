"""Histogram gradient boosting for the log-loss, deterministic by construction.

The second Phase 1 candidate: the one that can find a threshold ("more than
this much rain in the last quarter") where the logistic model can only find
a slope. Everything that makes a boosting library fast and non-reproducible
is left out — no row or column subsampling, no multithreaded histogram
building, no early stopping on a validation fold. What remains:

* Quantile bin edges per column from the training rows, ties resolved by
  value so the same data always gives the same edges. NaN sits in its own
  bin above every edge; a split can isolate it but never mixes it with a
  number.
* The initial score is the logit of the training base rate — or, when the
  model asks for history, each row's own history logit (leave-one-year-out
  for training rows, all training years for holdout units). History enters
  as the offset the trees boost from, never as a split column: a tree can
  carve a leave-one-year-out column back into its cells and read the label
  out of it, because within a cell every positive row sees a value exactly
  one count lower than its negative neighbours. That is the mirror image of
  the optimism the leave-one-year-out rule prevents in a linear model, and a
  greedy splitter finds it in a handful of rounds. As an offset the history
  is a prior the trees correct using only the covariates the harness built.
* Each round fits one depth-limited regression tree to the gradients: the
  best split of a node is the (column, bin) with the greatest gain, columns
  and bins scanned in ascending order and a tie kept with the earlier one,
  so the tree is a function of the histograms alone. Leaves take the Newton
  value −Σg / (Σh + 1).
* Predict sums the leaf values and passes the total through the sigmoid.
* Every total over floats — a node's gradient and Hessian sums, a histogram
  bin's, and the leaf values a prediction adds up — is `math.fsum`, so the
  same rows give the same split, the same leaf and the same probability on
  CPython 3.10 and 3.12 (whose builtin `sum` over floats differ). The gain
  reads the bin totals the histogram built, so the split a node takes and the
  value its leaves carry are two views of one reduction, not two.

Speed is not the goal; a few thousand rows fit in seconds, and the
orchestrator caps national-scale runs elsewhere.
"""

from __future__ import annotations

import bisect
import math
from typing import Sequence

from readiness.engine.base import FittedModel
from readiness.engine.features import request_matrix, specs_for, training_matrix
from readiness.engine.history import HistoryFeatures, clip, logit
from readiness.engine.linear import PROB_EPS, sigmoid
from readiness.harness.splits import PredictionRequest, TrainingView

__all__ = ["GradientBoosting", "Binner"]

#: Ridge on the Hessian sum in every leaf and gain: −Σg / (Σh + LAMBDA).
LAMBDA = 1.0


class Binner:
    """Quantile bin edges per column; NaN goes to the bin above every edge."""

    def __init__(self, bins: int) -> None:
        self.bins = bins
        self.edges: list[list[float]] = []

    def fit(self, matrix: Sequence[Sequence[float]]) -> "Binner":
        n_columns = len(matrix[0]) if matrix else 0
        self.edges = [self._edges([row[j] for row in matrix]) for j in range(n_columns)]
        return self

    def _edges(self, values: Sequence[float]) -> list[float]:
        present = sorted(v for v in values if not math.isnan(v))
        if not present:
            return []
        edges: list[float] = []
        for k in range(1, self.bins):
            cut = present[min(len(present) - 1, (k * len(present)) // self.bins)]
            if not edges or cut > edges[-1]:
                edges.append(cut)
        return edges

    def bin(self, j: int, value: float) -> int:
        """Bin index in [0, len(edges)] for a number, len(edges) + 1 for NaN."""
        if math.isnan(value):
            return len(self.edges[j]) + 1
        return bisect.bisect_left(self.edges[j], value)

    def n_bins(self, j: int) -> int:
        return len(self.edges[j]) + 2

    def transform(self, row: Sequence[float]) -> list[int]:
        return [self.bin(j, v) for j, v in enumerate(row)]


class GradientBoosting(FittedModel):
    """Boosted depth-limited trees on histogram bins, no subsampling, no RNG.

    `rounds` trees of at most `depth` levels, each scaled by `lr`; a node
    needs `min_leaf` rows on each side to split. `bins` is the number of
    quantile bins per column. `feature_sets` and `shrinkage` mean what they
    mean for `LogisticRegression`; `history` makes the seasonal-rate logit
    the boosting offset (see the module docstring for why not a column). A
    model with no feature sets and history is the seasonal climatology with
    no trees to grow; one with neither is the pooled rate.
    """

    name = "gbm"
    version = "1.0.0"

    def __init__(
        self,
        feature_sets: Sequence[str] = ("era5-antecedent", "terrain"),
        history: bool = True,
        rounds: int = 150,
        depth: int = 2,
        lr: float = 0.1,
        bins: int = 32,
        min_leaf: int = 20,
        shrinkage: float = 10.0,
    ) -> None:
        super().__init__()
        self.feature_sets = tuple(feature_sets)
        self.feature_specs = specs_for(self.feature_sets)
        self.history = history
        self.rounds = rounds
        self.depth = depth
        self.lr = lr
        self.bins = bins
        self.min_leaf = min_leaf
        self.shrinkage = shrinkage
        self._history: HistoryFeatures | None = None
        self._binner = Binner(bins)
        self.columns: list[str] = []
        self.base_score = 0.0
        self.trees: list = []

    def _fit(self, view: TrainingView) -> None:
        self._history = (
            HistoryFeatures(self.shrinkage).fit(view.rows()) if self.history else None
        )
        self.columns, raw, labels = training_matrix(view, self.feature_specs, None)
        self._binner.fit(raw)
        binned = [self._binner.transform(row) for row in raw]
        # Labels are integers, so this one builtin sum is exact everywhere.
        rate = clip(sum(labels) / len(labels), PROB_EPS, 1.0 - PROB_EPS)
        self.base_score = logit(rate)
        scores = self._offsets(view.units(), in_sample=True)
        self.trees = []
        for _ in range(self.rounds):
            probs = [sigmoid(s) for s in scores]
            grad = [p - y for p, y in zip(probs, labels)]
            hess = [p * (1.0 - p) for p in probs]
            tree = self._grow(binned, grad, hess, list(range(len(labels))), self.depth)
            self.trees.append(tree)
            for i, row in enumerate(binned):
                scores[i] += self.lr * _leaf_value(tree, row)

    def _grow(self, binned, grad, hess, rows: list[int], depth: int):
        """A node: a leaf value, or (column, bin, left, right) with bins <= bin left."""
        g = math.fsum(grad[i] for i in rows)
        h = math.fsum(hess[i] for i in rows)
        leaf = -g / (h + LAMBDA)
        if depth == 0 or len(rows) < 2 * self.min_leaf:
            return leaf
        split = self._best_split(binned, grad, hess, rows, g, h)
        if split is None:
            return leaf
        column, threshold = split
        left = [i for i in rows if binned[i][column] <= threshold]
        right = [i for i in rows if binned[i][column] > threshold]
        return (
            column,
            threshold,
            self._grow(binned, grad, hess, left, depth - 1),
            self._grow(binned, grad, hess, right, depth - 1),
        )

    def _best_split(self, binned, grad, hess, rows, g_total, h_total):
        """Greatest-gain (column, bin) scanning columns then bins in ascending order.

        A strictly greater gain replaces the incumbent, so ties stay with the
        lowest column index and, within it, the lowest bin.
        """
        parent = g_total * g_total / (h_total + LAMBDA)
        best, best_gain = None, 0.0
        for j in range(len(self.columns)):
            n_bins = self._binner.n_bins(j)
            buckets: list[list[int]] = [[] for _ in range(n_bins)]
            for i in rows:
                buckets[binned[i][j]].append(i)
            # The same arithmetic as the node totals above: one exactly-rounded
            # reduction over the rows a bin holds, and then over the bins on
            # each side of the candidate split. Nothing accumulates across
            # iterations, so no side of the gain depends on a running order.
            g_bin = [math.fsum(grad[i] for i in bucket) for bucket in buckets]
            h_bin = [math.fsum(hess[i] for i in bucket) for bucket in buckets]
            n_left = 0
            for b in range(n_bins - 1):
                n_left += len(buckets[b])
                if n_left < self.min_leaf or len(rows) - n_left < self.min_leaf:
                    continue
                g_left, h_left = math.fsum(g_bin[: b + 1]), math.fsum(h_bin[: b + 1])
                g_right = math.fsum(g_bin[b + 1 :])
                h_right = math.fsum(h_bin[b + 1 :])
                gain = (
                    g_left * g_left / (h_left + LAMBDA)
                    + g_right * g_right / (h_right + LAMBDA)
                    - parent
                )
                if gain > best_gain:
                    best, best_gain = (j, b), gain
        return best

    def _offsets(self, units: Sequence, in_sample: bool) -> list[float]:
        """Where boosting starts for each unit: the base rate, or its history logit."""
        if self._history is None:
            return [self.base_score] * len(units)
        return [self._history.logit(unit, in_sample) for unit in units]

    def _score(self, offset: float, row: Sequence[float]) -> float:
        binned = self._binner.transform(row)
        return offset + self.lr * math.fsum(
            _leaf_value(t, binned) for t in self.trees
        )

    def _predict(self, request: PredictionRequest) -> Sequence[float]:
        raw = request_matrix(request, self.feature_specs, None)
        offsets = self._offsets(request.units, in_sample=False)
        return [
            clip(sigmoid(self._score(offset, row)), PROB_EPS, 1.0 - PROB_EPS)
            for offset, row in zip(offsets, raw)
        ]


def _leaf_value(node, binned_row: Sequence[int]) -> float:
    while not isinstance(node, float):
        column, threshold, left, right = node
        node = left if binned_row[column] <= threshold else right
    return node
