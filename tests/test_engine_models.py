"""The Phase 1 candidates: deterministic, train-only, and able to find planted signal.

The sources here are synthetic. `SignalSeries` hands the harness a seeded
monthly precipitation and temperature series per region; the planted-signal
panel from `tests.fixtures` draws its labels from the trailing three-month
precipitation the harness computes for each unit, so a feature model has a
real relationship to learn and the tests can ask for skill, not just for
"runs without error".
"""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
import random
import unittest

from readiness.engine import FEATURE_SETS, REGISTRY, build_model, specs_for
from readiness.engine.baseline import ClimatologySeasonal, seasonal_rates
from readiness.engine.boosting import GradientBoosting
from readiness.engine.calibrate import Calibrated, IsotonicMap, PlattMap
from readiness.engine.history import HistoryFeatures
from readiness.engine.linear import LogisticRegression
from readiness.harness import features as F
from readiness.harness import scoring
from readiness.harness.splits import PredictionRequest, TrainingView, split_panel
from tests.fixtures import make_contract, make_panel, make_signal_panel
from tests.test_features import FakeSeries, FakeStatic


class SignalSeries:
    """Seeded monthly precipitation and temperature per region, 1985 onward."""

    name = "era5"
    kind = "series"
    manifest_keys = ("open-meteo/era5/99",)
    derived_through = None
    global_coverage = True

    def __init__(self, seed: int = 7, missing: frozenset[int] = frozenset()) -> None:
        self.seed = seed
        self.missing = missing
        self.start = F.month_index(1985, 1)
        self.end = F.month_index(2026, 1)
        self._cache: dict[tuple[str, str], F.Series] = {}

    def series(self, region: str, variable: str) -> F.Series | None:
        if variable not in ("precip_mm", "tmean_c"):
            return None
        key = (region, variable)
        if key not in self._cache:
            rng = random.Random(f"{self.seed}:{region}:{variable}")
            values = []
            for m in range(self.start, self.end):
                if variable == "precip_mm":
                    v = rng.lognormvariate(4.0, 0.6)
                else:
                    v = 15.0 + 10.0 * math.cos(2 * math.pi * ((m % 12) - 6) / 12) + rng.gauss(0, 1)
                values.append(F.NAN if m in self.missing else v)
            self._cache[key] = F.Series(region, self.start, tuple(values))
        return self._cache[key]

    def static(self, region: str):
        return None


class FakeElevation(FakeStatic):
    name = "elevation"
    manifest_keys = ("open-meteo/era5/99",)

    def __init__(self) -> None:
        super().__init__("elevation")

    def static(self, region: str):
        return {"elevation_m": 10.0 + int(region[-3:])}


def sources(series: SignalSeries | None = None) -> dict:
    return {
        "era5": series or SignalSeries(),
        "elevation": FakeElevation(),
        "gazetteer": FakeStatic(),
    }


#: Fewer steps than the registry default, so the suite stays quick; the
#: properties under test do not depend on the step count.
QUICK = {"iters": 60}
QUICK_GBM = {"rounds": 40}

# ---------------------------------------------------------------------------
# The cross-interpreter fingerprint
# ---------------------------------------------------------------------------

#: The blessed probabilities and feature digests of the four Phase 1 models.
#: Written by `phase1_fingerprints()` under CPython 3.12 *and* 3.10, which
#: produced identical JSON: that is the whole point of the file. Every float
#: reduction in the feature channel and in the models is `math.fsum`, so a
#: probability is a function of the data and not of which interpreter added
#: up a twelve-month window (the builtin `sum` over floats compensates on
#: 3.12 and folds left on 3.10). A diff here means either the arithmetic
#: moved or it stopped being interpreter-independent; both are findings.
FINGERPRINTS = pathlib.Path(__file__).resolve().parent / "expected" / "phase1_fingerprints.json"

#: Reduced iteration counts, so the whole comparison fits in a few seconds.
FINGERPRINT_MODELS: dict[str, dict] = {
    "logistic": {"iters": 40},
    "logistic+iso": {"iters": 40},
    "gbm": {"rounds": 20},
    "gbm+iso": {"rounds": 20},
}
FINGERPRINT_REGIONS = 8


def fingerprint_sources() -> dict:
    """The fixture sources: one series source and two static ones, no real data."""
    return {
        "era5": FakeSeries(),
        "gazetteer": FakeStatic(),
        "elevation": FakeStatic("elevation"),
    }


def probs_hash(probs, figures: int = 12) -> str:
    """The forecast vector at `figures` significant figures, hashed."""
    blob = ",".join(f"{p:.{figures}g}" for p in probs)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def phase1_fingerprints() -> dict:
    """Probabilities and feature digest per Phase 1 model, on the fixture panel.

    Run under two interpreters this is the proof that the models are
    interpreter-independent; run under one it is the guard that keeps them so.

    A calibrated model also records its isotonic map. On this fixture the
    planted signal is a time trend, so within the three late training years
    the logistic has nothing left to rank by and pool-adjacent-violators
    collapses its map to one block: the forecasts come out constant. The
    block's centre is still the mean of the inner model's forecasts, so the
    map — unlike the constant it produces — moves with the inner arithmetic,
    which is what makes this entry worth blessing.
    """
    contract = make_contract()
    sources = fingerprint_sources()
    panel = make_signal_panel(contract, sources["era5"], n_regions=FINGERPRINT_REGIONS)
    out: dict = {
        "_note": (
            "Blessed from identical output under CPython 3.10 and 3.12. Hashes "
            "are over probabilities rendered at 12 significant figures; the "
            "feature digest is exact."
        ),
        "_contract": contract.digest(),
        "_panel_digest": panel.digest(),
        "_n_regions": FINGERPRINT_REGIONS,
        "_sources": sorted(sources),
    }
    for name, kwargs in FINGERPRINT_MODELS.items():
        model = build_model(name, **kwargs)
        _units, probs, _outcomes = scoring.predictions_for(
            model, panel, contract, "validate", sources=sources
        )
        entry = {
            "kwargs": dict(kwargs),
            "n_probs": len(probs),
            "n_distinct": len(set(probs)),
            "probs_sha256_12sf": probs_hash(probs),
            "feature_digest": model.feature_digest,
        }
        fitted_map = getattr(model, "map", None)
        if fitted_map is not None:
            entry["map_blocks"] = len(fitted_map.centres)
            entry["map_sha256_12sf"] = probs_hash(
                list(fitted_map.centres) + list(fitted_map.values)
            )
        out[name] = entry
    return out


class TestFeatureSets(unittest.TestCase):
    def test_specs_for_concatenates_in_order_and_rejects_unknown_names(self):
        specs = specs_for(["terrain", "era5-antecedent"])
        self.assertEqual(specs, FEATURE_SETS["terrain"] + FEATURE_SETS["era5-antecedent"])
        self.assertEqual(specs_for([]), ())
        with self.assertRaises(KeyError):
            specs_for(["gut-feeling"])

    def test_column_names_are_unique_across_every_set(self):
        columns = [s.column for s in specs_for(list(FEATURE_SETS))]
        self.assertEqual(len(columns), len(set(columns)))

    def test_every_series_spec_lags_one_month_and_names_era5(self):
        for spec in FEATURE_SETS["era5-antecedent"]:
            self.assertEqual(spec.source, "era5")
            self.assertEqual(spec.lag_months, 1)
        for name in ("terrain", "nri", "climada-prior"):
            self.assertTrue(all(s.is_static for s in FEATURE_SETS[name]), name)


class TestSeasonalRates(unittest.TestCase):
    def test_seasonal_rates_reproduces_climatology_seasonal_exactly(self):
        c = make_contract()
        panel = make_panel(contract=c, n_regions=6)
        view = TrainingView(split_panel(panel, c.splits.train), c.splits.train)
        model = ClimatologySeasonal()
        model.fit(view)
        pooled, by_period, by_cell = seasonal_rates(view.rows(), 10.0)
        self.assertEqual(pooled, model.pooled_rate)
        self.assertEqual(by_period, model.by_period)
        self.assertEqual(by_cell, model.by_region_period)
        request = PredictionRequest.from_panel(
            split_panel(panel, c.splits.validate), c.splits.validate
        )
        history = HistoryFeatures(10.0).fit(view.rows())
        self.assertEqual(
            list(model.predict(request)),
            [history.rate(u, in_sample=False) for u in request],
        )


class TestHistory(unittest.TestCase):
    def rows(self):
        rows = []
        for year in range(2000, 2010):
            rows.append((("A", year, 1), 1 if year == 2005 else 0))
            rows.append((("B", year, 1), 0))
        return rows

    def test_history_uses_leave_one_year_out_for_training_rows(self):
        history = HistoryFeatures(10.0).fit(self.rows())
        holdout = history.logit(("A", 2016, 1), in_sample=False)
        # The one positive year, seen without itself: no positive is left in
        # its cell, its period or the pool, so the rate is far lower.
        self.assertLess(history.logit(("A", 2005, 1), in_sample=True), holdout)
        # A negative year, seen without itself: slightly higher than holdout.
        self.assertGreater(history.logit(("A", 2001, 1), in_sample=True), holdout)
        # A row the history never saw cannot be treated as in-sample.
        with self.assertRaises(ValueError):
            history.logit(("A", 2016, 1), in_sample=True)

    def test_leave_one_year_out_drops_the_whole_year_from_every_level(self):
        # Region B never has an event; region A has one, in 2005. B's own 2005
        # row must not be able to see it — not through the pooled rate and not
        # through the seasonal one. Leaving out only the row itself let the
        # year's event in through both, which is the optimism the rule exists
        # to stop.
        history = HistoryFeatures(10.0).fit(self.rows())
        same_year = history.rate(("B", 2005, 1), in_sample=True)
        other_year = history.rate(("B", 2001, 1), in_sample=True)
        self.assertEqual(same_year, 0.0)
        self.assertGreater(other_year, 0.0)
        # A's own year is gone from its cell as well, and the holdout path
        # still sees every training year.
        self.assertEqual(history.rate(("A", 2005, 1), in_sample=True), 0.0)
        self.assertGreater(history.rate(("B", 2016, 1), in_sample=False), 0.0)

    def test_logits_are_finite_even_for_all_negative_cells(self):
        history = HistoryFeatures(10.0).fit([(("A", y, 1), 0) for y in range(2000, 2010)])
        for unit, in_sample in ((("A", 2003, 1), True), (("A", 2016, 1), False)):
            self.assertTrue(math.isfinite(history.logit(unit, in_sample)))

    def test_unseen_cells_back_off_like_the_baseline(self):
        history = HistoryFeatures(10.0).fit(self.rows())
        self.assertEqual(history.rate(("Z", 2016, 1), False), history.by_period[1])
        self.assertEqual(history.rate(("Z", 2016, 4), False), history.pooled_rate)


class TestDeterminism(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = make_contract()
        cls.series = SignalSeries(missing=frozenset({F.month_index(2003, 5)}))
        cls.panel = make_signal_panel(cls.c, cls.series, n_regions=8)
        cls.sources = sources(cls.series)

    def probs(self, model):
        _u, probs, _o = scoring.predictions_for(
            model, self.panel, self.c, "validate", sources=self.sources
        )
        return probs

    def assert_deterministic(self, factory):
        first = self.probs(factory())
        second = self.probs(factory())
        self.assertEqual(first, second)
        # Request order must not matter either: fit once more, then ask for
        # the validate units back to front.
        model = factory()
        forward = self.probs(model)
        units = split_panel(self.panel, self.c.splits.validate).units
        frame = F.build_frame(
            model.feature_specs, self.sources, units, self.c.periods_per_year
        )
        reversed_request = PredictionRequest(
            tuple(reversed(units)), "validate", frame.restrict(units)
        )
        backward = list(model.predict(reversed_request))
        self.assertEqual(forward, list(reversed(backward)))
        return first

    def test_logistic_is_deterministic(self):
        probs = self.assert_deterministic(lambda: LogisticRegression(**QUICK))
        self.assertTrue(all(0.0 < p < 1.0 for p in probs))
        self.assertGreater(len(set(probs)), 1)

    def test_gbm_is_deterministic_and_bounded(self):
        probs = self.assert_deterministic(lambda: GradientBoosting(**QUICK_GBM))
        self.assertTrue(all(1e-6 <= p <= 1 - 1e-6 for p in probs))
        self.assertGreater(len(set(probs)), 1)

    def test_a_missing_month_becomes_an_indicator_column_not_a_zero(self):
        gappy = LogisticRegression(feature_sets=("era5-antecedent",), history=False, **QUICK)
        self.probs(gappy)
        # May 2003 is missing from both variables, so every window that spans
        # it is NaN in training and every column earns an indicator.
        self.assertIn("precip_1m:missing", gappy.columns)
        self.assertEqual(gappy.columns[-1], "intercept")
        self.assertEqual(len(gappy.weights), len(gappy.columns))
        self.assertTrue(all(math.isfinite(w) for w in gappy.weights))

        complete = LogisticRegression(feature_sets=("era5-antecedent",), history=False, **QUICK)
        scoring.predictions_for(
            complete, make_signal_panel(self.c, SignalSeries(), n_regions=4), self.c,
            "validate", sources=sources(SignalSeries()),
        )
        self.assertFalse([c for c in complete.columns if c.endswith(":missing")])


class TestSkill(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = make_contract()
        cls.series = SignalSeries()
        cls.panel = make_signal_panel(cls.c, cls.series, n_regions=12)
        cls.sources = sources(cls.series)

    def test_feature_models_beat_pooled_on_planted_signal(self):
        for model in (LogisticRegression(**QUICK), GradientBoosting(**QUICK_GBM)):
            with self.subTest(model=model.name):
                card = scoring.score(
                    model, self.panel, self.c, "validate", sources=self.sources
                )
                self.assertGreater(card.brier_skill_score, 0.0, card.format())
                self.assertGreater(card.auc, 0.6, card.format())
                self.assertEqual(len(card.feature_digest), 16)
                self.assertTrue(card.feature_audit["clean"])

    def test_history_only_logistic_needs_no_sources(self):
        model = LogisticRegression(feature_sets=(), **QUICK)
        self.assertEqual(model.feature_specs, ())
        card = scoring.score(model, self.panel, self.c, "validate")
        self.assertEqual(card.feature_digest, "")
        self.assertEqual(model.columns, ["history", "intercept"])
        self.assertTrue(all(math.isfinite(w) for w in model.weights))

    def test_gbm_uses_history_as_its_offset_not_a_split_column(self):
        # A leave-one-year-out column is readable by a tree (module docstring
        # of `boosting.py`), so the history is where boosting starts, not a
        # column it may split on: with no feature sets the model is the
        # seasonal climatology to within rounding.
        panel = make_panel(contract=self.c, n_regions=8)
        gbm = GradientBoosting(feature_sets=(), **QUICK_GBM)
        _u, boosted, _o = scoring.predictions_for(gbm, panel, self.c, "validate")
        _u, seasonal, _o = scoring.predictions_for(
            ClimatologySeasonal(), panel, self.c, "validate"
        )
        self.assertEqual(gbm.columns, [])
        for a, b in zip(boosted, seasonal):
            self.assertAlmostEqual(a, b, places=12)
        with_features = GradientBoosting(**QUICK_GBM)
        scoring.predictions_for(
            with_features, self.panel, self.c, "validate", sources=self.sources
        )
        self.assertNotIn("history", with_features.columns)

    def test_screen_passes_the_feature_provenance_check(self):
        card, report = scoring.screen(
            GradientBoosting(**QUICK_GBM), self.panel, self.c, "validate",
            sources=self.sources,
        )
        provenance = [f for f in report.findings if f.check == "feature provenance"][0]
        self.assertFalse(provenance.tripped, provenance.detail)


class SpyInner(ClimatologySeasonal):
    """A seasonal climatology that records every view and request it is handed."""

    name = "spy"

    def __init__(self) -> None:
        super().__init__()
        self.fit_years: list[tuple[int, ...]] = []
        self.request_years: list[tuple[int, ...]] = []
        self.view_types: list[type] = []

    def _fit(self, view):
        self.view_types.append(type(view))
        self.fit_years.append(tuple(sorted({u[1] for u in view.units()})))
        super()._fit(view)

    def _predict(self, request):
        self.request_years.append(tuple(sorted({u[1] for u in request})))
        return super()._predict(request)


class TestCalibrated(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.panel = make_panel(contract=self.c, n_regions=6)
        train = self.c.splits.train
        self.view = TrainingView(split_panel(self.panel, train), train)
        self.request = PredictionRequest.from_panel(
            split_panel(self.panel, self.c.splits.validate), self.c.splits.validate
        )

    def test_calibrated_only_constructs_train_only_views(self):
        spy = SpyInner()
        model = Calibrated(spy, "isotonic", holdout_years=3)
        model.fit(self.view)
        train_years = tuple(self.c.train_years)
        self.assertEqual(spy.view_types, [TrainingView, TrainingView])
        self.assertEqual(spy.fit_years, [train_years[:-3], train_years])
        self.assertEqual(spy.request_years[0], train_years[-3:])
        for years in spy.fit_years + spy.request_years[:1]:
            self.assertTrue(set(years) <= set(train_years), years)
        self.assertEqual(model.name, "spy+iso")
        self.assertEqual(model.version, spy.version)
        self.assertEqual(model.training_digest, self.view.digest)

    def test_calibrated_output_is_monotone_in_the_inner_forecast(self):
        for method in ("isotonic", "platt"):
            with self.subTest(method=method):
                inner = ClimatologySeasonal()
                model = Calibrated(inner, method)
                model.fit(self.view)
                raw = list(inner.predict(self.request))
                mapped = list(model.predict(self.request))
                pairs = sorted(zip(raw, mapped))
                for (_x0, y0), (_x1, y1) in zip(pairs, pairs[1:]):
                    self.assertLessEqual(y0, y1)
                self.assertTrue(all(0.0 <= y <= 1.0 for y in mapped))

    def test_isotonic_pools_ties_and_violators(self):
        m = IsotonicMap().fit([0.1, 0.2, 0.2, 0.3, 0.4], [0, 1, 0, 0, 1])
        self.assertEqual(m.values, [0.0, 1 / 3, 1.0])
        self.assertEqual(m(0.0), 0.0)
        self.assertEqual(m(1.0), 1.0)
        self.assertEqual(m(0.4), 1.0)

    def test_platt_recovers_an_identity_map_on_calibrated_input(self):
        rng = random.Random(3)
        probs = [rng.random() for _ in range(2000)]
        labels = [1 if rng.random() < p else 0 for p in probs]
        m = PlattMap().fit(probs, labels)
        self.assertAlmostEqual(m.a, 1.0, delta=0.15)
        self.assertAlmostEqual(m.b, 0.0, delta=0.15)

    def test_too_few_training_years_is_refused(self):
        model = Calibrated(ClimatologySeasonal(), holdout_years=40)
        with self.assertRaises(ValueError):
            model.fit(self.view)
        with self.assertRaises(ValueError):
            Calibrated(ClimatologySeasonal(), method="wishful")

    def test_calibrated_feature_model_through_the_harness(self):
        series = SignalSeries()
        panel = make_signal_panel(self.c, series, n_regions=8)
        model = build_model("logistic+iso", **QUICK)
        card = scoring.score(model, panel, self.c, "validate", sources=sources(series))
        self.assertEqual(card.model, "logistic+iso")
        self.assertEqual(card.feature_columns, tuple(s.column for s in model.feature_specs))


class TestPhase1Fingerprints(unittest.TestCase):
    """The blessed file is the same under CPython 3.10 and 3.12, and stays so."""

    def test_probabilities_and_feature_digests_reproduce(self):
        blessed = json.loads(FINGERPRINTS.read_text())
        observed = phase1_fingerprints()
        self.assertEqual(set(observed), set(blessed))
        for key in sorted(blessed):
            with self.subTest(entry=key):
                self.assertEqual(
                    observed[key], blessed[key],
                    f"{key} moved: rebless only with a reason, and only after "
                    "running the generator under 3.10 and 3.12",
                )

    def test_the_uncalibrated_entries_are_not_vacuous(self):
        # A fingerprint over a constant vector would pass whatever the
        # arithmetic did; the two uncalibrated models must spread out.
        blessed = json.loads(FINGERPRINTS.read_text())
        for name in ("logistic", "gbm"):
            with self.subTest(model=name):
                self.assertGreater(blessed[name]["n_distinct"], 5)
        for name in ("logistic+iso", "gbm+iso"):
            with self.subTest(model=name):
                self.assertGreaterEqual(blessed[name]["map_blocks"], 1)

    def test_every_model_asked_the_harness_for_the_same_frame(self):
        # One frame, four models: a feature digest that differs between them
        # would mean a model is being handed rows the others are not.
        blessed = json.loads(FINGERPRINTS.read_text())
        digests = {blessed[name]["feature_digest"] for name in FINGERPRINT_MODELS}
        self.assertEqual(len(digests), 1, digests)
        self.assertEqual(len(digests.pop()), 16)


class TestRegistryEntries(unittest.TestCase):
    NEW = ("logistic", "logistic+iso", "gbm", "gbm+iso")

    def test_no_new_model_needs_a_panel(self):
        for name in self.NEW:
            with self.subTest(model=name):
                spec = REGISTRY[name]
                self.assertFalse(spec.is_canary_target)
                self.assertTrue(spec.needs_features)
                model = build_model(name)
                self.assertEqual(model.name, name)
                self.assertEqual(model.feature_specs, specs_for(["era5-antecedent", "terrain"]))

    def test_feature_sets_kwarg_accepts_a_list_and_keeps_a_tuple(self):
        model = build_model("gbm", feature_sets=["terrain"])
        self.assertEqual(model.feature_sets, ("terrain",))
        self.assertEqual(model.feature_specs, FEATURE_SETS["terrain"])
        wrapped = build_model("gbm+iso", feature_sets=[], holdout_years=2)
        self.assertEqual(wrapped.feature_specs, ())
        self.assertEqual(wrapped.holdout_years, 2)

    def test_describe_marks_the_feature_models(self):
        from readiness.engine import describe_registry

        text = describe_registry()
        self.assertIn("[needs features]", text)
        for name in self.NEW:
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
