from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evex_agent_messaging.mcp_server import McpServer, bearer_capability  # noqa: E402


class FakeService:
    def __init__(self):
        self.calls = []

    def create_child(self, *args, **kwargs):
        self.calls.append(("create_child", args, kwargs))
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

    def get_usage(self, *args):
        self.calls.append(("get_usage", args, {}))
        return {"cacheHitRate": 0.9}

    def inspect_authority(self, *args):
        self.calls.append(("inspect_authority", args, {}))
        return {
            "role": "writer",
            "taskKey": "writer-653",
            "allowedActions": ["cancel_mission", "send_message"],
            "expiresAt": "2026-08-20T01:00:00Z",
        }


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.server = McpServer(self.service)

    def test_initialize_and_tools_list(self):
        initialized = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "evex-agent-messaging")
        listed = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual({tool["name"] for tool in listed["result"]["tools"]}, {"create_child", "send_to_parent", "request_user_decision", "cancel_mission", "resume_mission", "publish_navigation_links", "get_usage", "inspect_authority"})
        create = next(tool for tool in listed["result"]["tools"] if tool["name"] == "create_child")
        self.assertNotIn("parentCapabilityRef", create["inputSchema"]["required"])
        self.assertNotIn("parentCapabilityRef", create["inputSchema"]["properties"])
        self.assertNotIn("parentCapability", create["inputSchema"]["properties"])
        self.assertNotIn("terminal_wake", {tool["name"] for tool in listed["result"]["tools"]})
        self.assertEqual(
            create["inputSchema"]["properties"]["capabilities"]["items"]["enum"],
            ["runtime_environment"],
        )
        self.assertEqual(create["inputSchema"]["properties"]["mission"]["type"], "object")
        self.assertIn("checkout", create["inputSchema"]["properties"]["mission"]["required"])
        self.assertIn("model", create["inputSchema"]["required"])
        self.assertIn("reasoningEffort", create["inputSchema"]["required"])
        self.assertEqual(
            create["inputSchema"]["properties"]["role"]["enum"],
            ["spec", "plan-author", "writer", "reviewer", "qa", "repair"],
        )
        self.assertEqual(
            create["inputSchema"]["properties"]["model"]["enum"],
            ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
        )
        self.assertEqual(
            create["inputSchema"]["properties"]["reasoningEffort"]["enum"],
            ["medium", "high"],
        )
        self.assertEqual(
            create["inputSchema"]["properties"]["mission"]["properties"]["skills"]["minItems"],
            1,
        )
        resume = next(tool for tool in listed["result"]["tools"] if tool["name"] == "resume_mission")
        self.assertIn("context", resume["inputSchema"]["required"])
        inspect = next(tool for tool in listed["result"]["tools"] if tool["name"] == "inspect_authority")
        self.assertEqual(
            inspect["inputSchema"],
            {"type": "object", "additionalProperties": False, "properties": {}},
        )
        for tool in listed["result"]["tools"]:
            self.assertNotIn("capabilityRef", tool["inputSchema"].get("properties", {}))

    def test_create_child_uses_transport_bound_capability(self):
        result = self.server.handle({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "create_child",
                "arguments": {
                    "taskKey": "writer-1",
                    "role": "writer",
                    "model": "gpt-5.6-terra",
                    "reasoningEffort": "medium",
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
        }, capability_ref="evx1_parent")
        self.assertNotIn("capabilityRef", result["result"]["structuredContent"])
        self.assertEqual(self.service.calls[0][1][0], "evx1_parent")

    def test_tool_call_without_transport_capability_fails_closed(self):
        result = self.server.handle({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "create_child",
                "arguments": {
                    "taskKey": "writer-1",
                    "role": "writer",
                    "model": "gpt-5.6-terra",
                    "reasoningEffort": "medium",
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
        self.assertEqual(result["error"]["code"], -32602)
        self.assertIn("transport capability", result["error"]["message"])

    def test_unknown_tool_is_a_client_error(self):
        result = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "raw_openhands_api", "arguments": {}}})
        self.assertEqual(result["error"]["code"], -32602)

    def test_get_usage_uses_transport_bound_main_capability(self):
        target = "22222222-2222-4222-8222-222222222222"
        result = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "get_usage",
                    "arguments": {"targetId": target, "taskKey": "writer-604"},
                },
            },
            capability_ref="evx1_parent",
        )

        self.assertEqual(result["result"]["structuredContent"]["cacheHitRate"], 0.9)
        self.assertEqual(self.service.calls[-1][0], "get_usage")
        self.assertEqual(self.service.calls[-1][1][0], "evx1_parent")

    def test_inspect_authority_uses_only_transport_capability_and_redacts_it(self):
        transport_capability = "evx1_transport_bound_only"
        result = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "inspect_authority", "arguments": {}},
            },
            capability_ref=transport_capability,
        )

        content = result["result"]["structuredContent"]
        self.assertEqual(set(content), {"role", "taskKey", "allowedActions", "expiresAt"})
        self.assertEqual(content["role"], "writer")
        self.assertEqual(self.service.calls[-1], ("inspect_authority", (transport_capability,), {}))
        self.assertNotIn(transport_capability, str(result))

    def test_http_bearer_capability_is_strict(self):
        self.assertEqual(bearer_capability("Bearer evx1_opaque"), "evx1_opaque")
        for value in (None, "", "evx1_opaque", "Basic evx1_opaque", "Bearer other"):
            with self.subTest(value=value):
                self.assertIsNone(bearer_capability(value))
