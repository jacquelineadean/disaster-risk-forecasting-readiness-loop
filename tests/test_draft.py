"""The two drafters: templated prose that cites, and model prose that must.

The local drafter is deterministic and cites a guidance id and a facility field
for every recommendation. The claude drafter is tested with a fake model
function injected — never the SDK — because the property under test is what
happens to what a model returns, not whether a model can be reached.
"""

import unittest

from readiness import cite
from readiness.agent import planner
from readiness.plans import draft as draft_mod
from readiness.plans import gap_report as gap_report_mod
from readiness.plans import rules as rules_mod
from tests import fixtures_plans as fp


class DraftCase(unittest.TestCase):
    def setUp(self):
        self.scenario = fp.scenario()
        self.risk = fp.make_risk()
        self.facility = fp.make_facility()
        self.findings = rules_mod.run(self.facility, self.risk, self.scenario)


class TestLocalDrafter(DraftCase):
    def test_local_sections_cite_guidance_ids(self):
        sections = draft_mod.sections(self.facility, self.scenario, self.findings)
        self.assertEqual(sorted(sections), ["q1", "q2", "q3", "q4", "q5", "q6"])
        for question_id, (sentences, claims) in sorted(sections.items()):
            with self.subTest(question=question_id):
                self.assertEqual(len(sentences), 1)
                kinds = {c.source.kind for c in claims}
                self.assertIn("guidance", kinds)
                self.assertIn("facility", kinds)
                cited = set(sentences[0].cited())
                self.assertEqual(cited, {c.id for c in claims})

    def test_every_guidance_id_it_can_cite_is_in_the_registry(self):
        registry = cite.load_guidance()
        ids = draft_mod.guidance_ids()
        for guidance_id in ids:
            self.assertIn(guidance_id, registry)
        # Every document plan §4 lists for Phase 3 is reachable from a section.
        self.assertEqual(set(ids), set(registry))

    def test_the_spelling_each_template_uses_is_a_registered_alias(self):
        aliases = {
            alias
            for entry in cite.load_guidance().values()
            for alias in entry["aliases"]
        }
        spellings = {
            entry[2] for rule in draft_mod.SECTIONS.values() for entry in rule.values()
        }
        for spelling in spellings:
            with self.subTest(spelling=spelling):
                self.assertTrue(
                    any(alias in spelling for alias in aliases) or " " in spelling,
                    f"{spelling!r} is not a registered alias",
                )

    def test_the_section_follows_the_status(self):
        failed = rules_mod.Finding("q1", "failed", (), ())
        cannot = rules_mod.Finding("q1", "cannot_run", (), ())
        first, _ = draft_mod.section_for(failed, "switchgear_vs_intensity")
        second, claims = draft_mod.section_for(cannot, "switchgear_vs_intensity")
        self.assertNotEqual(first.text, second.text)
        self.assertIn("Elevation Certificate", second.text)
        self.assertEqual(claims[0].source.ref, "fema-elevation-certificate")

    def test_an_unknown_rule_drafts_nothing(self):
        sentence, claims = draft_mod.section_for(
            rules_mod.Finding("q1", "failed"), "no_such_rule"
        )
        self.assertIsNone(sentence)
        self.assertEqual(claims, [])

    def test_it_is_deterministic(self):
        first = draft_mod.sections(self.facility, self.scenario, self.findings)
        second = draft_mod.sections(self.facility, self.scenario, self.findings)
        self.assertEqual(
            {k: [s.text for s in v[0]] for k, v in first.items()},
            {k: [s.text for s in v[0]] for k, v in second.items()},
        )

    def test_the_disclaimer_cites_official_alerting(self):
        sentence, claim = draft_mod.disclaimer()
        self.assertIn(cite.NOT_A_WARNING_SENTENCE, sentence.text)
        self.assertEqual(claim.source.ref, cite.NOT_A_WARNING_GUIDANCE)
        self.assertEqual(sentence.cited(), (claim.id,))


class TestClaudeDrafter(DraftCase):
    """The optional backend, with a fake model. The SDK is never imported here."""

    def setUp(self):
        super().setUp()
        self.doc = gap_report_mod.build(
            self.facility, self.scenario, self.findings, self.risk, fp.PERIOD
        )
        self.claims = list(self.doc.claims)
        # The rewriter only ever sees the body: the provenance lines and the
        # disclaimer are added after it runs, so a drafter cannot restate its
        # own provenance. Capture exactly what it is handed.
        captured: dict = {}

        def capture(sentences, claims):
            captured["body"] = list(sentences)
            return list(sentences), 0

        gap_report_mod.build(
            self.facility, self.scenario, self.findings, self.risk, fp.PERIOD,
            drafter="claude", rewriter=capture,
        )
        self.full_body = captured["body"]
        self.body = self.full_body[:4]

    def model(self, reply):
        """A fake backend: records the prompt it was given, returns `reply`."""
        self.prompts = []

        def ask(prompt: str) -> str:
            self.prompts.append(prompt)
            return reply(prompt) if callable(reply) else reply

        return ask

    def json_reply(self, sentences):
        import json

        return json.dumps(sentences)

    def test_a_faithful_rewrite_is_kept(self):
        rewritten = [f"Rewritten: {s.text}" for s in self.body]
        kept, dropped = planner.rewrite(
            self.body, self.claims, model=self.model(self.json_reply(rewritten))
        )
        self.assertEqual(dropped, 0)
        self.assertEqual([s.text for s in kept], rewritten)
        self.assertIn("[c:", self.prompts[0])

    def test_llm_sentences_without_claims_are_dropped_and_counted(self):
        rewritten = [s.text for s in self.body]
        rewritten[0] = "The plan is fine."          # no marker at all
        rewritten[1] = cite.strip_markers(self.body[1].text)  # markers removed
        kept, dropped = planner.rewrite(
            self.body, self.claims, model=self.model(self.json_reply(rewritten))
        )
        self.assertEqual(dropped, 2)
        self.assertEqual(len(kept), len(self.body) - 2)
        self.assertNotIn("The plan is fine.", [s.text for s in kept])

    def test_a_sentence_citing_a_claim_the_report_does_not_hold_is_dropped(self):
        rewritten = [s.text for s in self.body]
        rewritten[0] = "Something true [c:not-a-claim]."
        _kept, dropped = planner.rewrite(
            self.body, self.claims, model=self.model(self.json_reply(rewritten))
        )
        self.assertEqual(dropped, 1)

    def test_a_sentence_citing_another_sentence_s_claim_is_dropped(self):
        # A rewrite may reword; it may not re-attribute.
        other = self.body[1].cited()[0]
        rewritten = [s.text for s in self.body]
        rewritten[0] = f"Borrowed citation [c:{other}]."
        _kept, dropped = planner.rewrite(
            self.body, self.claims, model=self.model(self.json_reply(rewritten))
        )
        self.assertEqual(dropped, 1)

    def test_an_invented_number_is_dropped(self):
        rewritten = [s.text for s in self.body]
        marker = self.body[0].cited()[0]
        rewritten[0] = f"The switchgear sits at 44 feet [c:{marker}]."
        _kept, dropped = planner.rewrite(
            self.body, self.claims, model=self.model(self.json_reply(rewritten))
        )
        self.assertEqual(dropped, 1)

    def test_warning_language_is_dropped(self):
        rewritten = [s.text for s in self.body]
        marker = self.body[0].cited()[0]
        rewritten[0] = f"This is a warning to evacuate [c:{marker}]."
        _kept, dropped = planner.rewrite(
            self.body, self.claims, model=self.model(self.json_reply(rewritten))
        )
        self.assertEqual(dropped, 1)

    def test_a_malformed_response_leaves_the_local_prose_alone(self):
        for reply in ("not json", "[]", '["only one"]', '{"sentences": []}'):
            with self.subTest(reply=reply):
                kept, dropped = planner.rewrite(
                    self.body, self.claims, model=self.model(reply)
                )
                self.assertEqual(dropped, 0)
                self.assertEqual(kept, self.body)

    def test_a_backend_that_raises_leaves_the_local_prose_alone(self):
        def boom(_prompt):
            raise RuntimeError("no api key")

        kept, dropped = planner.rewrite(self.body, self.claims, model=boom)
        self.assertEqual(kept, self.body)
        self.assertEqual(dropped, 0)

    def test_an_empty_body_is_not_sent_to_a_model(self):
        called = []
        kept, dropped = planner.rewrite(
            [], self.claims, model=lambda p: called.append(p) or "[]"
        )
        self.assertEqual((kept, dropped), ([], 0))
        self.assertEqual(called, [])

    def test_a_pathological_body_is_refused_rather_than_sent(self):
        with self.assertRaises(ValueError):
            planner.rewrite(
                self.body * planner.MAX_SENTENCES, self.claims, model=lambda p: "[]"
            )

    def test_the_dropped_count_reaches_the_report_s_provenance_line(self):
        rewritten = [s.text for s in self.full_body]
        rewritten[0] = "Unsupported."
        rewriter = planner.rewriter(model=self.model(self.json_reply(rewritten)))
        doc = gap_report_mod.build(
            self.facility, self.scenario, self.findings, self.risk, fp.PERIOD,
            drafter="claude", rewriter=rewriter,
        )
        self.assertEqual(doc.claim_index()["provenance-dropped"].value, 1)
        self.assertIn("1 drafted sentence(s) were dropped", fp.sentences_text(doc))
        self.assertIn("prose from the claude drafter", fp.sentences_text(doc))
        resolve = gap_report_mod.resolver(self.facility, self.scenario, self.risk)
        self.assertEqual(gap_report_mod.check(doc, resolve), [])

    def test_the_rewritten_report_still_validates_as_a_whole(self):
        rewriter = planner.rewriter(
            model=self.model(
                lambda prompt: self.json_reply(
                    [f"In other words, {s.text}" for s in self.full_body]
                )
            )
        )
        doc = gap_report_mod.build(
            self.facility, self.scenario, self.findings, self.risk, fp.PERIOD,
            drafter="claude", rewriter=rewriter,
        )
        resolve = gap_report_mod.resolver(self.facility, self.scenario, self.risk)
        self.assertEqual(gap_report_mod.check(doc, resolve), [])
        self.assertIn("In other words,", fp.sentences_text(doc))

    def test_the_prompt_states_the_rules_the_answer_is_checked_against(self):
        planner.rewrite(self.body, self.claims, model=self.model("[]"))
        prompt = self.prompts[0]
        for needle in ("[c:ID]", "warning", "JSON array", "dropped"):
            self.assertIn(needle, prompt)

    def test_the_sdk_is_only_imported_inside_a_try(self):
        import ast
        import pathlib

        source = pathlib.Path(planner.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        found = []

        def walk(node, guarded):
            for child in ast.iter_child_nodes(node):
                inner = guarded or isinstance(child, ast.Try)
                if isinstance(child, ast.ImportFrom) and child.module:
                    found.append((child.module, inner))
                walk(child, inner)

        walk(tree, False)
        sdk = [guarded for module, guarded in found if module == "claude_agent_sdk"]
        self.assertEqual(sdk, [True])


if __name__ == "__main__":
    unittest.main()
