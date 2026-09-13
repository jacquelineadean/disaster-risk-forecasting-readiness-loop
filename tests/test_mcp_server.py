"""The MCP server speaks the protocol, and cannot be used to obtain holdout labels.

The second property is the one that matters: an agent connected to this server
should have no path to a validate or test outcome, however it phrases the
request.
"""

import io
import json
import unittest

from readiness.connectors import mcp_server
from tests.fixtures import make_contract


class TestProtocol(unittest.TestCase):
    def test_initialize_handshake(self):
        resp = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assertEqual(resp["id"], 1)
        self.assertIn("protocolVersion", resp["result"])
        self.assertIn("serverInfo", resp["result"])
        self.assertIn("tools", resp["result"]["capabilities"])

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


if __name__ == "__main__":
    unittest.main()
