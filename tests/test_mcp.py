from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evex_agent_messaging.mcp_server import McpServer  # noqa: E402


class FakeService:
    def create_child(self, *args):
        return {"childId": "11111111-1111-4111-8111-111111111111", "capabilityRef": "evx1_opaque"}

    def send_message(self, *args):
        return {"accepted": True}

    def cancel_mission(self, *args):
        return {"accepted": True}

    def resume_mission(self, *args):
        return {"accepted": True}

    def send_to_parent(self, *args):
        return {"accepted": True}

    def request_user_decision(self, *args):
        return {"accepted": True}

    def publish_navigation_links(self, *args):
        return {"accepted": True}


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.server = McpServer(FakeService())

    def test_initialize_and_tools_list(self):
        initialized = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "evex-agent-messaging")
        listed = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual({tool["name"] for tool in listed["result"]["tools"]}, {"create_child", "send_to_parent", "request_user_decision", "cancel_mission", "resume_mission", "publish_navigation_links"})
        create = next(tool for tool in listed["result"]["tools"] if tool["name"] == "create_child")
        self.assertIn("parentCapabilityRef", create["inputSchema"]["required"])
        self.assertNotIn("parentCapability", create["inputSchema"]["properties"])
        self.assertNotIn("terminal_wake", {tool["name"] for tool in listed["result"]["tools"]})
        self.assertEqual(
            create["inputSchema"]["properties"]["capabilities"]["items"]["enum"],
            ["runtime_environment"],
        )
        self.assertEqual(create["inputSchema"]["properties"]["mission"]["type"], "object")
        self.assertIn("checkout", create["inputSchema"]["properties"]["mission"]["required"])

    def test_create_child_uses_only_opaque_reference_field(self):
        result = self.server.handle({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "create_child",
                "arguments": {
                    "parentCapabilityRef": "evx1_parent",
                    "taskKey": "writer-1",
                    "role": "writer",
                    "mission": {
                        "immediateTask": "Your task now: implement.",
                        "links": {},
                        "checkout": {"repository": "EvexU2/evex-u-core", "branch": "fix/1", "headSha": "a" * 40},
                        "allowedMutations": [],
                        "prohibitions": [],
                        "skills": ["evex-delivery-writer"],
                        "evidence": ["tests"],
                    },
                },
            },
        })
        self.assertEqual(result["result"]["structuredContent"]["capabilityRef"], "evx1_opaque")

    def test_unknown_tool_is_a_client_error(self):
        result = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "raw_openhands_api", "arguments": {}}})
        self.assertEqual(result["error"]["code"], -32602)
