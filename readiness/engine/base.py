"""What every proposable model shares, so that no model has to get it right alone.

Report §5: "The agent proposes; the harness disposes." A model's side of that
bargain is small but easy to fumble: it must fit through the `TrainingView`
and nothing else, refuse to predict before it has been fitted, and declare
the digest of the data it was fitted on so the canary's provenance check can
compare it with what the harness actually handed over.

Before this class existed each baseline carried its own copy of that
boilerplate, and a model that simply forgot `training_digest` weakened the
canary silently — the provenance check used to be skipped for it. Owning the
digest here means a model built on this class declares it for free, and the
canary can now treat an undeclared digest as a trip rather than a shrug.

Subclasses set `name` and `version` and implement two hooks:

    _fit(view)         learn whatever the model learns from training rows
    _predict(request)  one probability per unit, in request order

Everything else — the guard, the digest, the public `fit`/`predict` the
harness calls — lives here and is not meant to be overridden.
"""

from __future__ import annotations

from typing import Sequence

from readiness.harness.splits import PredictionRequest, TrainingView


class FittedModel:
    """Base class for a model the harness can fit and score.

    `training_digest` is None until `fit()` has run, and is then the digest of
    the view the harness exposed. `predict()` refuses to run before that.
    """

    #: Registry name and semantic version. Set by every subclass.
    name: str = ""
    version: str = ""

    def __init__(self) -> None:
        self.training_digest: str | None = None

    def fit(self, view: TrainingView) -> None:
        """Fit through the training view, then record what was fitted on."""
        self._fit(view)
        self.training_digest = view.digest

    def predict(self, request: PredictionRequest) -> Sequence[float]:
        """Forecast the request's bare units; only after `fit()`."""
        if self.training_digest is None:
            raise RuntimeError(f"{self.name} was not fitted")
        return self._predict(request)

    # -- hooks ----------------------------------------------------------------

    def _fit(self, view: TrainingView) -> None:
        raise NotImplementedError(f"{type(self).__name__} must implement _fit")

    def _predict(self, request: PredictionRequest) -> Sequence[float]:
        raise NotImplementedError(f"{type(self).__name__} must implement _predict")
