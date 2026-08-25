"""Minimal MCP stdio server with provider-neutral agent messaging tools."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .provider import OpenHandsProvider
from .service import MessagingService
from .fallback import GitHubCallbackFallbackAdapter


TOOLS = [
    {
        "name": "create_child",
        "description": "Create or recover one deterministic Child Conversation using the transport-bound Main capability. Use only for a bounded mission; never use it to create peer or nested delivery owners.",
        "inputSchema": {"type": "object", "additionalProperties": False, "required": ["taskKey", "role", "model", "reasoningEffort", "mission"], "properties": {"taskKey": {"type": "string"}, "role": {"type": "string", "enum": ["spec", "plan-author", "writer", "reviewer", "qa", "repair"]}, "model": {"type": "string", "enum": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]}, "reasoningEffort": {"type": "string", "enum": ["medium", "high"]}, "mission": {"type": "object", "additionalProperties": True, "required": ["immediateTask", "links", "checkout", "allowedMutations", "prohibitions", "skills", "evidence"], "properties": {"immediateTask": {"type": "string", "pattern": "^Your task now:"}, "displayTitle": {"type": "string", "minLength": 3, "maxLength": 60}, "links": {"type": "object"}, "checkout": {"type": "object", "additionalProperties": False, "required": ["repository", "branch", "headSha"], "properties": {"repository": {"type": "string"}, "branch": {"type": "string"}, "headSha": {"type": "string", "pattern": "^[0-9a-f]{40}$"}}}, "allowedMutations": {"type": "array", "items": {"type": "string"}}, "prohibitions": {"type": "array", "items": {"type": "string"}}, "skills": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "evidence": {"type": "array", "items": {"type": "string"}}}}, "capabilities": {"type": "array", "items": {"type": "string", "enum": ["runtime_environment"]}, "maxItems": 1, "uniqueItems": True}}},
    },
    {
        "name": "send_to_parent",
        "description": "Send a structured RESULT or NEEDS_INPUT to the owning Main. The target is derived from the signed capability; peers cannot be selected.",
        "inputSchema": {"type": "object", "additionalProperties": False, "required": ["result"], "properties": {"result": {"type": "object"}}},
    },
    {
        "name": "send_callback_fallback",
        "description": "Converge the immutable-Mission-authorized recovery marker after server-observed direct callback retry exhaustion. This Child-only operation has no arguments.",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
    {
        "name": "request_user_decision",
        "description": "Ask the human a bounded A/B/C-style question through the owning Main.",
        "inputSchema": {"type": "object", "additionalProperties": False, "required": ["question", "options"], "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 5}}},
    },
    {
        "name": "cancel_mission",
        "description": "Interrupt the exact Child mission bound to this capability before a replacement mission starts.",
        "inputSchema": {"type": "object", "additionalProperties": False, "required": ["targetId", "taskKey", "messageKey"], "properties": {"targetId": {"type": "string", "format": "uuid"}, "taskKey": {"type": "string"}, "messageKey": {"type": "string"}}},
    },
    {
        "name": "resume_mission",
        "description": "Resume the exact Child mission after its dependency or blocker is cleared.",
        "inputSchema": {"type": "object", "additionalProperties": False, "required": ["targetId", "taskKey", "messageKey", "context"], "properties": {"targetId": {"type": "string", "format": "uuid"}, "taskKey": {"type": "string"}, "messageKey": {"type": "string"}, "context": {"type": "object", "minProperties": 1}}},
    },
    {
        "name": "publish_navigation_links",
        "description": "Publish bounded human navigation links to the owning Main; links are informational, never workflow authority.",
        "inputSchema": {"type": "object", "additionalProperties": False, "required": ["links"], "properties": {"links": {"type": "object", "additionalProperties": {"type": "string"}}}},
    },
    {
        "name": "get_usage",
        "description": "Read live token usage, cache hit rate, model, reasoning effort, and official Standard API-equivalent cost for this Main or one deterministic Child. This is observability, never workflow authority or a subscription invoice.",
        "inputSchema": {"type": "object", "additionalProperties": False, "required": ["targetId", "taskKey"], "properties": {"targetId": {"type": "string", "format": "uuid"}, "taskKey": {"type": "string"}}},
    },
]


class McpServer:
    def __init__(self, service: MessagingService) -> None:
        self._service = service

    def handle(self, request: dict, *, capability_ref: str | None = None) -> dict | None:
        method = request.get("method")
        request_id = request.get("id")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return self._result(request_id, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "evex-agent-messaging", "version": "0.1.0"}})
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._call(request_id, request.get("params") or {}, capability_ref)
        return self._error(request_id, -32601, "method not found")

    def _call(self, request_id: object, params: dict, capability_ref: str | None) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if not isinstance(capability_ref, str) or not capability_ref.startswith("evx1_"):
                raise ValueError("transport capability is required")
            if name == "create_child":
                value = self._service.create_child(capability_ref, args["taskKey"], args["role"], args["mission"], args.get("capabilities"), model=args["model"], reasoning_effort=args["reasoningEffort"])
                value = {key: item for key, item in value.items() if key != "capabilityRef"}
            elif name == "send_to_parent":
                value = self._service.send_to_parent(capability_ref, args["result"])
            elif name == "send_callback_fallback":
                if args:
                    raise ValueError("send_callback_fallback accepts no arguments")
                value = self._service.send_callback_fallback(capability_ref)
            elif name == "request_user_decision":
                value = self._service.request_user_decision(capability_ref, args["question"], args["options"])
            elif name == "cancel_mission":
                value = self._service.cancel_mission(capability_ref, uuid.UUID(args["targetId"]), args["taskKey"], args["messageKey"])
            elif name == "resume_mission":
                value = self._service.resume_mission(capability_ref, uuid.UUID(args["targetId"]), args["taskKey"], args["messageKey"], args["context"])
            elif name == "publish_navigation_links":
                value = self._service.publish_navigation_links(capability_ref, args["links"])
            elif name == "get_usage":
                value = self._service.get_usage(
                    capability_ref, uuid.UUID(args["targetId"]), args["taskKey"]
                )
            else:
                return self._error(request_id, -32602, "unknown messaging tool")
        except (KeyError, ValueError, TypeError) as exc:
            return self._error(request_id, -32602, f"invalid tool arguments: {exc}")
        except Exception as exc:
            return self._error(request_id, -32000, str(exc))
        return self._result(request_id, {"content": [{"type": "text", "text": json.dumps(value, sort_keys=True, separators=(",", ":"))}], "structuredContent": value})

    @staticmethod
    def _result(request_id: object, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: object, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(server: McpServer, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    while True:
        headers = b""
        while b"\r\n\r\n" not in headers:
            chunk = stdin.readline()
            if not chunk:
                return
            headers += chunk
        try:
            length = int(next(line for line in headers.split(b"\r\n") if line.lower().startswith(b"content-length:" )).split(b":", 1)[1])
            payload = json.loads(stdin.read(length))
            response = server.handle(payload)
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"invalid MCP request: {exc}"}}
        if response is not None:
            encoded = json.dumps(response, separators=(",", ":")).encode()
            stdout.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode() + encoded)
            stdout.flush()


def make_http_server(
    server: McpServer, host: str = "0.0.0.0", port: int = 3101
) -> ThreadingHTTPServer:
    """Build the small stateless HTTP server used for in-cluster MCP transport."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok\n")
            elif self.path == "/readyz":
                try:
                    ready = bool(server._service.readiness())
                except Exception:
                    ready = False
                if ready:
                    self.send_response(200)
                    body = b"ok\n"
                else:
                    self.send_response(503)
                    body = b"unavailable\n"
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def do_POST(self):  # noqa: N802
            if self.path != "/mcp":
                self.send_error(404)
                return
            try:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                response = server.handle(
                    request,
                    capability_ref=bearer_capability(self.headers.get("Authorization")),
                )
                if response is None:
                    self.send_response(202)
                    self.end_headers()
                    return
                body = json.dumps(response, separators=(",", ":")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:  # malformed request is a JSON-RPC parse error, never a traceback
                body = json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"invalid MCP request: {exc}"}}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *_args):
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve_http(server: McpServer, host: str = "0.0.0.0", port: int = 3101) -> None:
    """Serve the small stateless HTTP MCP transport."""
    make_http_server(server, host, port).serve_forever()


def bearer_capability(value: str | None) -> str | None:
    """Return one transport-bound EVEX capability from an HTTP Bearer header."""
    if not isinstance(value, str) or not value.startswith("Bearer evx1_"):
        return None
    token = value.removeprefix("Bearer ")
    return token if " " not in token else None


def main() -> int:
    secret_value = os.environ.get("EVEX_MESSAGING_SECRET", "")
    if not secret_value.strip():
        raise SystemExit("EVEX_MESSAGING_SECRET is required")
    base_url = os.environ.get("OPENHANDS_URL", "")
    api_key = os.environ.get("OPENHANDS_API_KEY", "")
    public_url = os.environ.get("OPENHANDS_PUBLIC_URL", "")
    if not all(value.strip() for value in (base_url, api_key, public_url)):
        raise SystemExit("OPENHANDS_URL, OPENHANDS_API_KEY, and OPENHANDS_PUBLIC_URL are required")
    secret = secret_value.encode()
    fallback_token = os.environ.get("EVEX_MESSAGING_FALLBACK_GITHUB_TOKEN", "")
    fallback_login = os.environ.get("EVEX_MESSAGING_FALLBACK_GITHUB_APP_LOGIN", "")
    fallback_adapter = None
    if fallback_token or fallback_login:
        fallback_adapter = GitHubCallbackFallbackAdapter(fallback_token, fallback_login)
    server = McpServer(MessagingService(OpenHandsProvider(base_url, api_key, public_url), secret, callback_fallback_adapter=fallback_adapter))
    if os.environ.get("EVEX_MESSAGING_TRANSPORT", "stdio") == "http":
        serve_http(server, os.environ.get("EVEX_MESSAGING_HOST", "0.0.0.0"), int(os.environ.get("EVEX_MESSAGING_PORT", "3101")))
    else:
        serve(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
