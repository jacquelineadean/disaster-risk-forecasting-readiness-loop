"""The MCP server speaks the protocol, and cannot be used to obtain holdout labels.

The second property is the one that matters: an agent connected to this server
should have no path to a validate or test outcome, however it phrases the
request.
"""

import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import readiness
from readiness import data as data_mod
from readiness.agent import orchestrator
from readiness.connectors import mcp_server
from tests.fixtures import make_contract
from tests.test_cli import synthetic_dataset


class TestProtocol(unittest.TestCase):
    def test_initialize_handshake(self):
        resp = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assertEqual(resp["id"], 1)
        self.assertIn("protocolVersion", resp["result"])
        self.assertIn("serverInfo", resp["result"])
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_server_version_is_the_package_version(self):
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(resp["result"]["serverInfo"]["version"], readiness.__version__)

    def test_initialized_notification_gets_no_response(self):
        self.assertIsNone(
            mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_ping(self):
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(resp["result"], {})

    def test_tools_list_is_well_formed(self):
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        tools = resp["result"]["tools"]
        self.assertTrue(tools)
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertIn("description", tool)
                self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_unknown_method_errors(self):
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_unknown_tool_errors(self):
        resp = mcp_server.handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "get_holdout_labels", "arguments": {}},
            }
        )
        self.assertEqual(resp["error"]["code"], -32602)

    def test_tool_failure_is_returned_not_raised(self):
        resp = mcp_server.handle(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "get_region_history", "arguments": {}},
            }
        )
        self.assertIn("content", resp["result"])

    def test_serve_reads_ndjson_and_writes_ndjson(self):
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + "\n"
        )
        stdout = io.StringIO()
        mcp_server.serve(stdin, stdout)
        lines = [json.loads(x) for x in stdout.getvalue().strip().split("\n")]
        self.assertEqual(len(lines), 2)  # the notification produced no response
        self.assertEqual([m["id"] for m in lines], [1, 2])

    def test_malformed_json_does_not_kill_the_server(self):
        stdin = io.StringIO(
            "{not json\n" + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n"
        )
        stdout = io.StringIO()
        mcp_server.serve(stdin, stdout)
        lines = [json.loads(x) for x in stdout.getvalue().strip().split("\n")]
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["id"], 9)

    def test_valid_json_that_is_not_an_object_is_an_invalid_request(self):
        # A bare list, number or string parses fine and used to crash the
        # dispatcher; each is answered with -32600 and the server keeps serving.
        stdin = io.StringIO(
            "[1, 2]\n42\n\"ping\"\n"
            + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n"
        )
        stdout = io.StringIO()
        mcp_server.serve(stdin, stdout)
        lines = [json.loads(x) for x in stdout.getvalue().strip().split("\n")]
        self.assertEqual(len(lines), 4)
        for bad in lines[:3]:
            self.assertEqual(bad["error"]["code"], -32600)
            self.assertIsNone(bad["id"])
            self.assertIn("invalid request", bad["error"]["message"])
        self.assertEqual(lines[3], {"jsonrpc": "2.0", "id": 9, "result": {}})

    def test_handle_refuses_a_non_object_without_raising(self):
        for message in ([], 42, "ping", None):
            with self.subTest(message=message):
                resp = mcp_server.handle(message)
                self.assertEqual(resp["error"]["code"], -32600)


class TestNoHoldoutExposure(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        mcp_server.configure(self.c)

    def tearDown(self):
        mcp_server.configure(None)

    def test_no_tool_offers_holdout_outcomes(self):
        blob = json.dumps(
            [{"name": n, **s} for n, (s, _f) in mcp_server.TOOLS.items()]
        ).lower()
        for word in ("label", "outcome", "answer", "truth"):
            # Descriptions may mention these words, but only to say they are
            # unavailable. Assert no tool *name* promises them.
            self.assertNotIn(f'"name": "get_{word}', blob)

    def test_region_history_is_restricted_to_training_years(self):
        # Read the implementation's contract rather than hitting the network:
        # the tool filters on the contract's train_years and nothing else.
        import inspect

        src = inspect.getsource(mcp_server.tool_region_history)
        self.assertIn("train_years", src)
        self.assertNotIn("validate_years", src)
        self.assertNotIn("test_years", src)

    def test_contract_tool_reports_the_bound_contract(self):
        text = mcp_server.tool_contract({})
        self.assertIn(self.c.digest(), text)
        self.assertIn(self.c.hazard, text)
        self.assertIn(self.c.name, text)

    def test_server_is_bound_to_one_contract_at_a_time(self):
        other = make_contract(name="other-hazard", hazard="tornado")
        mcp_server.configure(other)
        self.assertIn("tornado", mcp_server.tool_contract({}))


class TestDataTools(unittest.TestCase):
    """The data tools, run against a synthetic dataset in place of `data.build`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.c = make_contract(name="flood-zz")
        self.ds = synthetic_dataset(self.c, self.dir)
        self.patches = [
            mock.patch.dict(os.environ, {data_mod.EXPERIMENTS_DIR_ENV: str(self.dir)}),
            mock.patch.object(mcp_server.data_mod, "build", lambda _c, **_kw: self.ds),
        ]
        for p in self.patches:
            p.start()
        mcp_server.configure(self.c)
        self.region = self.ds.panel.regions[0]

    def tearDown(self):
        mcp_server.configure(None)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def call(self, name: str, **arguments) -> str:
        resp = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}}
        )
        self.assertNotIn("isError", resp["result"], resp)
        return resp["result"]["content"][0]["text"]

    def test_panel_summary_describes_the_bound_dataset(self):
        text = self.call("get_panel_summary")
        self.assertIn("regions: 10", text)
        self.assertIn("data version: sha256:synthetic", text)
        self.assertIn("split coverage:", text)
        self.assertIn("event coverage:", text)
        self.assertIs(mcp_server._dataset(), self.ds)  # built once, then cached

    def test_region_history_over_the_training_split(self):
        text = self.call("get_region_history", region_id=self.region)
        n_train = len(self.c.train_years) * self.c.periods_per_year
        self.assertIn(f"region {self.region}: {n_train} training region-quarters", text)
        self.assertEqual(text.count("years with a damaging event"), 4)

    def test_region_history_for_one_training_year(self):
        year = self.c.train_years[3]
        text = self.call("get_region_history", region_id=self.region, year=year)
        self.assertIn(f"region {self.region}: 4 training region-quarters", text)
        self.assertEqual(text.count("years with a damaging event"), 4)

    def test_region_history_refuses_holdout_years_without_a_label(self):
        for year in (self.c.validate_years[0], self.c.test_years[-1]):
            with self.subTest(year=year):
                text = self.call("get_region_history", region_id=self.region, year=year)
                self.assertTrue(text.startswith(f"refused: year {year}"), text)
                self.assertNotIn("positive", text)
                self.assertNotIn("damaging event", text)
                # The refusal names the training window, not the holdout split.
                self.assertIn("1996-2015", text)
                self.assertNotIn(f"{year} is in", text)

    def test_region_history_with_a_bad_year_or_unknown_region(self):
        self.assertIn("error: year", self.call("get_region_history",
                                                region_id=self.region, year="soon"))
        text = self.call("get_region_history", region_id="00000")
        self.assertTrue(text.startswith("no training-split rows for region 00000"))

    def test_ledger_tool_reads_the_contracts_ledger(self):
        self.assertEqual(self.call("read_ledger"), "ledger is empty")
        orchestrator.run_local(self.c, dataset=self.ds, progress=lambda _m: None)
        summary = self.call("read_ledger")
        self.assertIn("exp-0001", summary)
        self.assertIn("chain intact", summary)
        card = json.loads(self.call("read_ledger", experiment_id="exp-0001"))
        self.assertEqual(card["contract_digest"], self.c.digest())
        self.assertIn("card_hash", card)
        self.assertEqual(self.call("read_ledger", experiment_id="exp-9999"),
                         "no experiment 'exp-9999'")

    def test_models_tool_lists_the_registry(self):
        text = self.call("list_models")
        self.assertIn("climatology-pooled", text)
        self.assertIn("leaky-oracle", text)


if __name__ == "__main__":
    unittest.main()
