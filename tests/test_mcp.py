from __future__ import annotations

from contextlib import contextmanager
from http.client import HTTPConnection
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evex_agent_messaging.mcp_server import (  # noqa: E402
    McpServer,
    bearer_capability,
    main,
    make_http_server,
)
from evex_agent_messaging.provider import RetryableProviderError  # noqa: E402


class FakeService:
    def __init__(self):
        self.calls = []
        self.readiness_result = False

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

    def send_callback_fallback(self, *args):
        self.calls.append(("send_callback_fallback", args, {}))
        return {"accepted": True, "replayed": False}

    def request_user_decision(self, *args):
        return {"accepted": True}

    def publish_navigation_links(self, *args):
        return {"accepted": True}

    def get_usage(self, *args):
        self.calls.append(("get_usage", args, {}))
        return {"cacheHitRate": 0.9}

    def readiness(self):
        self.calls.append(("readiness", (), {}))
        if isinstance(self.readiness_result, Exception):
            raise self.readiness_result
        return self.readiness_result


@contextmanager
def running_http_server(server):
    httpd = make_http_server(server, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


def request_http(address, method, path, body=None, headers=None):
    connection = HTTPConnection(*address, timeout=2)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.server = McpServer(self.service)

    def test_initialize_and_tools_list(self):
        initialized = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "evex-agent-messaging")
        listed = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual({tool["name"] for tool in listed["result"]["tools"]}, {"create_child", "send_to_parent", "send_callback_fallback", "request_user_decision", "cancel_mission", "resume_mission", "publish_navigation_links", "get_usage"})
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
        self.assertEqual(
            create["inputSchema"]["properties"]["mission"]["properties"]["displayTitle"],
            {"type": "string", "minLength": 3, "maxLength": 60},
        )
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

    def test_callback_fallback_has_no_caller_authority_arguments(self):
        result = self.server.handle({
            "jsonrpc": "2.0", "id": 41, "method": "tools/call",
            "params": {"name": "send_callback_fallback", "arguments": {}},
        }, capability_ref="evx1_opaque")
        self.assertEqual(result["result"]["structuredContent"], {"accepted": True, "replayed": False})
        self.assertEqual(self.service.calls[-1], ("send_callback_fallback", ("evx1_opaque",), {}))
        rejected = self.server.handle({
            "jsonrpc": "2.0", "id": 42, "method": "tools/call",
            "params": {"name": "send_callback_fallback", "arguments": {"body": "forged"}},
        }, capability_ref="evx1_opaque")
        self.assertIn("accepts no arguments", rejected["error"]["message"])
        self.assertEqual(len(self.service.calls), 1)

    def test_retryable_provider_failure_has_stable_mcp_discriminator(self):
        def fail(*_args):
            raise RetryableProviderError("credential-free internal detail")

        self.service.send_to_parent = fail
        result = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 43,
                "method": "tools/call",
                "params": {"name": "send_to_parent", "arguments": {"result": {}}},
            },
            capability_ref="evx1_opaque",
        )

        self.assertEqual(result["error"], {
            "code": -32001,
            "message": "EVEX_RETRYABLE_MESSAGING_TRANSPORT",
        })

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

    def test_http_bearer_capability_is_strict(self):
        self.assertEqual(bearer_capability("Bearer evx1_opaque"), "evx1_opaque")
        for value in (None, "", "evx1_opaque", "Basic evx1_opaque", "Bearer other"):
            with self.subTest(value=value):
                self.assertIsNone(bearer_capability(value))

    def test_http_healthz_is_process_only(self):
        self.service.readiness_result = True

        with running_http_server(self.server) as address:
            status, body = request_http(address, "GET", "/healthz")

        self.assertEqual((status, body), (200, b"ok\n"))
        self.assertEqual(self.service.calls, [])

    def test_http_readyz_has_exact_success_and_failure_contracts(self):
        cases = {
            "complete configuration and active profile": (True, 200, b"ok\n"),
            "incomplete configuration": (False, 503, b"unavailable\n"),
            "timeout": (TimeoutError(), 503, b"unavailable\n"),
            "connection failure": (ConnectionError(), 503, b"unavailable\n"),
            "authentication failure": (PermissionError(), 503, b"unavailable\n"),
            "non-success response": (RuntimeError(), 503, b"unavailable\n"),
            "invalid or missing active profile": (False, 503, b"unavailable\n"),
        }
        for name, (result, expected_status, expected_body) in cases.items():
            with self.subTest(name=name):
                self.service.readiness_result = result
                with running_http_server(self.server) as address:
                    status, body = request_http(address, "GET", "/readyz")
                self.assertEqual((status, body), (expected_status, expected_body))

    def test_http_readyz_fails_closed_for_incomplete_server_construction(self):
        incomplete_server = McpServer(object())

        with running_http_server(incomplete_server) as address:
            status, body = request_http(address, "GET", "/readyz")

        self.assertEqual((status, body), (503, b"unavailable\n"))

    def test_http_mcp_does_not_perform_a_readiness_read(self):
        request = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
        with running_http_server(self.server) as address:
            status, body = request_http(
                address,
                "POST",
                "/mcp",
                request,
                {"Content-Type": "application/json", "Content-Length": str(len(request))},
            )

        self.assertEqual(status, 200)
        self.assertIn(b'"protocolVersion"', body)
        self.assertEqual(self.service.calls, [])

    def test_main_rejects_absent_blank_and_whitespace_required_configuration_before_construction(self):
        valid = {
            "EVEX_MESSAGING_SECRET": "secret",
            "OPENHANDS_URL": "http://openhands",
            "OPENHANDS_API_KEY": "key",
            "OPENHANDS_PUBLIC_URL": "http://public",
            "EVEX_MESSAGING_FALLBACK_GITHUB_TOKEN": "fallback-token",
            "EVEX_MESSAGING_FALLBACK_GITHUB_APP_LOGIN": "fallback[bot]",
        }
        for name in valid:
            for label, value in (("absent", None), ("blank", ""), ("whitespace", " \t")):
                with self.subTest(name=name, label=label):
                    environment = dict(valid)
                    if value is None:
                        environment.pop(name)
                    else:
                        environment[name] = value
                    with patch.dict(os.environ, environment, clear=True), patch(
                        "evex_agent_messaging.mcp_server.OpenHandsProvider"
                    ) as provider:
                        with self.assertRaises(SystemExit) as raised:
                            main()
                    self.assertNotEqual(raised.exception.code, 0)
                    provider.assert_not_called()

    def test_main_requires_complete_callback_fallback_configuration(self):
        valid = {
            "EVEX_MESSAGING_SECRET": "secret",
            "OPENHANDS_URL": "http://openhands",
            "OPENHANDS_API_KEY": "key",
            "OPENHANDS_PUBLIC_URL": "http://public",
            "EVEX_MESSAGING_FALLBACK_GITHUB_TOKEN": "fallback-token",
            "EVEX_MESSAGING_FALLBACK_GITHUB_APP_LOGIN": "fallback[bot]",
        }
        for missing in (
            "EVEX_MESSAGING_FALLBACK_GITHUB_TOKEN",
            "EVEX_MESSAGING_FALLBACK_GITHUB_APP_LOGIN",
        ):
            with self.subTest(missing=missing):
                environment = dict(valid)
                environment.pop(missing)
                with patch.dict(os.environ, environment, clear=True), patch(
                    "evex_agent_messaging.mcp_server.OpenHandsProvider"
                ) as provider:
                    with self.assertRaisesRegex(SystemExit, "are required"):
                        main()
                provider.assert_not_called()

    def test_module_exits_nonzero_for_whitespace_startup_configuration(self):
        environment = {
            "PYTHONPATH": str(ROOT / "src"),
            "EVEX_MESSAGING_SECRET": "secret",
            "OPENHANDS_URL": "http://openhands",
            "OPENHANDS_API_KEY": " \t",
            "OPENHANDS_PUBLIC_URL": "http://public",
        }

        result = subprocess.run(
            [sys.executable, "-m", "evex_agent_messaging"],
            capture_output=True,
            env=environment,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OPENHANDS_URL, OPENHANDS_API_KEY, and OPENHANDS_PUBLIC_URL are required", result.stderr)
