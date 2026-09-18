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

    def test_the_provenance_line_names_the_drafter_and_the_refusals(self):
        doc = self.build()
        text = fp.sentences_text(doc)
        self.assertIn("prose from the local drafter", text)
        self.assertIn("0 of ", text)
        self.assertIn("drafted sentences were refused and kept their local wording",
                      text)
        self.assertEqual(doc.claim_index()["provenance-refused"].value, 0)
        self.assertEqual(doc.inputs["refused_sentences"], "0")
        self.assertEqual(
            doc.inputs["drafted_sentences"],
            str(doc.claim_index()["provenance-drafted"].value),
        )

    def test_a_question_with_no_finding_is_refused_not_skipped(self):
        # Finding 8: `continue` made a question vanish from the report
        # silently, so nothing ever ran and nothing ever said so.
        facility = fp.make_facility()
        findings = [
            f for f in rules_mod.run(facility, self.risk, self.scenario)
            if f.question_id != "q3"
        ]
        with self.assertRaises(gap_report_mod.GapReportError) as ctx:
            gap_report_mod.build(
                facility, self.scenario, findings, self.risk, fp.PERIOD
            )
        self.assertIn("q3", str(ctx.exception))

    def test_the_legend_maps_every_label_to_what_the_record_calls_it(self):
        doc = self.build(
            co_located_operators=[{"name": "Dialysis Co", "occupants": 9}]
        )
        entries = dict((label, name) for _what, label, name
                       in gap_report_mod.legend(doc))
        self.assertEqual(entries["PARTNER-1"], "Far Ridge Hospital")
        self.assertEqual(entries["PARTNER-2"], "Dialysis Co")
        self.assertEqual(entries["COUNTY-A"], fp.COUNTY)
        self.assertEqual(entries["COUNTY-B"], fp.OTHER_COUNTY)
        self.assertTrue(any(k.startswith("DOCUMENT-") for k in entries))
        page = gap_report_mod.render_html(doc)
        self.assertIn("<h2>Labels</h2>", page)
        self.assertIn("Dialysis Co", page)

    def test_its_own_labels_are_identifiers_and_nothing_else_is(self):
        doc = self.build()
        exempt = gap_report_mod.identifiers(doc)
        self.assertIn("PARTNER-1", exempt)
        self.assertIn("COUNTY-A", exempt)
        self.assertIn(fp.COUNTY, exempt)
        self.assertIn(self.facility.blind_label, exempt)
        # Without them the report does not validate — the labels carry digits
        # — and with an unrelated number it still does not.
        self.assertTrue(gap_report_mod.check(doc, self.resolver, exempt=()))
        doctored = cite.to_dict(doc)
        doctored["sentences"][0]["text"] = (
            doctored["sentences"][0]["text"]
            .replace("this record is", "for 4121 occupants this record is")
        )
        found = gap_report_mod.check(cite.from_dict(doctored), self.resolver)
        self.assertEqual([v.code for v in found], [cite.NUMBER_WITHOUT_CLAIM])

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
        for key in ("lat", "Lat", "LON", "Address", "zip", "Plus_Code"):
            with self.subTest(key=key):
                doctored = cite.to_dict(doc)
                doctored["inputs"][key] = "35.9"
                found = gap_report_mod.check(
                    cite.from_dict(doctored), self.resolver
                )
                self.assertEqual([v.code for v in found], [cite.UNRESOLVED])
                self.assertIn(f"forbidden field (inputs.{key}", found[0].detail)

    def test_a_place_in_a_value_is_a_violation_so_the_rule_is_not_dead(self):
        # Finding 5 of the leakage review: `cite.to_dict` fixes the key set, so
        # a key-only scan over a rendered document could never fire at all.
        doc = self.build()
        for value in ("412 Riverside Drive", "27834-1234",
                      "35.6127, -77.3664", "ZIP 27834"):
            with self.subTest(value=value):
                doctored = cite.to_dict(doc)
                doctored["inputs"]["documents"] = f"DOCUMENT-1 = Certificate, {value}"
                found = gap_report_mod.check(
                    cite.from_dict(doctored), self.resolver
                )
                self.assertTrue(found)
                self.assertIn("forbidden field (inputs.documents", found[0].detail)
        # A county FIPS is five digits and is not a place finer than a county.
        doctored = cite.to_dict(doc)
        doctored["inputs"]["documents"] = "DOCUMENT-1 = Certificate for county 27834"
        self.assertEqual(
            gap_report_mod.check(cite.from_dict(doctored), self.resolver), []
        )


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

    def test_the_blinded_render_has_no_county_and_no_legend(self):
        # Finding 9 of the leakage review: in a rural county with one
        # critical-access hospital the FIPS is the name.
        doc = self.build()
        page, _sha = gap_report_mod.render_blinded(doc)
        self.assertNotIn(fp.COUNTY, page)
        self.assertNotIn(fp.OTHER_COUNTY, page)
        self.assertIn("COUNTY-A", page)
        self.assertNotIn("<h2>Labels</h2>", page)
        self.assertEqual(gap_report_mod.legend(gap_report_mod.blinded_document(doc)), [])
        # The claim ids that carried the FIPS are re-keyed, links included.
        blinded = gap_report_mod.blinded_document(doc)
        self.assertIn(f"risk-{fp.CONTRACT}-COUNTY-A", [c.id for c in blinded.claims])
        for claim_id in (c.id for c in blinded.claims):
            self.assertNotIn(fp.COUNTY, claim_id)

    def test_the_blinded_render_carries_no_timestamp(self):
        # Finding 17: the sha covered `generated_at`, so re-running the command
        # with identical inputs orphaned every review of the report.
        doc = self.build()
        later = cite.Document(
            doc.title, doc.kind, doc.sentences, doc.claims,
            "2099-01-01T00:00:00+00:00", doc.inputs,
        )
        first, sha_a = gap_report_mod.render_blinded(doc)
        second, sha_b = gap_report_mod.render_blinded(later)
        self.assertEqual(first, second)
        self.assertEqual(sha_a, sha_b)
        self.assertNotIn(doc.generated_at, first)
        self.assertEqual(gap_report_mod.blinded_document(doc).generated_at, "")
        # The plain page and the JSON keep theirs.
        self.assertIn(doc.generated_at, gap_report_mod.render_html(doc))
        self.assertIn(doc.generated_at, cite.to_json(doc))

    def test_the_render_refuses_a_page_that_still_names_the_record(self):
        # A3: blinding that failed must refuse, not ship. The rewriter here
        # knows a name it could not have learned from the body, and tries to
        # put it back re-cased and double-spaced.
        doc = self.build(slug="saint-example-regional")
        for smuggled in ("FAR RIDGE HOSPITAL", "Far  Ridge   Hospital",
                         "far ridge hospital", "Saint-Example-Regional",
                         fp.COUNTY):
            with self.subTest(smuggled=smuggled):
                doctored = cite.to_dict(doc)
                doctored["sentences"][0]["text"] = (
                    f"{smuggled} is the subject here "
                    f"[c:{doc.sentences[0].cited()[0]}]."
                )
                with self.assertRaises(gap_report_mod.GapReportError) as ctx:
                    gap_report_mod.render_blinded(cite.from_dict(doctored))
                self.assertIn("still holds", str(ctx.exception))

    def test_a_rewriter_that_smuggles_a_name_back_in_is_caught_by_the_render(self):
        # The end-to-end version of the guard: a drafter that somehow knows a
        # name (it is never shown one) and puts it back re-cased or
        # double-spaced. `write` refuses and the tree stays empty.
        name = "Far Ridge Hospital"
        for smuggled in (name, name.upper(), "far  ridge   hospital"):
            with self.subTest(smuggled=smuggled):
                def rewriter(sentences, claims, smuggled=smuggled):
                    body = list(sentences)
                    first = body[0]
                    markers = "".join(f"[c:{c}]" for c in first.cited())
                    body[0] = cite.Sentence.from_text(
                        f"{smuggled} is the subject of this report {markers}."
                    )
                    return body, 0

                doc = self.build(rewriter=rewriter, drafter="claude")
                with self.assertRaises(gap_report_mod.GapReportError) as ctx:
                    gap_report_mod.write(doc, self.dir, resolve=self.resolver)
                self.assertIn("still holds", str(ctx.exception))
                self.assertEqual(sorted(self.dir.iterdir()), [])

    def test_the_identifying_strings_are_the_record_s_own(self):
        doc = self.build(slug="saint-example-regional")
        strings = gap_report_mod.identifying_strings(doc)
        self.assertIn("far ridge hospital", strings)
        self.assertIn("saint-example-regional", strings)
        self.assertIn(fp.COUNTY, strings)
        self.assertIn("synthetic planning record", strings)

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

    def test_the_meta_tag_escapes_what_it_carries(self):
        # Finding 7B: the one unescaped sink in an otherwise escaped renderer.
        doc = self.build()
        doctored = cite.to_dict(doc)
        doctored["inputs"]["period"] = '"><img src=x onerror=alert(1)>'
        page, _sha = gap_report_mod.render_blinded(cite.from_dict(doctored))
        self.assertNotIn("<img src=x", page)
        self.assertIn("&quot;&gt;&lt;img", page)

    def test_the_label_is_the_record_s_blind_id_not_a_digest_of_the_slug(self):
        doc = self.build(slug="one-slug")
        mapping = gap_report_mod.blind_map(doc)
        self.assertEqual(mapping["one-slug"], self.facility.blind_label)
        self.assertRegex(self.facility.blind_label, r"^FACILITY-[0-9a-f]{12}$")
        self.assertEqual(
            self.facility.blind_label, f"FACILITY-{self.facility.blind_id[:12]}"
        )

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
    def test_writes_three_files_with_the_blinded_one_in_its_own_tree(self):
        doc = self.build()
        html_path, json_path, blind_path = gap_report_mod.write(
            doc, self.dir, resolve=self.resolver
        )
        label = self.facility.blind_label
        self.assertEqual(html_path, self.dir / "test-facility" / f"{fp.PERIOD}.html")
        self.assertEqual(json_path, self.dir / "test-facility" / f"{fp.PERIOD}.json")
        self.assertEqual(
            blind_path, self.dir / "blinded" / label / f"{fp.PERIOD}.blind.html"
        )
        # Finding 9: nothing under blinded/ carries the slug — not in a path,
        # not in the title, not in the meta tag.
        self.assertNotIn("test-facility", str(blind_path.relative_to(self.dir)))
        page = blind_path.read_text(encoding="utf-8")
        self.assertNotIn("test-facility", page)
        self.assertIn(f"<title>Gap report: {label}", page)
        # The JSON is the document the pages were rendered from.
        again = cite.from_json(json_path.read_text(encoding="utf-8"))
        self.assertEqual(again.sentences, doc.sentences)
        self.assertEqual(
            gap_report_mod.render_blinded(again)[1],
            gap_report_mod.render_blinded(doc)[1],
        )

    def test_a_period_that_is_not_a_period_is_refused_before_anything_is_written(self):
        # Finding 7A / 11: the label is a path component, so `--period
        # ../../case-studies/leaked` wrote a report naming the facility into a
        # tracked directory.
        doc = self.build()
        for period in ("../../case-studies/leaked", "not/a/period", "2026-q4",
                       "2026-Q0", "2026-Q5", "2026-M13", "2026-M0", "26-Q4",
                       '"><img src=x>', "", "2026-P3"):
            with self.subTest(period=period):
                doctored = cite.to_dict(doc)
                doctored["inputs"]["period"] = period
                with self.assertRaises(gap_report_mod.GapReportError):
                    gap_report_mod.write(
                        cite.from_dict(doctored), self.dir, resolve=self.resolver
                    )
                self.assertEqual(sorted(self.dir.iterdir()), [])
        for period in ("2026", "2026-Q1", "2026-Q4", "2026-M01", "2026-M12"):
            with self.subTest(period=period):
                self.assertEqual(gap_report_mod.check_period(period), period)

    def test_the_period_grammar_agrees_with_readiness_issue(self):
        # Spelled here rather than imported (Phase 3 has no contract in scope),
        # so a test holds the two together.
        from readiness import issue as issue_mod
        from tests.fixtures import make_contract

        # One contract per period length, because the grammar a gap report
        # accepts is the union: it has no contract in scope to narrow it.
        shapes = [make_contract(period=kind) for kind in ("year", "quarter", "month")]
        good = ["2026", "2026-Q1", "2026-Q4", "2030-M01", "2030-M12", "1999-Q2"]
        bad = ["2026-q4", "2026-Q0", "2026-Q5", "2026-M00", "2026-M13", "26-Q4",
               "2026-P3", "2026Q4", "", "not/a/period", "../../x", "20266-Q1"]

        def any_contract_accepts(label: str) -> bool:
            for contract in shapes:
                try:
                    issue_mod.parse_period(label, contract)
                    return True
                except issue_mod.IssueRefused:
                    continue
            return False

        for label in good:
            with self.subTest(label=label):
                self.assertTrue(gap_report_mod.PERIOD_RE.fullmatch(label))
                self.assertTrue(any_contract_accepts(label), label)
        for label in bad:
            with self.subTest(label=label):
                self.assertIsNone(gap_report_mod.PERIOD_RE.fullmatch(label))
                self.assertFalse(any_contract_accepts(label), label)

    def test_a_failed_write_leaves_nothing_behind(self):
        # Finding 12: three sequential writes left a .html and a .json with no
        # .blind.html — exactly the state `verify --phase 3` reads.
        doc = self.build()
        calls = []
        original = pathlib.Path.write_text

        def failing(self_path, *args, **kwargs):
            calls.append(self_path)
            if len(calls) == 3:
                raise OSError("disk full on the third write")
            return original(self_path, *args, **kwargs)

        pathlib.Path.write_text = failing
        try:
            with self.assertRaises(OSError):
                gap_report_mod.write(doc, self.dir, resolve=self.resolver)
        finally:
            pathlib.Path.write_text = original
        left = sorted(p for p in self.dir.rglob("*") if p.is_file())
        self.assertEqual(left, [], f"a partial set survived: {left}")

    def test_a_target_that_is_already_a_directory_is_refused(self):
        doc = self.build()
        blind = self.dir / "blinded" / self.facility.blind_label / f"{fp.PERIOD}.blind.html"
        blind.mkdir(parents=True)
        with self.assertRaises(gap_report_mod.GapReportError):
            gap_report_mod.write(doc, self.dir, resolve=self.resolver)
        self.assertEqual(
            sorted(p.name for p in self.dir.rglob("*") if p.is_file()), []
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

    def test_write_needs_the_facility_label_and_period_in_the_inputs(self):
        doc = self.build()
        for drop in ("facility", "period", "facility_blind"):
            with self.subTest(drop=drop):
                doctored = cite.to_dict(doc)
                del doctored["inputs"][drop]
                with self.assertRaises(gap_report_mod.GapReportError):
                    gap_report_mod.write(
                        cite.from_dict(doctored), self.dir, resolve=self.resolver
                    )
        doctored = cite.to_dict(doc)
        doctored["inputs"]["facility_blind"] = "FACILITY-nothex"
        with self.assertRaises(gap_report_mod.GapReportError):
            gap_report_mod.write(
                cite.from_dict(doctored), self.dir, resolve=self.resolver
            )


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
