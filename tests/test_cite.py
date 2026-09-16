"""The citation validator: every rule made to fail on its own, then all pass.

Report §7 asks that every sentence cite a computed number, a dataset row or
a named guidance document, "validated by rules before display". These tests
are those rules, one per case: a sentence with no citation, a citation to a
claim the document lacks, a claim whose source the resolver cannot find, a
number the prose invented, each identifier that is not a number, the scenario
constant that is, and the warning language that never appears — except in
the one disclaimer sentence, and only when it cites official alerting.

No network, no ledger: the resolver is `DictResolver` over in-memory sets.
"""

import unittest

from readiness import cite
from readiness.cite import Claim, DictResolver, Document, Sentence, Source

GUIDANCE_IDS = {
    "fema-cpg-101", "cms-482-15", "aspr-tracie-evac", "nfpa-110", "nfpa-99",
    "fema-elevation-certificate", "nws-ipaws",
}

GUIDANCE = cite.load_guidance()
KNOWN = {
    "ledger": {"exp-0007": {"scorecard": {"bss": 0.23}}, "exp-0003": {}},
    "issued": {"issued/flood-zz/2026-Q4.json": {"2026-Q4"}},
    "manifest": {"noaa/storm_events_2024"},
    "facility": {"power": {"fuel_hours": 72}},
    "scenario": {"96h-isolation": {"outage_hours", "water_loss_hour"}},
}
RESOLVER = DictResolver(KNOWN, GUIDANCE)

PROB = Claim("p", "chance of at least one damaging event", 0.12,
             Source("issued", "issued/flood-zz/2026-Q4.json#2026-Q4"), "{:.0%}")
BSS = Claim("bss", "Brier skill score on the test split", 0.23,
            Source("ledger", "exp-0007#scorecard.bss"), "{:+.2f}")
COUNT = Claim("n", "structures in the county", 12345,
              Source("manifest", "noaa/storm_events_2024"), "{:,}")
NWS = Claim("g1", "official alerting", None, Source("guidance", "nws-ipaws"))
CPG = Claim("g2", "planning guidance", None, Source("guidance", "fema-cpg-101"))
OUTAGE = Claim("s96", "grid power lost for", 96,
               Source("scenario", "96h-isolation#outage_hours"))
FUEL = Claim("fuel", "generator fuel on site", 72,
             Source("facility", "power.fuel_hours"), "{:d}")
ALL_CLAIMS = (PROB, BSS, COUNT, NWS, CPG, OUTAGE, FUEL)


def document(*texts: str, claims=ALL_CLAIMS) -> Document:
    return Document(
        title="County brief",
        kind="brief",
        sentences=tuple(Sentence.from_text(t) for t in texts),
        claims=tuple(claims),
        generated_at="2026-09-16T00:00:00+00:00",
        inputs={"ledger": "experiments/flood-zz/ledger.jsonl"},
    )


def codes(violations) -> list[str]:
    return [v.code for v in violations]


class TestRules(unittest.TestCase):
    def test_valid_document_has_no_violations(self):
        doc = document(
            "12% chance of at least one damaging flood event during 2026-Q4 [c:p].",
            "The model scored BSS +0.23 on the untouched 2021-2025 [c:bss].",
            "The county holds 12,345 structures [c:n].",
            "Not a warning product; official alerts come from the NWS and IPAWS [c:g1].",
        )
        self.assertEqual(cite.validate(doc, RESOLVER), [])

    def test_uncited_sentence(self):
        doc = document("Flooding is common here.")
        found = cite.validate(doc, RESOLVER)
        self.assertEqual(codes(found), [cite.UNCITED])
        self.assertEqual(found[0].sentence_index, 0)

    def test_unknown_claim_id(self):
        doc = document("Flooding is common here [c:nope].")
        found = cite.validate(doc, RESOLVER)
        self.assertEqual(codes(found), [cite.UNKNOWN_CLAIM])
        self.assertIn("nope", found[0].detail)

    def test_unresolvable_card(self):
        ghost = Claim("ghost", "a score", 0.5, Source("ledger", "exp-0099"))
        doc = document("Skill was 0.5 [c:ghost].", claims=(ghost,))
        found = cite.validate(doc, RESOLVER)
        self.assertEqual(codes(found), [cite.UNRESOLVED])
        self.assertIsNone(found[0].sentence_index)
        self.assertIn("exp-0099", found[0].detail)

    def test_unresolvable_card_field(self):
        field = Claim("f", "a score", 0.5, Source("ledger", "exp-0007#scorecard.auc"))
        doc = document("AUC was 0.5 [c:f].", claims=(field,))
        self.assertEqual(codes(cite.validate(doc, RESOLVER)), [cite.UNRESOLVED])

    def test_guidance_id_must_exist(self):
        bogus = Claim("g", "a standard", None, Source("guidance", "iso-99999"))
        doc = document("Plans follow the standard [c:g].", claims=(bogus,))
        found = cite.validate(doc, RESOLVER)
        self.assertEqual(codes(found), [cite.UNRESOLVED])
        self.assertIn("iso-99999", found[0].detail)

    def test_computed_claim_names_its_inputs(self):
        first = Claim("first", "first break, hours", 72,
                      Source("computed", "fuel+s96"))
        doc = document("Power is the first break, at 72 hours [c:first].",
                       claims=(FUEL, OUTAGE, first))
        self.assertEqual(cite.validate(doc, RESOLVER), [])
        loose = Claim("first", "first break, hours", 72, Source("computed", "fuel+nope"))
        doc = document("Power is the first break, at 72 hours [c:first].",
                       claims=(FUEL, loose))
        self.assertEqual(codes(cite.validate(doc, RESOLVER)), [cite.UNRESOLVED])

    def test_number_without_claim(self):
        doc = document("The county holds 12,345 structures and 40 schools [c:n].")
        found = cite.validate(doc, RESOLVER)
        self.assertEqual(codes(found), [cite.NUMBER_WITHOUT_CLAIM])
        self.assertIn("'40'", found[0].detail)

    def test_number_must_match_rendering(self):
        # 0.12 rendered as "12%": prose that says "0.12" is not the cited value.
        doc = document("A 0.12 chance [c:p].")
        self.assertEqual(codes(cite.validate(doc, RESOLVER)), [cite.NUMBER_WITHOUT_CLAIM])
        doc = document("A 12% chance [c:p].")
        self.assertEqual(cite.validate(doc, RESOLVER), [])

    def test_identifier_aliases_exempt(self):
        cases = {
            "alias": "Plans follow CPG 101 and 42 CFR 482.15 and NFPA 110 [c:g2].",
            "semver": "Issued by climatology@1.2.0 [c:g2].",
            "card id": "Recorded on card exp-0007 [c:g2].",
            "period": "For 2026-Q4 and 2027-M1 [c:g2].",
            "fips": "County 48201 [c:g2].",
            "year": "Tested on 2021 through 2025 [c:g2].",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertEqual(cite.validate(document(text), RESOLVER), [])

    def test_five_digits_with_a_separator_are_a_number(self):
        doc = document("County 48,201 [c:g2].")
        self.assertEqual(codes(cite.validate(doc, RESOLVER)), [cite.NUMBER_WITHOUT_CLAIM])

    def test_scenario_constant_is_a_claim(self):
        doc = document("Grid power is lost for the 96-hour scenario [c:g2].")
        found = cite.validate(doc, RESOLVER)
        self.assertEqual(codes(found), [cite.NUMBER_WITHOUT_CLAIM])
        self.assertIn("'96'", found[0].detail)
        doc = document("Grid power is lost for the 96-hour scenario [c:s96].")
        self.assertEqual(cite.validate(doc, RESOLVER), [])

    def test_forbidden_phrasing(self):
        for phrase in ("will occur", "Will Hit", "will strike", "is predicted to hit",
                       "warning", "Warnings", "alert", "alerted"):
            with self.subTest(phrase):
                doc = document(f"A flood {phrase} the county in 2026-Q4 [c:p].")
                found = cite.validate(doc, RESOLVER)
                self.assertEqual(codes(found), [cite.FORBIDDEN_PHRASE])
                self.assertIn(phrase.lower(), found[0].detail.lower())

    def test_not_a_warning_sentence_needs_its_citation(self):
        text = "Not a warning product; official alerts come from the NWS and IPAWS"
        self.assertEqual(cite.validate(document(f"{text} [c:g1]."), RESOLVER), [])
        self.assertEqual(cite.validate(document(f"{text} [c:g1]"), RESOLVER), [])
        found = cite.validate(document(f"{text} [c:g2]."), RESOLVER)
        self.assertEqual(codes(found), [cite.FORBIDDEN_PHRASE])
        self.assertIn("nws-ipaws", found[0].detail)
        # A paraphrase is not the fixed sentence, and is refused as warning language.
        found = cite.validate(document(f"{text}, not from here [c:g1]."), RESOLVER)
        self.assertEqual(codes(found), [cite.FORBIDDEN_PHRASE] * 2)

    def test_markers_and_claim_ids_both_count(self):
        explicit = Sentence("The county holds 12,345 structures.", ("n",))
        doc = Document("t", "brief", (explicit,), ALL_CLAIMS, "now")
        self.assertEqual(cite.validate(doc, RESOLVER), [])
        self.assertEqual(explicit.cited(), ("n",))
        both = Sentence("Holds 12,345 [c:n] with 12% [c:p].", ("n",))
        self.assertEqual(both.cited(), ("n", "p"))

    def test_guidance_can_be_supplied_without_the_file(self):
        # The browser sandbox has no plans/ directory; the registry is passed in.
        guidance = {"x-1": {"id": "x-1", "aliases": ["Rule 7"]}}
        resolver = DictResolver({}, guidance)
        claim = Claim("g", "a rule", None, Source("guidance", "x-1"))
        doc = document("Rule 7 applies [c:g].", claims=(claim,))
        self.assertEqual(cite.validate(doc, resolver, guidance), [])


class TestHelpers(unittest.TestCase):
    def test_numbers_in(self):
        text = "12% of 1,234 at +0.23 by T+12 on exp-0007 in 2026-Q4 v1.2.0 [c:x] 2021."
        self.assertEqual(cite.numbers_in(text), ["12%", "1,234", "0.23", "12"])
        self.assertEqual(cite.numbers_in("CPG 101 says 3 things", ["CPG 101"]), ["3"])

    def test_render_value(self):
        self.assertEqual(cite.render_value(PROB), "12%")
        self.assertEqual(cite.render_value(BSS), "+0.23")
        self.assertEqual(cite.render_value(COUNT), "12,345")
        self.assertEqual(cite.render_value(NWS), "")
        self.assertEqual(cite.render_value(Claim("s", "t", "T+12", Source("scenario", "a#b"))),
                         "T+12")

    def test_source_kind_is_closed(self):
        with self.assertRaises(ValueError):
            Source("blog", "somewhere")
        with self.assertRaises(ValueError):
            Source("ledger", "")

    def test_claim_value_types(self):
        with self.assertRaises(ValueError):
            Claim("b", "t", True, Source("ledger", "exp-0001"))
        with self.assertRaises(ValueError):
            Claim("bad id", "t", 1, Source("ledger", "exp-0001"))

    def test_duplicate_claim_ids_refused(self):
        with self.assertRaises(ValueError):
            Document("t", "brief", (), (PROB, PROB), "now")

    def test_load_guidance_ids(self):
        self.assertEqual(set(GUIDANCE), GUIDANCE_IDS)
        for entry in GUIDANCE.values():
            self.assertTrue(entry["aliases"], entry["id"])
            self.assertTrue(entry["url"].startswith("https://"), entry["id"])
        aliases = cite.guidance_aliases(GUIDANCE)
        self.assertLess(aliases.index("42 CFR 482.15"), aliases.index("482.15"))


class TestRendering(unittest.TestCase):
    def setUp(self):
        self.doc = document(
            "12% chance of a damaging flood event in 2026-Q4 [c:p].",
            "The county holds 12,345 structures [c:n] & scored BSS +0.23 [c:bss].",
            "Not a warning product; official alerts come from the NWS and IPAWS [c:g1].",
        )

    def test_render_html_has_every_marker_and_the_sources(self):
        page = cite.render_html(self.doc)
        self.assertTrue(page.startswith("<!DOCTYPE html>"))
        self.assertNotIn("<script", page)  # self-contained: no code, no external assets
        self.assertNotIn("<link", page)
        for cid in ("p", "n", "bss", "g1"):
            self.assertIn(f'href="#claim-{cid}"', page)
            self.assertIn(f'id="claim-{cid}"', page)
        for claim in self.doc.claims:
            self.assertIn(cite.render_value(claim), page)
            self.assertIn(f"({claim.source.kind} {claim.source.ref})", page)
        self.assertIn("&amp;", page)  # prose is escaped
        self.assertIn("2026-09-16T00:00:00+00:00", page)
        self.assertIn("experiments/flood-zz/ledger.jsonl", page)

    def test_render_text(self):
        text = cite.render_text(self.doc)
        self.assertIn("County brief", text)
        self.assertIn("[c:p]", text)
        self.assertIn("[n] structures in the county = 12,345 (manifest", text)
        self.assertIn("Generated 2026-09-16", text)

    def test_json_round_trip(self):
        again = cite.from_json(cite.to_json(self.doc))
        self.assertEqual(again, self.doc)
        self.assertEqual(cite.to_json(again), cite.to_json(self.doc))
        self.assertIsInstance(again.claims[2].value, int)
        self.assertEqual(cite.validate(again, RESOLVER), [])


if __name__ == "__main__":
    unittest.main()
