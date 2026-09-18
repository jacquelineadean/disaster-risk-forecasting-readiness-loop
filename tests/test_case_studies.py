"""Case studies as regression tests, exercised on the synthetic one.

Zero real case studies ship — each is an example added deliberately, with a
published investigation cited for every fact — so the mechanism is tested
against `tests/fixtures_plans.py::synthetic_case_study`, which names an event
that did not happen at a facility that does not exist.
"""

import dataclasses
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from readiness.plans import case_studies as case_studies_mod
from readiness.plans import rules as rules_mod
from readiness.plans.case_studies import CaseStudy, CaseStudyError
from tests import fixtures_plans as fp


class CaseStudyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, **overrides) -> pathlib.Path:
        return fp.write_case_study(self.dir / "synthetic.json", **overrides)


class TestTheMechanism(CaseStudyCase):
    def test_the_synthetic_study_reproduces_its_expected_findings(self):
        result = case_studies_mod.check(self.write())
        self.assertTrue(result.passed, (result.problems, result.mismatches))
        self.assertEqual(result.slug, "synthetic-river-flood")
        self.assertEqual(
            result.observed,
            {"q1": "failed", "q2": "failed", "q3": "failed",
             "q4": "failed", "q5": "answered", "q6": "failed"},
        )
        self.assertIn("[ok]", result.format())

    def test_a_changed_expectation_is_a_mismatch_naming_the_question(self):
        study = fp.synthetic_case_study()
        study["expected_findings"]["q1"] = "answered"
        result = case_studies_mod.check(self.write(**study))
        self.assertFalse(result.passed)
        self.assertEqual(len(result.mismatches), 1)
        self.assertIn("q1: expected 'answered', rules said 'failed'", result.mismatches[0])
        self.assertIn("[FAIL]", result.format())

    def test_a_changed_record_is_a_mismatch(self):
        # This is what makes a case study a regression test: raise the
        # switchgear above the design elevation and q1 stops failing.
        study = fp.synthetic_case_study()
        study["facility_as_recorded"]["power"]["switchgear_elevation_ft"] = 20
        study["facility_as_recorded"]["power"]["transfer_switch_elevation_ft"] = 20
        result = case_studies_mod.check(self.write(**study))
        self.assertFalse(result.passed)
        self.assertEqual(result.observed["q1"], "answered")

    def test_it_runs_against_an_empty_risk_layer_and_says_so(self):
        # A past event has no issued file, and none is invented: the partner
        # question reports the absence instead of borrowing a number.
        from readiness.plans import rules as rules_mod
        from readiness.plans.risk import RiskLayer

        study = CaseStudy.from_json(fp.synthetic_case_study())
        findings = rules_mod.run(
            study.facility, RiskLayer.empty(case_studies_mod.NO_PERIOD), fp.scenario()
        )
        partner = next(f for f in findings if f.question_id == "q4")
        self.assertIn("no validated issuance covers", partner.text())
        self.assertNotIn("%", partner.text())


class TestSchema(CaseStudyCase):
    def refuse(self, **overrides) -> str:
        with self.assertRaises(CaseStudyError) as ctx:
            CaseStudy.from_json(fp.synthetic_case_study(**overrides), where="s.json")
        return str(ctx.exception)

    def test_a_fact_without_a_source_is_rejected(self):
        message = self.refuse(event={"text": "something happened"})
        self.assertIn("'event'", message)
        self.assertIn("a fact without a source does not belong", message)

    def test_a_fact_citing_a_source_that_does_not_exist_is_rejected(self):
        message = self.refuse(hazard={"text": "flood", "source": 7})
        self.assertIn("cites source 7", message)

    def test_every_fact_field_must_be_present(self):
        study = fp.synthetic_case_study()
        del study["dates"]
        with self.assertRaises(CaseStudyError) as ctx:
            CaseStudy.from_json(study, where="s.json")
        self.assertIn("'dates'", str(ctx.exception))

    def test_a_study_with_no_sources_is_rejected(self):
        self.assertIn("non-empty list", self.refuse(sources=[]))

    def test_a_source_missing_a_field_is_rejected(self):
        message = self.refuse(sources=[{"title": "t", "publisher": "p", "year": 2020}])
        self.assertIn("'url'", message)

    def test_a_facility_the_schema_refuses_refuses_the_study(self):
        study = fp.synthetic_case_study()
        study["facility_as_recorded"]["address"] = "1 Example Street"
        with self.assertRaises(CaseStudyError) as ctx:
            CaseStudy.from_json(study, where="s.json")
        self.assertIn("facility_as_recorded is refused", str(ctx.exception))
        self.assertIn("'address' is refused", str(ctx.exception))

    def test_an_unknown_status_is_rejected(self):
        study = fp.synthetic_case_study()
        study["expected_findings"]["q1"] = "sort of"
        with self.assertRaises(CaseStudyError) as ctx:
            CaseStudy.from_json(study, where="s.json")
        self.assertIn("expected one of", str(ctx.exception))

    def test_facts_with_sources_reports_a_source_index_that_does_not_resolve(self):
        # Reachable, and reached: `from_json` refuses a bad index, so the only
        # way here is the dataclass, which is how `check_study` could be handed
        # one. (The old second loop over the facility's evidence was
        # unreachable — `Facility` cannot exist with a blank `source_doc` — and
        # is gone rather than tested from the outside and never entered.)
        study = CaseStudy.from_json(fp.synthetic_case_study())
        self.assertEqual(case_studies_mod.facts_with_sources(study), [])
        broken = dataclasses.replace(
            study, hazard=case_studies_mod.Fact(text="inland flood", source=7)
        )
        problems = case_studies_mod.facts_with_sources(broken)
        self.assertEqual(len(problems), 1)
        self.assertIn("hazard: cites source 7", problems[0])
        result = case_studies_mod.check_study(broken)
        self.assertFalse(result.passed)
        self.assertIn("hazard: cites source 7", result.problems[0])

    def test_an_unknown_key_is_refused_at_every_level(self):
        # Finding 5 of the leakage review: this is the one plans directory that
        # *is* committed, and its loader took any extra top-level key.
        cases = {
            "top level": lambda s: s.update(facility_type="hospital"),
            "a fact": lambda s: s["event"].update(confidence="high"),
            "a source": lambda s: s["sources"][0].update(accessed="2026-01-01"),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                study = fp.synthetic_case_study()
                mutate(study)
                with self.assertRaises(CaseStudyError) as ctx:
                    CaseStudy.from_json(study, where="s.json")
                self.assertIn("unknown field", str(ctx.exception))
        # `note` on a source is known and kept.
        study = fp.synthetic_case_study()
        study["sources"][0]["note"] = "what this establishes"
        self.assertEqual(
            CaseStudy.from_json(study).sources[0].note, "what this establishes"
        )

    def test_a_place_anywhere_in_a_committed_study_is_refused(self):
        for value in ("412 Riverside Drive", "27834-1234", "35.6127, -77.3664",
                      "ZIP 27834"):
            with self.subTest(value=value):
                study = fp.synthetic_case_study()
                study["event"]["text"] = f"A flood at {value}."
                with self.assertRaises(CaseStudyError) as ctx:
                    CaseStudy.from_json(study, where="s.json")
                self.assertIn("is refused", str(ctx.exception))
        # A county FIPS is not a place finer than a county.
        study = fp.synthetic_case_study()
        study["event"]["text"] = "A flood in county 27834."
        CaseStudy.from_json(study)

    def test_a_top_level_that_is_not_an_object_is_refused(self):
        for payload in (None, [], "a study", 3):
            with self.subTest(payload=payload):
                with self.assertRaises(CaseStudyError) as ctx:
                    CaseStudy.from_json(payload, where="s.json")
                self.assertIn("expected a JSON object", str(ctx.exception))

    def test_a_file_whose_top_level_is_a_scalar_is_a_failing_result(self):
        path = self.dir / "null.json"
        path.write_text("null", encoding="utf-8")
        results = case_studies_mod.check_all(self.dir)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].passed)
        self.assertIn("expected a JSON object", results[0].problems[0])

    def test_the_readme_s_json_block_loads_through_the_schema(self):
        # Finding D3: the README's example omitted `slug` and `dates` and gave
        # `event`/`hazard` as bare strings, none of which the loader accepts.
        import re

        text = (
            pathlib.Path(case_studies_mod.CASE_STUDIES_DIR) / "README.md"
        ).read_text(encoding="utf-8")
        blocks = re.findall(r"```json\n(.*?)```", text, re.S)
        self.assertEqual(len(blocks), 1, "the README should show one JSON shape")
        raw = json.loads(blocks[0])
        study = CaseStudy.from_json(raw, where="plans/case-studies/README.md")
        self.assertEqual(sorted(raw), sorted(case_studies_mod.STUDY_KEYS))
        result = case_studies_mod.check_study(study)
        self.assertTrue(result.passed, (result.problems, result.mismatches))

    def test_expecting_a_question_the_scenario_does_not_ask_is_a_problem(self):
        study = fp.synthetic_case_study()
        study["expected_findings"]["q9"] = "answered"
        result = case_studies_mod.check(self.write(**study))
        self.assertFalse(result.passed)
        self.assertIn("q9", result.problems[0])

    def test_a_question_the_rules_never_answered_is_a_problem(self):
        # Finding 8: `check_study` compared the intersection, so an expectation
        # for a question that never ran was never checked and never reported.
        study = CaseStudy.from_json(fp.synthetic_case_study())
        result = case_studies_mod.check_study(study)
        self.assertTrue(result.passed)
        real_run = rules_mod.run
        with mock.patch.object(
            case_studies_mod.rules_mod, "run",
            side_effect=lambda f, r, s: [
                finding for finding in real_run(f, r, s)
                if finding.question_id != "q4"
            ],
        ):
            result = case_studies_mod.check_study(study)
        self.assertFalse(result.passed)
        self.assertIn("no finding for question(s) ['q4']", result.problems[0])

    def test_leaving_a_question_out_is_a_problem(self):
        study = fp.synthetic_case_study()
        del study["expected_findings"]["q6"]
        result = case_studies_mod.check(self.write(**study))
        self.assertFalse(result.passed)
        self.assertIn("q6", result.problems[0])

    def test_an_unknown_scenario_is_a_problem_not_a_crash(self):
        result = case_studies_mod.check(self.write(scenario="no-such-scenario"))
        self.assertFalse(result.passed)
        self.assertIn("no-such-scenario", result.problems[0])

    def test_a_file_that_is_not_json_is_refused_with_its_path(self):
        path = self.dir / "broken.json"
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(CaseStudyError) as ctx:
            CaseStudy.from_path(path)
        self.assertIn("not valid JSON", str(ctx.exception))


class TestTheLibrary(CaseStudyCase):
    def test_check_all_over_a_directory(self):
        self.write()
        fp.write_case_study(self.dir / "second.json", slug="second-study")
        results = case_studies_mod.check_all(self.dir)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.passed for r in results))

    def test_a_broken_file_becomes_a_failing_result_rather_than_an_exception(self):
        (self.dir / "broken.json").write_text(json.dumps({"slug": "x"}), encoding="utf-8")
        results = case_studies_mod.check_all(self.dir)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].passed)
        self.assertIn("missing", results[0].problems[0])

    def test_zero_case_studies_ship(self):
        root = pathlib.Path(case_studies_mod.CASE_STUDIES_DIR)
        self.assertEqual(case_studies_mod.paths(), [])
        self.assertEqual(case_studies_mod.check_all(), [])
        self.assertTrue((root / "README.md").exists())

    def test_the_readme_says_every_fact_must_cite_an_investigation(self):
        text = (
            pathlib.Path(case_studies_mod.CASE_STUDIES_DIR) / "README.md"
        ).read_text(encoding="utf-8")
        self.assertIn("published investigation", text)


if __name__ == "__main__":
    unittest.main()
