from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evex_agent_messaging.mcp_server import McpServer  # noqa: E402


class FakeService:
    def create_child(self, *args):
        return {"childId": "11111111-1111-4111-8111-111111111111", "capability": "opaque"}

    def send_message(self, *args):
        return {"accepted": True}

    def cancel_mission(self, *args):
        return {"accepted": True}


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.server = McpServer(FakeService())

    def test_initialize_and_tools_list(self):
        initialized = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "evex-agent-messaging")
        listed = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual({tool["name"] for tool in listed["result"]["tools"]}, {"create_child_conversation", "send_agent_message", "cancel_agent_mission"})

    def test_unknown_tool_is_a_client_error(self):
        result = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "raw_openhands_api", "arguments": {}}})
        self.assertEqual(result["error"]["code"], -32602)

