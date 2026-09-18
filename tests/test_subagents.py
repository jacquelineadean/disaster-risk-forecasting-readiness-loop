"""readiness/agent/subagents.py: the subagent set a loop gets for one contract.

Report §4: one hazard-analyst subagent per peril, a calibration critic and a
data steward, none of them granted a write tool. These tests pin that shape
against a synthetic contract rather than a registered one, so they need no
data on disk.
"""

import unittest

from readiness.agent.subagents import (
    CALIBRATION_CRITIC,
    all_hazard_analysts,
    data_steward,
    hazard_analyst,
    subagents_for,
)
from readiness import contracts
from tests.fixtures import make_contract

WRITE_TOOLS = {"Write", "Edit"}


class TestSubagentsFor(unittest.TestCase):
    def setUp(self):
        self.contract = make_contract(hazard="tornado", scope={"states": ["OK"]})
        self.subagents = subagents_for(self.contract)

    def test_one_analyst_named_after_the_contracts_hazard(self):
        analyst_keys = [k for k in self.subagents if k.startswith("hazard-analyst-")]
        self.assertEqual(analyst_keys, ["hazard-analyst-tornado"])

    def test_critic_and_steward_are_present(self):
        self.assertIn("calibration-critic", self.subagents)
        self.assertIn("data-steward", self.subagents)

    def test_exactly_three_subagents(self):
        self.assertEqual(len(self.subagents), 3)

    def test_no_subagent_has_a_write_tool(self):
        for name, spec in self.subagents.items():
            with self.subTest(subagent=name):
                self.assertFalse(
                    WRITE_TOOLS & set(spec["tools"]),
                    f"{name} was granted a write tool: {spec['tools']}",
                )

    def test_analyst_prompt_names_the_contracts_hazard_and_scope(self):
        analyst = self.subagents["hazard-analyst-tornado"]
        self.assertIn("tornado", analyst["prompt"])
        self.assertIn(self.contract.scope_label, analyst["prompt"])
        self.assertIn(self.contract.name, analyst["prompt"])

    def test_a_different_hazard_gets_a_differently_named_analyst(self):
        flood_contract = make_contract(hazard="inland_flood", scope={"states": ["LA"]})
        flood_subagents = subagents_for(flood_contract)
        self.assertIn("hazard-analyst-inland-flood", flood_subagents)
        self.assertNotIn("hazard-analyst-inland-flood", self.subagents)


class TestNoSubagentEverHasWriteOrEdit(unittest.TestCase):
    """The invariant holds for every registered contract, not just the fixture's."""

    def test_all_hazard_analysts(self):
        for name, spec in all_hazard_analysts().items():
            with self.subTest(subagent=name):
                self.assertFalse(WRITE_TOOLS & set(spec["tools"]))

    def test_calibration_critic(self):
        self.assertFalse(WRITE_TOOLS & set(CALIBRATION_CRITIC["tools"]))

    def test_data_steward(self):
        self.assertFalse(WRITE_TOOLS & set(data_steward()["tools"]))

    def test_every_registered_contract_has_an_analyst_keyed_by_its_name(self):
        registry = contracts.registered()
        analysts = all_hazard_analysts()
        self.assertEqual(list(analysts), sorted(registry))
        for name, c in registry.items():
            with self.subTest(contract=name):
                self.assertIn(f"contract `{name}`", analysts[name]["prompt"])
                self.assertIn(c.hazard, analysts[name]["prompt"])
                self.assertIn(c.scope_label, analysts[name]["prompt"])

    def test_an_injected_registry_replaces_the_registered_set(self):
        registry = {
            "heat-yy": make_contract(name="heat-yy", hazard="heat", period="month",
                                     scope={"states": ["YY"]}),
            "flood-zz": make_contract(name="flood-zz"),
        }
        analysts = all_hazard_analysts(registry)
        self.assertEqual(list(analysts), ["flood-zz", "heat-yy"])
        self.assertIn("month", analysts["heat-yy"]["prompt"])
        self.assertIn("US, YY", analysts["heat-yy"]["prompt"])
        self.assertEqual(all_hazard_analysts({}), {})


class TestHazardAnalystWithoutAContract(unittest.TestCase):
    """`hazard_analyst` also has to work generically, for Phase 2's fleet."""

    def test_prompt_names_the_hazard_but_not_a_contract(self):
        analyst = hazard_analyst("wildfire")
        self.assertIn("wildfire", analyst["prompt"])
        self.assertNotIn("contract `", analyst["prompt"])


if __name__ == "__main__":
    unittest.main()
