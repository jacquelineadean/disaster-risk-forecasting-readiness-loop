"""The gap report: what it cites, what it refuses to write, and what it blinds.

The blinding tests are the ones the exit criterion leans on. A blinded render
must contain neither the slug nor any partner name, because that file is what a
reviewer reads; and its sha256 is what a review record binds to, so the same
document must render to the same bytes and a changed document must not.
"""

import json
import pathlib
import tempfile
import unittest

from readiness import cite
from readiness.plans import gap_report as gap_report_mod
from readiness.plans import rules as rules_mod
from readiness.plans.facility import FacilityError
from readiness.plans.risk import RiskLayer
from readiness.plans.scenarios import ScenarioError
from tests import fixtures_plans as fp

PARTNERS = [
    {"name": "Far Ridge Hospital", "county_fips": fp.OTHER_COUNTY, "signed": True,
     "same_floodplain": False, "same_grid_feeder": False},
]


class GapReportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.scenario = fp.scenario()
        self.risk = fp.make_risk()

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, *, risk=None, drafter="local", rewriter=None, **overrides):
        risk = self.risk if risk is None else risk
        self.facility = fp.make_facility(**overrides)
        findings = rules_mod.run(self.facility, risk, self.scenario)
        doc = gap_report_mod.build(
            self.facility, self.scenario, findings, risk, fp.PERIOD,
            drafter=drafter, rewriter=rewriter,
        )
        self.findings = findings
        self.resolver = gap_report_mod.resolver(self.facility, self.scenario, risk)
        return doc


class TestTheDocument(GapReportCase):
    def test_every_sentence_cites_a_resolvable_claim(self):
        doc = self.build()
        self.assertEqual(gap_report_mod.check(doc, self.resolver), [])
        self.assertEqual(doc.kind, "gap-report")
        for index, sentence in enumerate(doc.sentences):
            with self.subTest(index=index):
                self.assertTrue(sentence.cited(), sentence.text)

    def test_sentences_come_in_the_scenario_s_question_order(self):
        doc = self.build()
        text = fp.sentences_text(doc)
        order = ["first", "second", "third", "fourth", "fifth", "sixth"]
        positions = [text.index(f"On the scenario's {word} question") for word in order]
        self.assertEqual(positions, sorted(positions))

    def test_each_question_is_quoted_verbatim_as_a_claim(self):
        doc = self.build()
        claims = doc.claim_index()
        for question in self.scenario.questions:
            with self.subTest(question=question.id):
                claim = claims[f"q-{question.id}"]
                self.assertEqual(claim.text, question.text)
                self.assertEqual(claim.source.kind, "scenario")

    def test_the_provenance_line_names_the_drafter_and_the_drops(self):
        doc = self.build()
        text = fp.sentences_text(doc)
        self.assertIn("prose from the local drafter", text)
        self.assertIn("0 drafted sentence(s) were dropped", text)
        self.assertEqual(doc.claim_index()["provenance-dropped"].value, 0)
        self.assertEqual(doc.inputs["dropped_sentences"], "0")

    def test_it_carries_the_not_a_warning_line_citing_official_alerting(self):
        doc = self.build()
        self.assertIn(cite.NOT_A_WARNING_SENTENCE, fp.sentences_text(doc))
        self.assertEqual(gap_report_mod.check(doc, self.resolver), [])

    def test_the_inputs_name_what_it_was_built_from(self):
        doc = self.build()
        self.assertEqual(doc.inputs["facility"], "test-facility")
        self.assertEqual(doc.inputs["period"], fp.PERIOD)
        self.assertIn(self.scenario.id, doc.inputs["scenario"])
        self.assertIn(f"issued/{fp.CONTRACT}", doc.inputs["risk_layer"])

    def test_with_nothing_issued_the_footer_says_so(self):
        doc = self.build(risk=RiskLayer.empty(fp.PERIOD))
        self.assertIn("nothing issued", doc.inputs["risk_layer"])
        resolve = gap_report_mod.resolver(
            self.facility, self.scenario, RiskLayer.empty(fp.PERIOD)
        )
        self.assertEqual(gap_report_mod.check(doc, resolve), [])

    def test_a_claim_cited_by_two_rules_appears_once(self):
        doc = self.build()
        ids = [c.id for c in doc.claims]
        self.assertEqual(len(ids), len(set(ids)))

    def test_an_unknown_drafter_is_refused(self):
        with self.assertRaises(gap_report_mod.GapReportError):
            self.build(drafter="gpt")

    def test_a_finding_for_a_question_the_scenario_lacks_is_refused(self):
        facility = fp.make_facility()
        findings = list(rules_mod.run(facility, self.risk, self.scenario))
        findings.append(rules_mod.Finding("q9", "answered"))
        with self.assertRaises(gap_report_mod.GapReportError) as ctx:
            gap_report_mod.build(
                facility, self.scenario, findings, self.risk, fp.PERIOD
            )
        self.assertIn("q9", str(ctx.exception))

    def test_the_json_carries_no_forbidden_key(self):
        doc = self.build(co_located_operators=[{"name": "Co", "occupants": 4}])
        payload = cite.to_dict(doc)
        self.assertEqual(gap_report_mod.forbidden_keys(payload), [])
        blob = json.dumps(payload)
        for key in ("\"address\"", "\"lat\"", "\"lon\"", "\"tract\"", "\"parcel\""):
            self.assertNotIn(key, blob)

    def test_a_forbidden_key_anywhere_is_a_violation(self):
        doc = self.build()
        doctored = cite.to_dict(doc)
        doctored["inputs"]["lat"] = "35.9"
        found = gap_report_mod.check(cite.from_dict(doctored), self.resolver)
        self.assertEqual([v.code for v in found], [cite.UNRESOLVED])
        self.assertIn("forbidden field (inputs.lat)", found[0].detail)


class TestBlinding(GapReportCase):
    def test_the_blinded_render_has_no_names(self):
        doc = self.build(
            slug="saint-example-regional",
            co_located_operators=[{"name": "Dialysis Partners", "occupants": 9}],
        )
        page, sha = gap_report_mod.render_blinded(doc)
        self.assertNotIn("saint-example-regional", page)
        self.assertNotIn("Far Ridge Hospital", page)
        self.assertNotIn("Dialysis Partners", page)
        self.assertIn(self.facility.blind_label, page)
        self.assertIn("PARTNER-1", page)
        self.assertIn("PARTNER-2", page)
        self.assertEqual(len(sha), 64)

    def test_the_unblinded_page_does_name_them(self):
        doc = self.build(slug="saint-example-regional")
        page = gap_report_mod.render_html(doc)
        self.assertIn("saint-example-regional", page)
        self.assertIn("Far Ridge Hospital", page)

    def test_the_blinded_page_announces_what_it_is(self):
        doc = self.build()
        page, _sha = gap_report_mod.render_blinded(doc)
        self.assertEqual(
            gap_report_mod.blind_details(page),
            (self.facility.blind_label, fp.PERIOD, "gap-report"),
        )
        self.assertIsNone(gap_report_mod.blind_details("<html></html>"))

    def test_the_label_is_six_hex_of_the_slug_hash(self):
        doc = self.build(slug="one-slug")
        mapping = gap_report_mod.blind_map(doc)
        self.assertEqual(mapping["one-slug"], self.facility.blind_label)
        self.assertRegex(self.facility.blind_label, r"^FACILITY-[0-9a-f]{6}$")

    def test_two_renders_of_one_document_are_the_same_bytes(self):
        doc = self.build()
        first, sha_a = gap_report_mod.render_blinded(doc)
        second, sha_b = gap_report_mod.render_blinded(doc)
        self.assertEqual(first, second)
        self.assertEqual(sha_a, sha_b)

    def test_a_changed_number_moves_the_sha(self):
        doc = self.build()
        _page, before = gap_report_mod.render_blinded(doc)
        doctored = cite.to_dict(doc)
        doctored["sentences"][1]["text"] = doctored["sentences"][1]["text"].replace(
            "10 feet", "99 feet"
        )
        _page, after = gap_report_mod.render_blinded(cite.from_dict(doctored))
        self.assertNotEqual(before, after)

    def test_the_review_note_is_on_both_renderings(self):
        doc = self.build()
        note = "A practising emergency manager reviews every finding"
        self.assertIn(note, gap_report_mod.render_html(doc))
        self.assertIn(note, gap_report_mod.render_blinded(doc)[0])


class TestWriting(GapReportCase):
    def test_writes_three_files(self):
        doc = self.build()
        html_path, json_path, blind_path = gap_report_mod.write(
            doc, self.dir, resolve=self.resolver
        )
        self.assertEqual(html_path, self.dir / "test-facility" / f"{fp.PERIOD}.html")
        self.assertEqual(json_path, self.dir / "test-facility" / f"{fp.PERIOD}.json")
        self.assertEqual(
            blind_path, self.dir / "test-facility" / f"{fp.PERIOD}.blind.html"
        )
        # The JSON is the document the pages were rendered from.
        again = cite.from_json(json_path.read_text(encoding="utf-8"))
        self.assertEqual(again.sentences, doc.sentences)
        self.assertEqual(
            gap_report_mod.render_blinded(again)[1],
            gap_report_mod.render_blinded(doc)[1],
        )

    def test_not_written_on_violation(self):
        doc = self.build()
        blind = cite.DictResolver({"facility": {}, "scenario": set(), "issued": {}})
        with self.assertRaises(gap_report_mod.GapReportRefused) as ctx:
            gap_report_mod.write(doc, self.dir, resolve=blind)
        self.assertTrue(ctx.exception.violations)
        self.assertIn(cite.UNRESOLVED, str(ctx.exception))
        self.assertFalse((self.dir / "test-facility").exists())

    def test_an_uncited_sentence_is_refused_too(self):
        doctored = cite.to_dict(self.build())
        doctored["sentences"].append({"text": "Evacuate now.", "claim_ids": []})
        with self.assertRaises(gap_report_mod.GapReportRefused) as ctx:
            gap_report_mod.write(
                cite.from_dict(doctored), self.dir, resolve=self.resolver
            )
        self.assertIn(cite.UNCITED, str(ctx.exception))
        self.assertFalse((self.dir / "test-facility").exists())

    def test_a_number_that_is_not_a_cited_value_is_refused(self):
        doctored = cite.to_dict(self.build())
        text = doctored["sentences"][1]["text"]
        self.assertIn("10 feet", text)
        doctored["sentences"][1]["text"] = text.replace("10 feet", "40 feet")
        with self.assertRaises(gap_report_mod.GapReportRefused) as ctx:
            gap_report_mod.write(
                cite.from_dict(doctored), self.dir, resolve=self.resolver
            )
        self.assertIn(cite.NUMBER_WITHOUT_CLAIM, str(ctx.exception))

    def test_warning_language_is_refused(self):
        doctored = cite.to_dict(self.build())
        doctored["sentences"][0]["text"] = (
            "A damaging flood will occur in this county [c:q-q1]."
        )
        with self.assertRaises(gap_report_mod.GapReportRefused) as ctx:
            gap_report_mod.write(
                cite.from_dict(doctored), self.dir, resolve=self.resolver
            )
        self.assertIn(cite.FORBIDDEN_PHRASE, str(ctx.exception))

    def test_write_needs_the_facility_and_period_in_the_inputs(self):
        doc = self.build()
        stripped = cite.Document(
            doc.title, doc.kind, doc.sentences, doc.claims, doc.generated_at, {}
        )
        with self.assertRaises(gap_report_mod.GapReportError):
            gap_report_mod.write(stripped, self.dir, resolve=self.resolver)


class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.issued_dir = self.dir / "issued"
        fp.make_issued().write(self.issued_dir)
        self.facility_path = fp.write_facility(self.dir / "f.json")

    def tearDown(self):
        self.tmp.cleanup()

    def run_report(self, **kw):
        options = dict(
            out_dir=self.dir / "reports", issued_dir=self.issued_dir,
            experiments_dir=self.dir / "experiments",
        )
        options.update(kw)
        return gap_report_mod.run(self.facility_path, fp.PERIOD, **options)

    def test_end_to_end_writes_and_reports_every_status(self):
        report = self.run_report()
        self.assertEqual(
            sorted(report.statuses()), ["q1", "q2", "q3", "q4", "q5", "q6"]
        )
        for path in (report.html_path, report.json_path, report.blind_path):
            self.assertTrue(path.exists())
        self.assertEqual(
            report.blind_sha256,
            gap_report_mod.render_blinded(report.document)[1],
        )
        from readiness.plans import reviews as reviews_mod

        self.assertEqual(reviews_mod.sha256_of(report.blind_path), report.blind_sha256)

    def test_it_reads_the_issued_layer_for_the_facility_and_partner_counties(self):
        report = self.run_report()
        issued_claims = [
            c for c in report.document.claims if c.source.kind == "issued"
        ]
        self.assertEqual(len(issued_claims), 2)

    def test_a_record_it_will_not_read_raises_facility_error(self):
        broken = self.dir / "broken.json"
        broken.write_text(json.dumps({"slug": "x", "lat": 1.0}), encoding="utf-8")
        with self.assertRaises(FacilityError) as ctx:
            gap_report_mod.run(
                broken, fp.PERIOD, out_dir=self.dir / "reports",
                issued_dir=self.issued_dir, experiments_dir=self.dir / "experiments",
            )
        self.assertIn("'lat' is refused", str(ctx.exception))
        self.assertFalse((self.dir / "reports").exists())

    def test_an_unknown_scenario_raises(self):
        with self.assertRaises(ScenarioError):
            self.run_report(scenario_id="no-such-scenario")

    def test_the_gaps_are_the_findings_that_are_not_answered(self):
        path = fp.write_facility(
            self.dir / "gappy.json", slug="gappy",
            **{"power.switchgear_elevation_ft": 2},
        )
        report = gap_report_mod.run(
            path, fp.PERIOD, out_dir=self.dir / "reports",
            issued_dir=self.issued_dir, experiments_dir=self.dir / "experiments",
        )
        self.assertEqual([f.question_id for f in report.gaps()], ["q1"])


if __name__ == "__main__":
    unittest.main()
