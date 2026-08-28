from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evex_agent_messaging.capability import CapabilityError  # noqa: E402
from evex_agent_messaging.mcp_server import McpServer, TOOLS, bearer_capability  # noqa: E402


class FakeService:
    def __init__(self):
        self.calls = []

    def send_message(self, *args):
        self.calls.append(args)
        return {"accepted": True, "messageKey": args[2]}

    def create_spec_chat(self, *args):
        if set(args[1]) != {"repository", "branch", "headSha"}:
            raise CapabilityError("checkout must contain repository, branch, and headSha")
        self.calls.append(args)
        return {"created": True, "specChatId": "spec-id"}

    def readiness(self):
        return True


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.service, self.server = FakeService(), McpServer(FakeService())
        self.service = self.server._service

    @staticmethod
    def message():
        return {"humanSummary": "Delivery passed", "aiEvidence": {"outcome": "passed", "evidence": [], "findings": [], "nextBoundary": "review"}}

    def test_lists_only_spec_lifecycle_and_message_tools(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(
            [tool["name"] for tool in response["result"]["tools"]],
            ["create_spec_chat", "send_message"],
        )
        self.assertEqual(TOOLS[0]["inputSchema"]["required"], ["checkout"])
        self.assertEqual(TOOLS[1]["inputSchema"]["required"], ["targetId", "messageKey", "message"])

        message_schema = TOOLS[1]["inputSchema"]["properties"]["message"]
        self.assertIn("never a JSON-encoded string", message_schema["description"])
        self.assertEqual(message_schema["required"], ["humanSummary", "aiEvidence"])
        self.assertFalse(message_schema["additionalProperties"])
        evidence_schema = message_schema["properties"]["aiEvidence"]
        self.assertEqual(
            evidence_schema["required"],
            ["outcome", "evidence", "findings", "nextBoundary"],
        )
        self.assertFalse(evidence_schema["additionalProperties"])
        self.assertEqual(
            set(evidence_schema["properties"]),
            {"outcome", "revision", "evidence", "findings", "nextBoundary"},
        )
        self.assertEqual(evidence_schema["properties"]["evidence"]["items"]["type"], "string")
        self.assertEqual(evidence_schema["properties"]["findings"]["maxItems"], 100)

    def test_create_spec_chat_uses_transport_bound_parent_capability(self):
        checkout = {
            "repository": "EvexU2/evex-u-workspace",
            "branch": "spec/issue-42",
            "headSha": "a" * 40,
        }
        response = self.server.handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "create_spec_chat", "arguments": {"checkout": checkout}},
        }, capability_ref="evx2_capability")
        self.assertEqual(response["result"]["structuredContent"]["specChatId"], "spec-id")
        self.assertEqual(self.service.calls, [("evx2_capability", checkout)])

    def test_create_spec_chat_returns_the_actionable_checkout_contract_error(self):
        response = self.server.handle({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "create_spec_chat",
                "arguments": {
                    "checkout": {
                        "repository": "EvexU2/evex-u-workspace",
                        "branch": "spec/issue-918",
                        "baseHead": "a" * 40,
                    },
                },
            },
        }, capability_ref="evx2_capability")

        self.assertEqual(response["error"], {
            "code": -32602,
            "message": "checkout must contain repository, branch, and headSha",
        })
        self.assertEqual(self.service.calls, [])

    def test_send_message_uses_transport_bound_capability(self):
        target = uuid.uuid4()
        response = self.server.handle({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "send_message", "arguments": {"targetId": str(target), "messageKey": "key", "message": self.message()}},
        }, capability_ref="evx2_capability")
        self.assertEqual(response["result"]["structuredContent"], {"accepted": True, "messageKey": "key"})
        self.assertEqual(self.service.calls, [("evx2_capability", target, "key", self.message())])

    def test_send_message_explains_stale_text_argument_without_accepting_it(self):
        target = uuid.uuid4()
        response = self.server.handle({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "send_message",
                "arguments": {
                    "targetId": str(target),
                    "messageKey": "key",
                    "text": json.dumps(self.message()),
                },
            },
        }, capability_ref="evx2_capability")

        self.assertEqual(response["error"], {
            "code": -32602,
            "message": "send_message requires the structured 'message' argument; 'text' is not accepted",
        })
        self.assertEqual(self.service.calls, [])

    def test_send_message_explains_json_encoded_message_without_accepting_it(self):
        target = uuid.uuid4()
        response = self.server.handle({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "send_message",
                "arguments": {
                    "targetId": str(target),
                    "messageKey": "key",
                    "message": json.dumps(self.message()),
                },
            },
        }, capability_ref="evx2_capability")

        self.assertEqual(response["error"], {
            "code": -32602,
            "message": "send_message 'message' must be an object, not a JSON-encoded string",
        })
        self.assertEqual(self.service.calls, [])

    def test_missing_capability_and_unknown_tool_fail_closed(self):
        target = str(uuid.uuid4())
        missing = self.server.handle({"id": 1, "method": "tools/call", "params": {"name": "send_message", "arguments": {"targetId": target, "messageKey": "key", "message": self.message()}}})
        unknown = self.server.handle({"id": 2, "method": "tools/call", "params": {"name": "create_child", "arguments": {}}}, capability_ref="evx2_capability")
        self.assertEqual(missing["error"]["code"], -32602)
        self.assertEqual(unknown["error"]["code"], -32602)
        self.assertEqual(self.service.calls, [])

    def test_initialize_reports_new_contract_version(self):
        response = self.server.handle({"id": 1, "method": "initialize"})
        self.assertEqual(response["result"]["serverInfo"]["version"], "0.3.0")

    def test_bearer_capability_is_strict(self):
        self.assertEqual(bearer_capability("Bearer evx2_test"), "evx2_test")
        for value in (None, "evx2_test", "Bearer secret", "Bearer evx2_test extra"):
            self.assertIsNone(bearer_capability(value))


if __name__ == "__main__":
    unittest.main()
