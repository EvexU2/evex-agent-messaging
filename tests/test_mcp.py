from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evex_agent_messaging.mcp_server import McpServer, TOOLS, bearer_capability  # noqa: E402


class FakeService:
    def __init__(self):
        self.calls = []

    def send_message(self, *args):
        self.calls.append(args)
        return {"accepted": True, "messageKey": args[2]}

    def readiness(self):
        return True


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.service, self.server = FakeService(), McpServer(FakeService())
        self.service = self.server._service

    def test_lists_exactly_one_tool(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual([tool["name"] for tool in response["result"]["tools"]], ["send_message"])
        self.assertEqual(TOOLS[0]["inputSchema"]["required"], ["targetId", "messageKey", "text"])

    def test_send_message_uses_transport_bound_capability(self):
        target = uuid.uuid4()
        response = self.server.handle({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "send_message", "arguments": {"targetId": str(target), "messageKey": "key", "text": "hello"}},
        }, capability_ref="evx1_capability")
        self.assertEqual(response["result"]["structuredContent"], {"accepted": True, "messageKey": "key"})
        self.assertEqual(self.service.calls, [("evx1_capability", target, "key", "hello")])

    def test_missing_capability_and_unknown_tool_fail_closed(self):
        target = str(uuid.uuid4())
        missing = self.server.handle({"id": 1, "method": "tools/call", "params": {"name": "send_message", "arguments": {"targetId": target, "messageKey": "key", "text": "x"}}})
        unknown = self.server.handle({"id": 2, "method": "tools/call", "params": {"name": "create_child", "arguments": {}}}, capability_ref="evx1_capability")
        self.assertEqual(missing["error"]["code"], -32602)
        self.assertEqual(unknown["error"]["code"], -32602)
        self.assertEqual(self.service.calls, [])

    def test_initialize_reports_new_contract_version(self):
        response = self.server.handle({"id": 1, "method": "initialize"})
        self.assertEqual(response["result"]["serverInfo"]["version"], "0.2.0")

    def test_bearer_capability_is_strict(self):
        self.assertEqual(bearer_capability("Bearer evx1_test"), "evx1_test")
        for value in (None, "evx1_test", "Bearer secret", "Bearer evx1_test extra"):
            self.assertIsNone(bearer_capability(value))


if __name__ == "__main__":
    unittest.main()
