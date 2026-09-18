"""The scenario library, and the markdown that is its specification.

`plans/scenarios/96h-isolation-acute-care.md` was written at Phase 0, before
anything could execute it. `test_questions_match_the_markdown` is what keeps
that true: it re-reads the markdown's own bullets and asserts the JSON carries
the same texts in the same order, so the markdown stays the specification and
the JSON stays a transcription of it.
"""

import json
import pathlib
import tempfile
import unittest

from readiness.plans import rules as rules_mod
from readiness.plans import scenarios as scenarios_mod
from readiness.plans.scenarios import Scenario, ScenarioError

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCENARIOS = REPO_ROOT / "plans" / "scenarios"
MARKDOWN = SCENARIOS / "96h-isolation-acute-care.md"
JSON_PATH = SCENARIOS / "96h-isolation-acute-care.json"


class TestTheMarkdownIsTheSpecification(unittest.TestCase):
    def setUp(self):
        self.scenario = scenarios_mod.load()

    def test_questions_match_the_markdown(self):
        # The one test that makes the specification binding: change the
        # markdown's bullets and this fails until the JSON follows.
        expected = scenarios_mod.questions_from_markdown(MARKDOWN)
        self.assertEqual(len(expected), 6)
        self.assertEqual([q.text for q in self.scenario.questions], expected)

    def test_question_ids_are_q1_to_q6_in_the_markdown_order(self):
        self.assertEqual(
            [q.id for q in self.scenario.questions],
            ["q1", "q2", "q3", "q4", "q5", "q6"],
        )

    def test_the_markdown_parser_strips_bold_and_italic_markers(self):
        texts = scenarios_mod.questions_from_markdown(MARKDOWN)
        for text in texts:
            self.assertNotIn("*", text)
        self.assertIn("What is the written trigger for evacuating before", texts[1])
        self.assertIn("do they fail in the same scenarios you do?", texts[3])

    def test_the_parser_refuses_a_markdown_without_the_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "x.md"
            path.write_text("# nothing here\n", encoding="utf-8")
            with self.assertRaises(ScenarioError) as ctx:
                scenarios_mod.questions_from_markdown(path)
            self.assertIn("The plan must answer", str(ctx.exception))

    def test_the_json_names_the_markdown_as_its_source(self):
        self.assertEqual(
            self.scenario.source_doc, "plans/scenarios/96h-isolation-acute-care.md"
        )
        self.assertTrue(scenarios_mod.markdown_path(self.scenario).exists())


class TestTheCommittedScenario(unittest.TestCase):
    def setUp(self):
        self.scenario = scenarios_mod.load()

    def test_six_injects_in_the_markdown_order(self):
        injects = self.scenario.injects
        self.assertEqual(len(injects), 6)
        self.assertEqual(
            [(i.hour, i.kind) for i in injects],
            [(0, "grid_power_lost"), (6, "municipal_water_lost"),
             (12, "design_intensity_reached"), (12, "road_access_lost"),
             (0, "ambient_temperature"), (0, "communications_degraded")],
        )

    def test_the_constants_are_the_numbers_the_injects_imply(self):
        self.assertEqual(self.scenario.constant("isolation_hours"), 96)
        self.assertEqual(self.scenario.constant("water_loss_hour"), 6)
        self.assertEqual(self.scenario.constant("road_access_lost_hour"), 12)
        self.assertEqual(self.scenario.constant("road_access_restored_hour"), 72)
        self.assertEqual(self.scenario.constant("design_intensity_hour"), 12)

    def test_fail_closed_on_names_the_design_intensity_and_both_elevations(self):
        self.assertEqual(
            sorted(self.scenario.fail_closed_on),
            ["design_intensity.flood_elevation_ft",
             "power.switchgear_elevation_ft",
             "power.transfer_switch_elevation_ft"],
        )

    def test_every_question_names_a_rule_that_exists(self):
        self.assertEqual(scenarios_mod.check_shape(self.scenario, rules_mod.RULES), [])
        for question in self.scenario.questions:
            self.assertIn(question.rule, rules_mod.RULES)

    def test_every_field_a_question_requires_is_a_facility_field(self):
        from readiness.plans.facility import SCHEMA

        for question in self.scenario.questions:
            for path in question.requires:
                with self.subTest(question=question.id, path=path):
                    head, _, leaf = path.partition(".")
                    self.assertIn(head, SCHEMA)
                    if leaf:
                        self.assertIn(leaf, SCHEMA[head])

    def test_refs_cover_constants_injects_questions_and_the_scenario_itself(self):
        refs = self.scenario.refs()
        self.assertIn("96h-isolation-acute-care", refs)
        self.assertIn("96h-isolation-acute-care/isolation_hours", refs)
        self.assertIn("96h-isolation-acute-care/i3", refs)
        self.assertIn("96h-isolation-acute-care/q1", refs)

    def test_lookups_refuse_what_they_do_not_have(self):
        for call in (lambda: self.scenario.question("q9"),
                     lambda: self.scenario.inject("i9"),
                     lambda: self.scenario.constant("nope"),
                     lambda: self.scenario.question_for_rule("nope")):
            with self.subTest(call=call), self.assertRaises(ScenarioError):
                call()


class TestLoading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, **overrides) -> pathlib.Path:
        raw = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        raw.update(overrides)
        path = self.dir / f"{raw['id']}.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def test_load_all_reads_the_committed_library(self):
        library = scenarios_mod.load_all()
        self.assertEqual([s.id for s in library], ["96h-isolation-acute-care"])
        self.assertIn("6 question(s)", scenarios_mod.summarise(library[0]))

    def test_an_unknown_scenario_id_lists_what_there_is(self):
        with self.assertRaises(ScenarioError) as ctx:
            scenarios_mod.load("no-such-scenario")
        self.assertIn("no-such-scenario", str(ctx.exception))
        self.assertIn("96h-isolation-acute-care", str(ctx.exception))

    def test_a_missing_key_is_refused_by_name(self):
        raw = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        del raw["constants"]
        path = self.dir / "broken.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ScenarioError) as ctx:
            Scenario.from_path(path)
        self.assertIn("'constants'", str(ctx.exception))

    def test_a_scenario_with_no_question_tests_nothing(self):
        path = self.write(questions=[])
        with self.assertRaises(ScenarioError) as ctx:
            Scenario.from_path(path)
        self.assertIn("tests nothing", str(ctx.exception))

    def test_duplicate_question_ids_are_refused(self):
        raw = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        raw["questions"][1]["id"] = raw["questions"][0]["id"]
        path = self.dir / "dup.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ScenarioError) as ctx:
            Scenario.from_path(path)
        self.assertIn("duplicate question id", str(ctx.exception))

    def test_a_non_numeric_constant_is_refused(self):
        path = self.write(constants={"isolation_hours": "ninety-six"})
        with self.assertRaises(ScenarioError) as ctx:
            Scenario.from_path(path)
        self.assertIn("must be a number", str(ctx.exception))

    def test_check_shape_names_a_rule_that_does_not_exist(self):
        raw = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        raw["questions"][0]["rule"] = "no_such_rule"
        path = self.dir / "rule.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        problems = scenarios_mod.check_shape(Scenario.from_path(path), rules_mod.RULES)
        self.assertEqual(len(problems), 1)
        self.assertIn("no_such_rule", problems[0])

    def test_a_round_trip_through_to_dict_keeps_everything(self):
        scenario = scenarios_mod.load()
        again = Scenario.from_json(scenario.to_dict())
        self.assertEqual(again.questions, scenario.questions)
        self.assertEqual(again.injects, scenario.injects)
        self.assertEqual(again.constants, scenario.constants)


if __name__ == "__main__":
    unittest.main()
