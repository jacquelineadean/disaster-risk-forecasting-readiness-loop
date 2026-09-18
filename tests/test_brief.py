"""The county brief: what it says, what it cites, and what it never contains.

Everything here is built by hand — an issued file, a test card, an exposure
row — so the sentences and their citations are under test rather than the
harness that produced the numbers. The rules are `readiness.cite`'s; what this
file checks is that the document the brief builds passes them, that it is not
written when it does not, and that nothing below the county can appear in it.
"""

import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from readiness import brief, cite, contracts
from readiness import data as data_mod
from readiness.exposure.table import ExposureTable
from readiness.harness.ledger import ExperimentCard, Ledger
from readiness.issue import Issued
from tests.fixtures import make_contract
from tests.test_verify import append_card

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIPS = "99001"
LABEL = "2026-Q4"


def make_card(contract, *, model="logistic+iso", version="1.0.0", bss=0.2345) -> ExperimentCard:
    """A sealed test card with the one number the brief quotes from a ledger."""
    return ExperimentCard(
        experiment_id="exp-0002", timestamp="2026-09-01T00:00:00+00:00", model=model,
        version=version, split="test", changed="promoted", hypothesis="generalises",
        outcome="passed the contract",
        scorecard={"brier_skill_score": bss, "auc": 0.81, "train_digest": "t" * 16,
                   "feature_digest": "f" * 16},
        verdict={"passed": True, "checks": [], "contract": contract.name,
                 "contract_version": contract.version,
                 "contract_digest": contract.digest()},
        canary={"rejected": False, "findings": []},
        data_snapshot={"model_kwargs": {}}, contract_digest=contract.digest(),
    ).seal()


def make_issued(contract, *, label=LABEL, probability=0.1234, card="exp-0002") -> Issued:
    return Issued(
        contract=contract.name, contract_digest=contract.digest(),
        model="logistic+iso", version="1.0.0", model_kwargs={}, validated_by=card,
        period=(2026, 4), period_label=label, probabilities={FIPS: probability},
        train_digest="t" * 16, feature_digest="f" * 16, feature_version="fv" * 8,
        data_version="dv" * 8, harness_digest="h" * 16,
        issued_at="2026-09-18T00:00:00+00:00", inputs=("census/national_county2020",),
    )


def make_exposure(fips=FIPS, residential=1230, schools=4, hospitals=2, unknown=51):
    rows = [
        {"fips": fips, "occ_cls": "Residential", "prim_occ": "Single Family Dwelling",
         "n": residential},
        {"fips": fips, "occ_cls": "Education", "prim_occ": "Grade Schools", "n": schools},
        {"fips": fips, "occ_cls": "Commercial", "prim_occ": "Hospital", "n": hospitals},
        {"fips": fips, "occ_cls": "Weird", "prim_occ": "Spaceport", "n": unknown},
    ]
    return ExposureTable.from_counts(rows, 2023, "fema/usa_structures/99").for_county(fips)


def make_resolver(cards, issued, *, manifest=("fema/usa_structures/99",)):
    return cite.DictResolver({
        "ledger": {
            f"{name}/{card.experiment_id}": card.record() for name, card in cards.items()
        } | {card.experiment_id: card.record() for card in cards.values()},
        "issued": {
            f"issued/{one.contract}/{one.period_label}.json": {one.period_label}
            for one in issued
        },
        "manifest": set(manifest),
    })


class BriefCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.tornado = make_contract(name="tornado-zz", hazard="tornado")
        self.flood = make_contract(name="flood-zz", hazard="inland_flood")
        self.cards = {
            "tornado-zz": make_card(self.tornado),
            "flood-zz": make_card(self.flood, bss=0.0712),
        }
        self.issued = [
            make_issued(self.tornado, probability=0.1234),
            make_issued(self.flood, probability=0.0448),
        ]
        self.contracts = {"tornado-zz": self.tornado, "flood-zz": self.flood}
        self.exposure = make_exposure()
        self.resolver = make_resolver(self.cards, self.issued)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, *, issued=None, exposure=..., county="Adair County, ZZ"):
        return brief.build(
            FIPS, LABEL,
            self.issued if issued is None else issued,
            self.exposure if exposure is ... else exposure,
            self.cards, county, self.contracts,
        )

    def text(self, doc) -> str:
        return " ".join(cite.strip_markers(s.text) for s in doc.sentences)


class TestSentences(BriefCase):
    def test_cites_probability_backtest_and_exposure_per_hazard(self):
        doc = self.build(issued=[self.issued[0]])
        self.assertEqual(brief.check(doc, self.resolver), [])
        self.assertEqual(len(doc.sentences), 4)
        probability, provenance, exposure, disclaimer = doc.sentences
        claims = doc.claim_index()

        self.assertIn(
            "12% chance of at least one damaging tornado event in Adair County, ZZ "
            "during 2026-Q4", probability.text,
        )
        self.assertEqual(probability.cited(), ("tornado-zz-p",))
        self.assertEqual(claims["tornado-zz-p"].source.kind, "issued")
        self.assertEqual(claims["tornado-zz-p"].source.ref,
                         "issued/tornado-zz/2026-Q4.json#2026-Q4")

        self.assertIn(
            "This comes from logistic+iso@1.0.0, which scored a Brier skill score of "
            "+0.23 on the untouched 2021-2025 with every populated reliability bin "
            "within 5 points", provenance.text,
        )
        self.assertEqual(provenance.cited(), ("tornado-zz-bss", "tornado-zz-tolerance-pp"))
        self.assertEqual(claims["tornado-zz-bss"].source.kind, "ledger")
        self.assertEqual(claims["tornado-zz-bss"].source.ref,
                         "tornado-zz/exp-0002#scorecard.brier_skill_score")
        # The 5 is computed from the contract's tolerance, not typed in.
        self.assertEqual(claims["tornado-zz-tolerance-pp"].source.kind, "computed")
        self.assertEqual(claims["tornado-zz-tolerance-pp"].source.ref,
                         "tornado-zz-tolerance")
        self.assertEqual(claims["tornado-zz-tolerance"].value,
                         self.tornado.reliability_tolerance_pp)

        self.assertIn(
            "Adair County, ZZ holds 1,287 structures (4% unclassified)", exposure.text
        )
        self.assertIn("including 4 schools and 2 hospitals", exposure.text)
        for claim_id in ("exposure-total", "exposure-unclassified", "exposure-schools",
                         "exposure-hospitals"):
            self.assertIn(claim_id, exposure.cited())
            self.assertEqual(claims[claim_id].source.kind, "manifest")
            self.assertEqual(claims[claim_id].source.ref, "fema/usa_structures/99")
        self.assertIn(cite.NOT_A_WARNING_SENTENCE, disclaimer.text)

    def test_a_second_contract_adds_a_paragraph(self):
        one = self.build(issued=[self.issued[0]])
        two = self.build()
        self.assertEqual(len(two.sentences), 2 * len(one.sentences))
        self.assertEqual(brief.check(two, self.resolver), [])
        text = self.text(two)
        self.assertIn("damaging tornado event", text)
        self.assertIn("damaging inland flood event", text)  # the hazard, in words
        self.assertIn("4% chance", text)  # the flood contract's own probability
        # One claim per contract for the probability and the skill; one set of
        # exposure claims for the county, cited by both paragraphs.
        ids = set(two.claim_index())
        self.assertLessEqual({"tornado-zz-p", "flood-zz-p", "tornado-zz-bss",
                              "flood-zz-bss"}, ids)
        self.assertEqual(len([c for c in two.claims if c.id.startswith("exposure-")]), 4)
        self.assertEqual(two.sentences[3].text, two.sentences[7].text)

    def test_not_a_warning_line(self):
        doc = self.build()
        disclaimers = [
            s for s in doc.sentences
            if cite.strip_markers(s.text).rstrip(".") == cite.NOT_A_WARNING_SENTENCE
        ]
        self.assertEqual(len(disclaimers), 2)  # one per contract paragraph
        for sentence in disclaimers:
            self.assertEqual(sentence.cited(), (cite.NOT_A_WARNING_GUIDANCE,))
        claim = doc.claim_index()[cite.NOT_A_WARNING_GUIDANCE]
        self.assertEqual(claim.source, cite.Source("guidance", "nws-ipaws"))
        self.assertEqual(brief.check(doc, self.resolver), [])

    def test_no_forbidden_phrasing_anywhere_else(self):
        text = self.text(self.build()).lower()
        for phrase in cite.FORBIDDEN_PHRASES:
            occurrences = text.count(phrase)
            expected = 2 if phrase in ("warning", "alert") else 0  # the disclaimers
            self.assertEqual(occurrences, expected, f"{phrase!r} appears {occurrences}x")

    def test_never_says_would_touch(self):
        # Occurrence is not footprint: the brief says what the county holds.
        text = self.text(self.build()).lower()
        for phrase in ("would touch", "would reach", "would affect", "will touch"):
            self.assertNotIn(phrase, text)
        self.assertIn("holds", text)

    def test_missing_exposure_stated_not_substituted(self):
        doc = self.build(issued=[self.issued[0]], exposure=None)
        self.assertEqual(brief.check(doc, self.resolver), [])
        text = self.text(doc)
        self.assertIn(
            "No exposure layer is pinned for Adair County, ZZ; structure counts are "
            "not reported", text,
        )
        self.assertNotIn("holds", text)
        self.assertNotIn("1,287", text)
        self.assertNotIn("schools", text)
        claim = doc.claim_index()["exposure-absent"]
        self.assertEqual(claim.value, None)
        self.assertIn("state 99", claim.text)
        self.assertEqual(doc.inputs["exposure"],
                         "no USA Structures extract pinned for this state")

    def test_the_title_and_kind_name_the_county_and_period(self):
        doc = self.build()
        self.assertEqual(doc.title, "Adair County, ZZ — 2026-Q4")
        self.assertEqual(doc.kind, brief.KIND)
        self.assertEqual(doc.inputs["county"], FIPS)
        self.assertEqual(doc.inputs["period"], LABEL)
        self.assertIn("exp-0002", doc.inputs["card:tornado-zz"])
        self.assertIn("issued/tornado-zz/2026-Q4.json", doc.inputs["issued:tornado-zz"])

    def test_falls_back_to_the_fips_when_the_county_file_is_absent(self):
        doc = self.build(issued=[self.issued[0]], county="")
        self.assertEqual(brief.check(doc, self.resolver), [])
        self.assertIn(f"in {FIPS} during", self.text(doc))

    def test_refuses_to_build_without_an_issued_file_a_card_or_a_contract(self):
        with self.assertRaises(brief.BriefError):
            self.build(issued=[])
        with self.assertRaises(brief.BriefError):
            brief.build(FIPS, LABEL, self.issued, self.exposure, {}, "Adair County, ZZ",
                        self.contracts)
        with self.assertRaises(brief.BriefError):
            brief.build(FIPS, LABEL, self.issued, self.exposure, self.cards,
                        "Adair County, ZZ", {})
        with self.assertRaises(brief.BriefError):
            brief.build(FIPS, LABEL, [make_issued(self.tornado, card="exp-0009")],
                        self.exposure, self.cards, "Adair County, ZZ", self.contracts)


class TestCountyOnly(BriefCase):
    def test_never_names_sub_county(self):
        payload = cite.to_dict(self.build())
        self.assertEqual(brief.sub_county_keys(payload), [])
        blob = json.dumps(payload)
        for word in ("address", "lat", "lon", "tract", "block", "parcel"):
            self.assertNotIn(f'"{word}"', blob)

    def test_a_sub_county_key_is_a_violation(self):
        doc = self.build()
        doctored = cite.to_dict(doc)
        doctored["inputs"]["lat"] = "35.9"
        found = brief.check(cite.from_dict(doctored), self.resolver)
        self.assertEqual([v.code for v in found], [cite.UNRESOLVED])
        self.assertIn("sub-county field (inputs.lat)", found[0].detail)

    def test_the_forbidden_keys_include_every_finer_geography(self):
        for key in ("address", "lat", "lon", "tract", "block", "parcel"):
            self.assertIn(key, brief.SUB_COUNTY_KEYS)


class TestWriting(BriefCase):
    def test_writes_both_files_with_the_attribution_block(self):
        doc = self.build()
        html_path, json_path = brief.write_validated(
            doc, self.resolver, self.dir, contracts=list(self.contracts.values()),
            exposure_joined=True,
        )
        self.assertEqual(html_path, self.dir / FIPS / f"{LABEL}.html")
        self.assertEqual(json_path, self.dir / FIPS / f"{LABEL}.json")
        page = html_path.read_text(encoding="utf-8")
        self.assertIn("Adair County, ZZ — 2026-Q4", page)
        self.assertIn("NOAA National Centers for Environmental Information", page)
        self.assertIn("USA Structures (public domain), county counts only", page)
        self.assertNotIn("Building footprints", page)  # none is ever downloaded
        self.assertTrue(page.rstrip().endswith("</body></html>"))
        # The JSON is the document the page was rendered from.
        self.assertEqual(cite.from_json(json_path.read_text()).sentences, doc.sentences)

    def test_not_written_on_violation(self):
        doc = self.build()
        blind = cite.DictResolver({"ledger": {}, "issued": {}, "manifest": set()})
        with self.assertRaises(brief.BriefRefused) as ctx:
            brief.write_validated(doc, blind, self.dir)
        self.assertTrue(ctx.exception.violations)
        self.assertIn(cite.UNRESOLVED, str(ctx.exception))
        self.assertFalse((self.dir / FIPS).exists())

    def test_an_uncited_sentence_is_refused_too(self):
        doctored = cite.to_dict(self.build())
        doctored["sentences"].append({"text": "Evacuate the county.", "claim_ids": []})
        with self.assertRaises(brief.BriefRefused) as ctx:
            brief.write_validated(cite.from_dict(doctored), self.resolver, self.dir)
        self.assertIn(cite.UNCITED, str(ctx.exception))
        self.assertFalse((self.dir / FIPS).exists())

    def test_a_number_that_is_not_a_cited_value_is_refused(self):
        doctored = cite.to_dict(self.build(issued=[self.issued[0]]))
        self.assertIn("12%", doctored["sentences"][0]["text"])
        doctored["sentences"][0]["text"] = doctored["sentences"][0]["text"].replace(
            "12%", "80%"
        )
        with self.assertRaises(brief.BriefRefused) as ctx:
            brief.write_validated(cite.from_dict(doctored), self.resolver, self.dir)
        self.assertIn(cite.NUMBER_WITHOUT_CLAIM, str(ctx.exception))

    def test_write_needs_the_county_and_period_in_the_inputs(self):
        doc = self.build()
        stripped = cite.Document(
            doc.title, doc.kind, doc.sentences, doc.claims, doc.generated_at, {}
        )
        with self.assertRaises(brief.BriefError):
            brief.write(stripped, self.dir)


class TestResolverGrammar(unittest.TestCase):
    """The refs a brief writes must be the ones the website's builder accepts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.contract = make_contract(name="tornado-zz")
        self.experiments = self.dir / "experiments"
        where = data_mod.paths(self.contract, experiments_dir=self.experiments)
        self.card = append_card(Ledger(where.ledger), self.contract, "test")
        self.issued_dir = self.dir / "issued"
        make_issued(self.contract).write(self.issued_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def _build_site(self):
        spec = importlib.util.spec_from_file_location(
            "build_site_for_test", ROOT / "tools" / "build_site.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_ledger_and_issued_refs_match_the_site_builder(self):
        registry = {"tornado-zz": self.contract}
        site = self._build_site()
        with mock.patch.dict(
            "os.environ", {data_mod.EXPERIMENTS_DIR_ENV: str(self.experiments)}
        ):
            theirs_ledger = site._ledger_refs(registry)
            theirs_issued = site._issued_refs(self.issued_dir)
        mine_ledger = brief.ledger_refs(registry, experiments_dir=self.experiments)
        mine_issued = brief.issued_refs(self.issued_dir)
        self.assertEqual(sorted(mine_ledger), sorted(theirs_ledger))
        self.assertEqual(mine_ledger, theirs_ledger)
        self.assertEqual(mine_issued, theirs_issued)
        self.assertIn(f"tornado-zz/{self.card.experiment_id}", mine_ledger)
        self.assertIn(f"issued/tornado-zz/{LABEL}.json", mine_issued)

    def test_the_refs_a_brief_writes_resolve_through_that_resolver(self):
        registry = {"tornado-zz": self.contract}
        card = list(Ledger(
            data_mod.paths(self.contract, experiments_dir=self.experiments).ledger
        ).read())[0]
        doc = brief.build(
            FIPS, LABEL, [make_issued(self.contract, card=card.experiment_id)],
            make_exposure(), {"tornado-zz": card}, "Adair County, ZZ", registry,
        )
        site = self._build_site()
        with mock.patch.dict(
            "os.environ", {data_mod.EXPERIMENTS_DIR_ENV: str(self.experiments)}
        ):
            theirs = site.brief_resolver(registry, issued_dir=self.issued_dir)
        # The website knows no exposure key unless one is pinned; every other
        # ref must resolve exactly as it does for the command that wrote it.
        theirs.known["manifest"] = {"fema/usa_structures/99"}
        self.assertEqual(cite.validate(doc, theirs), [])


class TestAttribution(unittest.TestCase):
    def test_reads_the_licence_file_and_resolves_its_conditions(self):
        contract = make_contract(name="tornado-zz")
        lines = brief.attribution([contract], exposure_joined=True)
        self.assertTrue(any("NOAA" in line for line in lines))
        self.assertTrue(any("USA Structures" in line for line in lines))
        self.assertFalse(any("Building footprints" in line for line in lines))
        self.assertFalse(any("[if" in line for line in lines))
        without = brief.attribution([contract], exposure_joined=False)
        self.assertFalse(any("USA Structures" in line for line in without))

    def test_zone_expansion_adds_the_crosswalk_line(self):
        expand = make_contract(name="heat-zz", hazard="heat", zone_policy="expand")
        lines = brief.attribution([expand], exposure_joined=False)
        self.assertTrue(any("Zone-county crosswalk" in line for line in lines))

    def test_falls_back_when_the_licence_file_is_absent(self):
        contract = make_contract(name="tornado-zz")
        lines = brief.attribution(
            [contract], exposure_joined=True, path=pathlib.Path("/nonexistent.md")
        )
        self.assertIn(brief.FALLBACK_ATTRIBUTION[0], lines)
        self.assertTrue(any("USA Structures" in line for line in lines))


class TestRegistryIsUntouched(unittest.TestCase):
    def test_the_repository_ships_no_brief(self):
        # Every brief needs a passing test card, and no contract has one yet.
        committed = sorted(brief.BRIEFS_DIR.glob("*/*.json"))
        self.assertEqual(committed, [])
        self.assertTrue((brief.BRIEFS_DIR / "README.md").exists())
        self.assertTrue(contracts.registered())


if __name__ == "__main__":
    unittest.main()
