"""The rules: every status on every question, and the one that refuses to run.

The tests that matter most here are the fail-closed pair. A missing design
intensity must produce exactly one `cannot_run` finding, naming the Elevation
Certificate — and *no rule anywhere* may reach for the county probability to
fill the hole, which is checked by running every rule with a risk layer full of
probabilities and asserting the switchgear question cites none of them.
"""

import unittest

from readiness import cite
from readiness.plans import rules as rules_mod
from readiness.plans.risk import RiskLayer
from readiness.plans.rules import Finding, RuleError
from tests import fixtures_plans as fp


class RuleCase(unittest.TestCase):
    def setUp(self):
        self.scenario = fp.scenario()
        self.risk = fp.make_risk()

    def run_rules(self, **overrides) -> dict[str, Finding]:
        facility = fp.make_facility(**overrides)
        self.facility = facility
        findings = rules_mod.run(facility, self.risk, self.scenario)
        return {f.question_id: f for f in findings}

    def text(self, finding: Finding) -> str:
        return finding.text()

    def kinds(self, finding: Finding) -> set[str]:
        return {c.source.kind for c in finding.claims}

    def assert_validates(self, finding: Finding, facility=None):
        """Every sentence of a finding passes the citation rules on its own."""
        from readiness.plans import gap_report as gap_report_mod

        facility = facility or self.facility
        doc = cite.Document(
            "probe", "gap-report", finding.sentences, finding.claims, "", {}
        )
        resolve = gap_report_mod.resolver(facility, self.scenario, self.risk)
        self.assertEqual(cite.validate(doc, resolve), [])


class TestEveryRuleProducesOneFindingPerQuestion(RuleCase):
    def test_one_finding_per_question_in_order(self):
        facility = fp.make_facility()
        findings = rules_mod.run(facility, self.risk, self.scenario)
        self.assertEqual(
            [f.question_id for f in findings], [q.id for q in self.scenario.questions]
        )
        for finding in findings:
            self.assertIn(finding.status, rules_mod.STATUSES)
            self.assertTrue(finding.sentences)
            self.assertTrue(finding.claims)

    def test_every_rule_in_the_table_is_used_by_the_scenario(self):
        used = {q.rule for q in self.scenario.questions}
        self.assertEqual(used, set(rules_mod.RULES))

    def test_a_finding_with_an_unknown_status_is_refused(self):
        with self.assertRaises(RuleError):
            Finding("q1", "maybe")

    def test_a_scenario_naming_an_unknown_rule_is_refused(self):
        import dataclasses

        from readiness.plans.scenarios import Question

        broken = dataclasses.replace(
            self.scenario,
            questions=(Question("q1", "?", "no_such_rule", ()),),
        )
        with self.assertRaises(RuleError) as ctx:
            rules_mod.run(fp.make_facility(), self.risk, broken)
        self.assertIn("no_such_rule", str(ctx.exception))


class TestSwitchgearVsIntensity(RuleCase):
    def test_missing_intensity_fails_closed_naming_the_document(self):
        findings = self.run_rules(**{"design_intensity.flood_elevation_ft": None})
        cannot_run = [f for f in findings.values() if f.status == "cannot_run"]
        self.assertEqual(len(cannot_run), 1)
        finding = cannot_run[0]
        self.assertEqual(finding.question_id, "q1")
        self.assertIn(
            "fema-elevation-certificate",
            {c.source.ref for c in finding.claims if c.source.kind == "guidance"},
        )
        self.assertIn("Elevation Certificate", self.text(finding))
        self.assertIn("design_intensity.flood_elevation_ft", self.text(finding))
        self.assert_validates(finding)

    def test_a_missing_elevation_also_fails_closed(self):
        for path in ("power.switchgear_elevation_ft", "power.transfer_switch_elevation_ft"):
            with self.subTest(path=path):
                findings = self.run_rules(**{path: None})
                self.assertEqual(findings["q1"].status, "cannot_run")
                self.assertIn(path, self.text(findings["q1"]))

    def test_no_rule_substitutes_a_county_probability_for_the_intensity(self):
        # The risk layer is full of probabilities for both counties; the
        # switchgear question must not cite one, whatever the record says.
        for overrides in ({"design_intensity.flood_elevation_ft": None},
                          {"power.switchgear_elevation_ft": 2},
                          {}):
            with self.subTest(overrides=overrides):
                findings = self.run_rules(**overrides)
                finding = findings["q1"]
                self.assertNotIn("issued", self.kinds(finding))
                for claim in finding.claims:
                    self.assertNotEqual(claim.source.kind, "issued")
                self.assertNotIn("%", self.text(finding))

    def test_the_rule_ignores_the_risk_layer_entirely(self):
        facility = fp.make_facility()
        with_layer = rules_mod.switchgear_vs_intensity(facility, self.risk, self.scenario)
        without = rules_mod.switchgear_vs_intensity(
            facility, RiskLayer.empty(fp.PERIOD), self.scenario
        )
        self.assertEqual(with_layer, without)

    def test_switchgear_below_design_elevation_is_failed_citing_both_numbers(self):
        findings = self.run_rules(**{"power.switchgear_elevation_ft": 4})
        finding = findings["q1"]
        self.assertEqual(finding.status, "failed")
        text = self.text(finding)
        self.assertIn("4 feet", text)
        self.assertIn("10 feet", text)
        refs = {c.source.ref for c in finding.claims if c.source.kind == "facility"}
        self.assertIn("power.switchgear_elevation_ft", refs)
        self.assertIn("design_intensity.flood_elevation_ft", refs)
        self.assert_validates(finding)

    def test_a_transfer_switch_below_the_elevation_is_enough(self):
        findings = self.run_rules(**{"power.transfer_switch_elevation_ft": 3})
        self.assertEqual(findings["q1"].status, "failed")
        self.assertIn("transfer switch", self.text(findings["q1"]))

    def test_both_above_the_elevation_is_answered(self):
        findings = self.run_rules()
        self.assertEqual(findings["q1"].status, "answered")
        self.assert_validates(findings["q1"])


class TestWrittenTrigger(RuleCase):
    def test_answered_needs_a_written_trigger_an_authority_and_a_lead_time(self):
        self.assertEqual(self.run_rules()["q2"].status, "answered")
        for override in ({"evacuation.trigger_written": False},
                         {"evacuation.authority": None},
                         {"evacuation.transport_lead_hours": None}):
            with self.subTest(override=override):
                finding = self.run_rules(**override)["q2"]
                self.assertEqual(finding.status, "unanswered")
                self.assertIn(list(override)[0], self.text(finding))
                self.assert_validates(finding)

    def test_lead_time_past_the_road_closure_is_failed(self):
        findings = self.run_rules(**{"evacuation.transport_lead_hours": 24})
        finding = findings["q2"]
        self.assertEqual(finding.status, "failed")
        self.assertIn("24 hours of notice", self.text(finding))
        self.assertIn("hour 12", self.text(finding))
        self.assert_validates(finding)

    def test_the_road_hour_comes_from_the_scenario_not_from_prose(self):
        finding = self.run_rules(**{"evacuation.transport_lead_hours": 24})["q2"]
        road = next(c for c in finding.claims if c.source.kind == "scenario")
        self.assertEqual(road.source.ref, "96h-isolation-acute-care/road_access_lost_hour")
        self.assertEqual(road.value, 12)

    def test_a_lead_time_equal_to_the_closure_hour_still_answers(self):
        self.assertEqual(
            self.run_rules(**{"evacuation.transport_lead_hours": 12})["q2"].status,
            "answered",
        )


class TestPriorityOrder(RuleCase):
    def test_an_unwritten_order_is_the_finding(self):
        finding = self.run_rules(**{"evacuation.priority_order_written": False})["q3"]
        self.assertEqual(finding.status, "failed")
        self.assertIn("not written down in advance", self.text(finding))
        self.assert_validates(finding)

    def test_written_but_undated_is_unanswered_which_is_a_finding(self):
        finding = self.run_rules(**{"evacuation.priority_decided_on": None})["q3"]
        self.assertEqual(finding.status, "unanswered")
        self.assertTrue(finding.is_gap)
        self.assert_validates(finding)

    def test_written_and_dated_is_answered(self):
        self.assertEqual(self.run_rules()["q3"].status, "answered")


class TestPartnerCorrelatedFailure(RuleCase):
    def test_no_signed_agreement_is_unanswered(self):
        finding = self.run_rules(**{"transfer_agreements.0.signed": False})["q4"]
        self.assertEqual(finding.status, "unanswered")
        self.assertIn("no signed transfer agreement", self.text(finding))

    def test_a_same_county_partner_is_a_correlated_failure(self):
        finding = self.run_rules(**{"transfer_agreements.0.county_fips": fp.COUNTY})["q4"]
        self.assertEqual(finding.status, "failed")
        self.assertIn("same county", self.text(finding))
        self.assert_validates(finding)

    def test_the_same_floodplain_or_grid_feeder_is_enough(self):
        for path, needle in (("transfer_agreements.0.same_floodplain", "floodplain"),
                             ("transfer_agreements.0.same_grid_feeder", "grid feeder")):
            with self.subTest(path=path):
                finding = self.run_rules(**{path: True})["q4"]
                self.assertEqual(finding.status, "failed")
                self.assertIn(needle, self.text(finding))

    def test_both_counties_probabilities_are_cited_as_context(self):
        finding = self.run_rules()["q4"]
        issued = [c for c in finding.claims if c.source.kind == "issued"]
        self.assertEqual(len(issued), 2)
        self.assertIn("For context", self.text(finding))
        self.assert_validates(finding)

    def test_the_context_is_not_the_criterion(self):
        # The same record with and without a risk layer reaches the same verdict.
        facility = fp.make_facility(**{"transfer_agreements.0.same_floodplain": True})
        with_layer = rules_mod.partner_correlated_failure(
            facility, self.risk, self.scenario
        )
        without = rules_mod.partner_correlated_failure(
            facility, RiskLayer.empty(fp.PERIOD), self.scenario
        )
        self.assertEqual(with_layer.status, without.status)
        self.assertEqual(without.status, "failed")

    def test_absence_is_stated_not_substituted(self):
        facility = fp.make_facility()
        finding = rules_mod.partner_correlated_failure(
            facility, RiskLayer.empty(fp.PERIOD), self.scenario
        )
        absent = [c for c in finding.claims if c.id.startswith("risk-absent-")]
        self.assertTrue(absent)
        self.assertEqual(absent[0].source.kind, "computed")
        self.assertIsNone(absent[0].value)
        self.assertIn("no validated issuance covers", self.text(finding))
        self.assertNotIn("%", self.text(finding))

    def test_an_uncorrelated_signed_partner_is_answered(self):
        self.assertEqual(self.run_rules()["q4"].status, "answered")


class TestCampusSeam(RuleCase):
    def test_no_co_located_operator_is_answered(self):
        finding = self.run_rules()["q5"]
        self.assertEqual(finding.status, "answered")
        self.assertIn("no other operator", self.text(finding))

    def test_a_co_located_operator_is_unanswered_and_names_the_occupants(self):
        finding = self.run_rules(
            co_located_operators=[{"name": "Dialysis Co", "occupants": 9}]
        )["q5"]
        self.assertEqual(finding.status, "unanswered")
        self.assertIn("Dialysis Co", self.text(finding))
        self.assertIn("9 occupants", self.text(finding))
        self.assertIn("no field for a joint plan", self.text(finding))
        self.assert_validates(finding)


class TestFirstBreak(RuleCase):
    def test_the_break_hour_is_a_computed_claim_over_fuel_and_water(self):
        finding = self.run_rules(**{"water.on_site_storage_hours": 12})["q6"]
        computed = [c for c in finding.claims if c.source.kind == "computed"]
        self.assertEqual(len(computed), 1)
        claim = computed[0]
        self.assertEqual(claim.id, "first-break-hour")
        self.assertEqual(claim.value, 18)  # 12 hours of water from the T+6 inject
        self.assertIn("f-water.on_site_storage_hours", claim.source.ref)
        self.assertIn("s-water_loss_hour", claim.source.ref)
        self.assertIn("hour 18", self.text(finding))
        self.assert_validates(finding)

    def test_fuel_can_be_the_first_break(self):
        finding = self.run_rules(**{"power.fuel_hours": 10})["q6"]
        self.assertEqual(finding.status, "failed")
        self.assertIn("generator fuel runs out", self.text(finding))
        self.assertIn("hour 10", self.text(finding))

    def test_no_generator_breaks_at_hour_zero(self):
        finding = self.run_rules(**{"power.generator": False})["q6"]
        self.assertEqual(finding.status, "failed")
        self.assertIn("no generator at all", self.text(finding))
        self.assertIn("hour 0", self.text(finding))

    def test_reserves_past_the_isolation_length_are_answered(self):
        finding = self.run_rules()["q6"]
        self.assertEqual(finding.status, "answered")
        self.assertIn("96 hours", self.text(finding))
        self.assert_validates(finding)

    def test_missing_water_storage_is_stated_not_assumed(self):
        finding = self.run_rules(**{"water.on_site_storage_hours": None})["q6"]
        self.assertIn("does not say how many hours of water", self.text(finding))
        self.assertNotIn("stored water runs out", self.text(finding))
        self.assert_validates(finding)

    def test_the_fallback_is_named_as_the_planner_s_to_write(self):
        self.assertIn("fallback is the planner's to write", self.text(self.run_rules()["q6"]))


class TestClaimBuilders(unittest.TestCase):
    def test_facility_refs_cover_null_leaves_and_list_items(self):
        facility = fp.make_facility(**{"design_intensity.design_wind_mph": None})
        refs = rules_mod.facility_refs(facility)
        self.assertIn("design_intensity.design_wind_mph", refs)
        self.assertIn("transfer_agreements.0.signed", refs)
        self.assertIn("power", refs)

    def test_a_number_claim_refuses_a_field_that_is_not_a_number(self):
        facility = fp.make_facility()
        with self.assertRaises(RuleError):
            rules_mod.number_claim(facility, "evacuation.trigger_written")

    def test_a_fact_claim_never_carries_a_value(self):
        claim = rules_mod.fact_claim("evacuation.trigger_written")
        self.assertIsNone(claim.value)
        self.assertEqual(claim.source.kind, "facility")

    def test_every_guidance_id_a_rule_names_is_in_the_registry(self):
        registry = cite.load_guidance()
        for ids in rules_mod.GUIDANCE.values():
            for guidance_id in ids:
                self.assertIn(guidance_id, registry)


if __name__ == "__main__":
    unittest.main()
