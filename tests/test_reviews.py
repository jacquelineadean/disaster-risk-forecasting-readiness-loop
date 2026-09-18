"""Review records: the sha must match, the vocabulary is closed, nothing else is claimed."""

import pathlib
import tempfile
import unittest

from readiness.plans import gap_report as gap_report_mod
from readiness.plans import reviews as reviews_mod
from readiness.plans import rules as rules_mod
from readiness.plans.reviews import Review, ReviewError
from tests import fixtures_plans as fp

KWARGS = dict(
    rating="useful", reviewer_role="practising emergency manager",
    organisation_type="county", years_in_role=14,
)


class ReviewCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.reviews_dir = self.dir / "reviews"
        self.scenario = fp.scenario()
        self.risk = fp.make_risk()

    def tearDown(self):
        self.tmp.cleanup()

    def write_report(self, slug="test-facility", **overrides) -> pathlib.Path:
        facility = fp.make_facility(slug=slug, **overrides)
        findings = rules_mod.run(facility, self.risk, self.scenario)
        doc = gap_report_mod.build(
            facility, self.scenario, findings, self.risk, fp.PERIOD
        )
        resolve = gap_report_mod.resolver(facility, self.scenario, self.risk)
        _html, _json, blind = gap_report_mod.write(
            doc, self.dir / "reports", resolve=resolve
        )
        return blind


class TestRecording(ReviewCase):
    def test_the_sha_must_match_the_blinded_render(self):
        blind = self.write_report()
        path, review = reviews_mod.record(
            blind, reviews_dir=self.reviews_dir, **KWARGS
        )
        self.assertEqual(review.report_sha256, reviews_mod.sha256_of(blind))
        self.assertEqual(path.name, reviews_mod.file_name(review))
        self.assertTrue(path.name.startswith(review.report_sha256[:16] + "-"))
        self.assertTrue(review.blinded)

        # Change one byte of the report and the review no longer describes it.
        blind.write_text(
            blind.read_text(encoding="utf-8").replace("14 feet", "44 feet"),
            encoding="utf-8",
        )
        with self.assertRaises(ReviewError) as ctx:
            reviews_mod.record(
                blind, reviews_dir=self.reviews_dir,
                expected_sha256=review.report_sha256, **KWARGS
            )
        self.assertIn("the report changed after it was reviewed", str(ctx.exception))

    def test_it_takes_the_facility_label_and_period_from_the_page_itself(self):
        blind = self.write_report(slug="saint-example")
        _path, review = reviews_mod.record(blind, reviews_dir=self.reviews_dir, **KWARGS)
        self.assertEqual(
            review.facility_label, fp.make_facility(slug="saint-example").blind_label
        )
        self.assertEqual(review.period, fp.PERIOD)
        self.assertNotIn("saint-example", review.facility_label)
        self.assertRegex(review.facility_label, r"^FACILITY-[0-9a-f]{12}$")

    def test_a_page_of_another_kind_is_refused(self):
        page = self.write_report().read_text(encoding="utf-8")
        other = page.replace("|gap-report\"", "|county-brief\"")
        path = self.dir / "other.blind.html"
        path.write_text(other, encoding="utf-8")
        with self.assertRaises(ReviewError) as ctx:
            reviews_mod.record(path, reviews_dir=self.reviews_dir, **KWARGS)
        self.assertIn("county-brief", str(ctx.exception))
        self.assertIn("gap-report", str(ctx.exception))

    def test_a_directory_passed_as_the_report_is_a_refusal(self):
        # Finding 14: `record` checked `exists()` and then read unguarded, so
        # the CLI answered a mistyped path with an IsADirectoryError traceback.
        directory = self.write_report().parent
        with self.assertRaises(ReviewError) as ctx:
            reviews_mod.record(directory, reviews_dir=self.reviews_dir, **KWARGS)
        self.assertIn("cannot be read", str(ctx.exception))

    def test_it_refuses_a_page_that_is_not_a_blinded_render(self):
        plain = self.dir / "plain.html"
        plain.write_text("<html><body>looks fine</body></html>", encoding="utf-8")
        with self.assertRaises(ReviewError) as ctx:
            reviews_mod.record(plain, reviews_dir=self.reviews_dir, **KWARGS)
        self.assertIn("not a blinded render", str(ctx.exception))
        self.assertIn("blind.html", str(ctx.exception))

    def test_it_refuses_the_unblinded_page(self):
        blind = self.write_report()
        named = blind.parent / f"{fp.PERIOD}.html"
        with self.assertRaises(ReviewError):
            reviews_mod.record(named, reviews_dir=self.reviews_dir, **KWARGS)

    def test_it_refuses_a_file_that_is_not_there(self):
        with self.assertRaises(ReviewError) as ctx:
            reviews_mod.record(
                self.dir / "nope.blind.html", reviews_dir=self.reviews_dir, **KWARGS
            )
        self.assertIn("no such blinded report", str(ctx.exception))

    def test_the_record_round_trips_through_disk(self):
        blind = self.write_report()
        path, review = reviews_mod.record(
            blind, comments="clear and usable", reviews_dir=self.reviews_dir, **KWARGS
        )
        again = Review.read(path)
        self.assertEqual(again, review)
        self.assertEqual(again.comments, "clear and usable")
        self.assertTrue(again.recorded_at)


class TestVocabulary(unittest.TestCase):
    def base(self, **overrides):
        raw = {
            "report_sha256": "a" * 64,
            "facility_label": "FACILITY-abc123def456",
            "period": "2026-Q4", "reviewer_role": "practising emergency manager",
            "organisation_type": "hospital", "years_in_role": 9, "rating": "useful",
        }
        raw.update(overrides)
        return raw

    def test_rating_vocabulary_is_closed(self):
        self.assertEqual(
            reviews_mod.RATINGS,
            ("not useful", "somewhat useful", "useful", "very useful"),
        )
        for rating in reviews_mod.RATINGS:
            Review.from_dict(self.base(rating=rating))
        for rating in ("brilliant", "USEFUL", "", "5/5"):
            with self.subTest(rating=rating), self.assertRaises(ReviewError):
                Review.from_dict(self.base(rating=rating))

    def test_useful_or_better_is_the_top_two_rungs(self):
        self.assertEqual(reviews_mod.USEFUL_OR_BETTER, {"useful", "very useful"})
        self.assertTrue(Review.from_dict(self.base(rating="very useful")).useful)
        self.assertFalse(Review.from_dict(self.base(rating="somewhat useful")).useful)

    def test_organisation_type_is_closed(self):
        for org in reviews_mod.ORG_TYPES:
            Review.from_dict(self.base(organisation_type=org))
        with self.assertRaises(ReviewError):
            Review.from_dict(self.base(organisation_type="consultancy"))

    def test_the_role_is_matched_case_insensitively_as_a_substring(self):
        for role, expected in (
            ("practising emergency manager", True),
            ("County Emergency Manager, 20 years", True),
            ("EMERGENCY MANAGER", True),
            ("hospital administrator", False),
            ("emergency physician", False),
        ):
            with self.subTest(role=role):
                self.assertEqual(
                    Review.from_dict(self.base(reviewer_role=role)).by_emergency_manager,
                    expected,
                )

    def test_a_review_counts_only_when_blinded_useful_and_by_a_manager(self):
        self.assertTrue(Review.from_dict(self.base()).counts)
        self.assertFalse(Review.from_dict(self.base(blinded=False)).counts)
        self.assertFalse(Review.from_dict(self.base(rating="not useful")).counts)
        self.assertFalse(
            Review.from_dict(self.base(reviewer_role="architect")).counts
        )

    def test_a_malformed_sha_is_refused(self):
        for sha in ("", "abc", "z" * 64, "A" * 64, "a" * 65, "a" * 128, "a" * 63):
            with self.subTest(sha=sha), self.assertRaises(ReviewError):
                Review.from_dict(self.base(report_sha256=sha))

    def test_a_malformed_facility_label_is_refused(self):
        # Finding 12: the field was an unvalidated string that
        # `distinct_facilities` de-duplicated the exit criterion on.
        for label in ("", "FACILITY-abc123", "facility-abc123def456",
                      "FACILITY-ABC123DEF456", "FACILITY-abc123def4567",
                      "abc123def456", "saint-example"):
            with self.subTest(label=label), self.assertRaises(ReviewError):
                Review.from_dict(self.base(facility_label=label))

    def test_a_top_level_that_is_not_an_object_is_refused(self):
        for payload in (None, [], "a review", 3):
            with self.subTest(payload=payload), self.assertRaises(ReviewError):
                Review.from_dict(payload, where="r.json")

    def test_a_mis_typed_year_count_is_a_refusal_not_a_type_error(self):
        with self.assertRaises(ReviewError) as ctx:
            Review.from_dict(self.base(years_in_role="ten years"))
        self.assertIn("years_in_role", str(ctx.exception))

    def test_years_must_be_a_whole_number(self):
        with self.assertRaises(ReviewError):
            Review.from_dict(self.base(years_in_role=-1))
        # `from_dict` coerces what a JSON file holds; the constructor is where
        # a caller passing a flag instead of a count is caught.
        for years in (True, 2.5, "ten"):
            with self.subTest(years=years), self.assertRaises(ReviewError):
                Review(**{**self.base(), "years_in_role": years})

    def test_a_missing_field_is_named(self):
        raw = self.base()
        del raw["period"]
        with self.assertRaises(ReviewError) as ctx:
            Review.from_dict(raw, where="r.json")
        self.assertIn("period", str(ctx.exception))


class TestLoading(ReviewCase):
    def test_load_all_reads_every_record(self):
        for slug in ("alpha-ridge", "bravo-ridge", "charlie-ridge"):
            reviews_mod.record(
                self.write_report(slug=slug), reviews_dir=self.reviews_dir, **KWARGS
            )
        loaded = reviews_mod.load_all(self.reviews_dir)
        self.assertEqual(len(loaded), 3)
        self.assertEqual(len(reviews_mod.distinct_facilities(loaded)), 3)
        self.assertEqual(len(reviews_mod.counting(loaded)), 3)

    def test_an_empty_or_missing_directory_is_no_reviews(self):
        self.assertEqual(reviews_mod.load_all(self.dir / "nothing"), [])

    def test_an_unreadable_record_is_refused_with_its_path(self):
        self.reviews_dir.mkdir(parents=True)
        (self.reviews_dir / "broken.json").write_text("{", encoding="utf-8")
        with self.assertRaises(ReviewError) as ctx:
            reviews_mod.load_all(self.reviews_dir)
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_two_reviews_of_one_report_coexist(self):
        # Finding 6: naming the file after the report's sha alone meant the
        # second emergency manager to review a report destroyed the first.
        blind = self.write_report()
        first, review_a = reviews_mod.record(
            blind, reviews_dir=self.reviews_dir, **KWARGS
        )
        second, review_b = reviews_mod.record(
            blind, reviews_dir=self.reviews_dir,
            **{**KWARGS, "rating": "not useful",
               "reviewer_role": "practising emergency manager B"},
        )
        self.assertNotEqual(first, second)
        self.assertEqual(review_a.report_sha256, review_b.report_sha256)
        ratings = {r.rating for r in reviews_mod.load_all(self.reviews_dir)}
        self.assertEqual(ratings, {"useful", "not useful"})

    def test_recording_the_same_attestation_twice_is_idempotent(self):
        blind = self.write_report()
        first, review_a = reviews_mod.record(
            blind, reviews_dir=self.reviews_dir, **KWARGS
        )
        before = first.read_text(encoding="utf-8")
        second, review_b = reviews_mod.record(
            blind, reviews_dir=self.reviews_dir, recorded_at="2099-01-01T00:00:00+00:00",
            **KWARGS
        )
        self.assertEqual(first, second)
        self.assertEqual(second.read_text(encoding="utf-8"), before)
        self.assertEqual(review_a, review_b)
        self.assertEqual(len(reviews_mod.load_all(self.reviews_dir)), 1)


class TestNothingIsCommitted(unittest.TestCase):
    def test_the_repository_ships_no_review(self):
        # `git ls-files`, not a glob over the working tree: the glob misses a
        # record in a subdirectory and trips over the planner's own ignored one.
        from tests.test_facility import ignored, tracked

        self.assertEqual(tracked("plans/reviews"), ["plans/reviews/README.md"])
        for name in ("2026/deadbeef.json", "review.JSON", "notes.md", "a/b/c.json"):
            with self.subTest(name=name):
                self.assertTrue(ignored(f"plans/reviews/{name}"))

    def test_the_readme_says_what_the_record_cannot_establish(self):
        text = (pathlib.Path(reviews_mod.REVIEWS_DIR) / "README.md").read_text()
        self.assertIn("attestation", text)
        self.assertIn("cannot establish", text)


if __name__ == "__main__":
    unittest.main()
