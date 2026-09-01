from __future__ import annotations

import json
import http.client
import io
from pathlib import Path
import sys
import unittest
import uuid
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evex_agent_messaging.mcp_server import McpServer, TOOLS, bearer_capability, make_http_server  # noqa: E402
from evex_agent_messaging.provider import OpenHandsProvider  # noqa: E402
from evex_agent_messaging.service import MessagingService  # noqa: E402
from evex_agent_messaging.capability import inspect_capability  # noqa: E402


class FakeService:
    def __init__(self):
        self.calls = []

    def send_message(self, *args):
        self.calls.append(args)
        return {"accepted": True, "messageKey": args[2]}

    def create_spec_chat(self, *args):
        self.calls.append(args)
        return {"created": True, "specChatId": "spec-id"}

    def readiness(self):
        return True

    def provisioning_allowed(self, credential):
        return credential == "private-service-key"

    def provision_project_capability(self, request):
        self.calls.append(("provision", request))
        return {"schemaVersion": 1, "conversationId": request["conversationId"],
                "projectId": "native-project-node-id", "bindingVerified": True}


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
        self.assertEqual(TOOLS[0]["inputSchema"], {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        })
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
        response = self.server.handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "create_spec_chat", "arguments": {}},
        }, capability_ref="evx2_capability")
        self.assertEqual(response["result"]["structuredContent"]["specChatId"], "spec-id")
        self.assertEqual(self.service.calls, [("evx2_capability",)])

    def test_create_spec_chat_rejects_every_legacy_argument(self):
        response = self.server.handle({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "create_spec_chat",
                "arguments": {"checkout": {"baseHead": "a" * 40}},
            },
        }, capability_ref="evx2_capability")

        self.assertEqual(response["error"], {
            "code": -32602,
            "message": "create_spec_chat accepts no arguments",
        })
        self.assertEqual(self.service.calls, [])

    def test_create_spec_chat_rejects_non_object_arguments(self):
        response = self.server.handle({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "create_spec_chat", "arguments": []},
        }, capability_ref="evx2_capability")

        self.assertEqual(response["error"], {
            "code": -32602,
            "message": "invalid messaging request",
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
        self.assertEqual(bearer_capability("Bearer evx3_test"), "evx3_test")
        for value in (None, "evx2_test", "Bearer secret", "Bearer evx2_test extra"):
            self.assertIsNone(bearer_capability(value))

    def test_project_send_uses_v3_without_private_provisioning(self):
        target = uuid.uuid4()
        for method in ("initialize", "tools/list"):
            self.server.handle({"id": 1, "method": method}, capability_ref="evx3_test")
        self.assertEqual(self.service.calls, [])
        result = self.server.handle({"id": 2, "method": "tools/call", "params": {
            "name": "send_message", "arguments": {
                "targetId": str(target), "messageKey": "key", "message": self.message(),
            },
        }}, capability_ref="evx3_test")
        self.assertEqual(result["result"]["structuredContent"], {"accepted": True, "messageKey": "key"})
        self.assertEqual(self.service.calls, [("evx3_test", target, "key", self.message())])
        for name in ("provision_project_capability", "project-capability", "mint", "resume", "create_project_chat"):
            result = self.server.handle({"id": 3, "method": "tools/call", "params": {"name": name}}, capability_ref="evx3_test")
            self.assertIn("error", result)
        self.assertEqual(len(self.service.calls), 1)


class ProjectPrivateHttpTest(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.server = McpServer(self.service)
        self.http = make_http_server(self.server, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.conversation_id = str(uuid.uuid4())

    def tearDown(self):
        self.http.shutdown()
        self.thread.join(timeout=2)
        self.http.server_close()

    def post(self, body, credential="private-service-key", path="/internal/project-capability", headers=None):
        client = http.client.HTTPConnection(*self.http.server_address, timeout=2)
        try:
            client.request("POST", path, body=body, headers={
                "Authorization": "Bearer " + credential,
                "Content-Type": "application/json", **(headers or {}),
            })
            response = client.getresponse()
            return response.status, response.read().decode()
        finally:
            client.close()

    def test_project_private_http_auth_precedes_parsing_and_calls(self):
        for credential in ("", "foreign", "evx2_test", "evx3_test", "private-service-key extra"):
            with self.subTest(credential=credential):
                status, body = self.post("private-invalid-json", credential)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body), {"error": "Project capability request denied"})
        self.assertEqual(self.service.calls, [])

    def test_project_private_http_success_returns_only_verified_binding(self):
        request = {"schemaVersion": 1, "conversationId": self.conversation_id}
        status, body = self.post(json.dumps(request))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {**request, "projectId": "native-project-node-id", "bindingVerified": True})
        self.assertEqual(self.service.calls, [("provision", request)])

    def test_project_private_http_real_service_uses_existing_exact_authenticated_host_paths(self):
        self.server._service = MessagingService(OpenHandsProvider("http://openhands", "private-service-key"), b"signing-secret")
        request = {"schemaVersion": 1, "conversationId": self.conversation_id}
        admitted = {"id": self.conversation_id, "evexProjectAdmission": {**request,
            "role": "project", "lifecycle": "eligible", "root": None,
            "project": {"id": "native-project-id", "chatId": self.conversation_id,
                        "state": "open", "subjectAccess": "allowed"},
        }}
        binding = {**request, "projectId": "native-project-id", "bindingVerified": True}
        responses = [io.BytesIO(json.dumps(value).encode()) for value in
                     (admitted, {"success": True, "evexProjectCapability": binding})]
        with patch("urllib.request.urlopen", side_effect=responses) as host:
            status, body = self.post(json.dumps(request))
        self.assertEqual((status, json.loads(body)), (200, binding))
        calls = host.call_args_list
        self.assertEqual([(call.args[0].method, call.args[0].full_url) for call in calls], [
            ("GET", f"http://openhands/api/conversations/{self.conversation_id}"),
            ("POST", f"http://openhands/api/conversations/{self.conversation_id}/secrets"),
        ])
        for call in calls:
            self.assertEqual(call.args[0].headers["X-session-api-key"], "private-service-key")
            self.assertEqual(call.kwargs["timeout"], 5.0)
        secrets = json.loads(calls[1].args[0].data)
        self.assertEqual(set(secrets), {"secrets"})
        self.assertEqual(set(secrets["secrets"]), {"EVEX_AGENT_MESSAGING_CAPABILITY"})
        secret = secrets["secrets"]["EVEX_AGENT_MESSAGING_CAPABILITY"]
        self.assertEqual(set(secret), {"kind", "value"})
        self.assertEqual(secret["kind"], "StaticSecret")
        self.assertEqual(inspect_capability(secret["value"], b"signing-secret").project_id, "native-project-id")
        self.assertNotIn(secret["value"], body)

    def test_project_private_http_real_service_rejects_extra_schema_before_host(self):
        self.server._service = MessagingService(OpenHandsProvider("http://openhands", "private-service-key"), b"signing-secret")
        for request in ({"schemaVersion": 1, "conversationId": self.conversation_id, "role": "project"},
                        {"schemaVersion": True, "conversationId": self.conversation_id}, [], {}):
            with patch("urllib.request.urlopen") as host:
                status, _ = self.post(json.dumps(request))
            self.assertEqual(status, 400)
            host.assert_not_called()

    def test_project_private_http_bounded_json_and_content_free_errors(self):
        invalid = ("private-not-json", "x" * 1025, '{"schemaVersion":1,"schemaVersion":1}')
        for value in invalid:
            status, body = self.post(value)
            self.assertEqual(status, 400)
            self.assertLess(len(body), 200)
            self.assertNotIn(value, body)
        self.assertEqual(self.service.calls, [])
        for headers in ({"Content-Length": "-1"}, {"Content-Length": "1025"}, {"Transfer-Encoding": "chunked"}):
            status, body = self.post("{}", headers=headers)
            self.assertEqual(status, 400)
        self.assertEqual(self.service.calls, [])

    def test_project_private_http_provider_errors_never_expose_outbound_content(self):
        def fail(_request):
            raise RuntimeError("private-service-key evx3_secret provider-response-body")
        self.service.provision_project_capability = fail
        status, body = self.post(json.dumps({"schemaVersion": 1, "conversationId": self.conversation_id}))
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"error": "Project capability operation failed"})

    def test_project_public_http_requests_never_provision(self):
        for method in ("initialize", "tools/list", "notifications/initialized"):
            status, _ = self.post(json.dumps({"id": 1, "method": method}), "evx3_test", "/mcp")
            self.assertIn(status, (200, 202))
        for path in ("/internal/project-capability/", "/internal/project-capability?extra=1"):
            self.assertEqual(self.post("{}", path=path)[0], 404)
        self.assertEqual(self.service.calls, [])


if __name__ == "__main__":
    unittest.main()
