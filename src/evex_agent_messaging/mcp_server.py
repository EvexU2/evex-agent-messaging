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


TOOLS = [
    {
        "name": "create_child",
        "description": "Create or recover one deterministic Child Conversation from a signed Main capability. Use only for a bounded mission; never use it to create peer or nested delivery owners.",
        "inputSchema": {"type": "object", "additionalProperties": False, "required": ["parentCapabilityRef", "taskKey", "role", "mission"], "properties": {"parentCapabilityRef": {"type": "string", "pattern": "^evx1_"}, "taskKey": {"type": "string"}, "role": {"type": "string", "enum": ["spec", "planner", "writer", "reviewer", "qa", "repair"]}, "mission": {"type": "object", "additionalProperties": True, "required": ["immediateTask", "links", "checkout", "allowedMutations", "prohibitions", "skills", "evidence"], "properties": {"immediateTask": {"type": "string", "pattern": "^Your task now:"}, "links": {"type": "object"}, "checkout": {"type": "object", "additionalProperties": False, "required": ["repository", "branch", "headSha"], "properties": {"repository": {"type": "string"}, "branch": {"type": "string"}, "headSha": {"type": "string", "pattern": "^[0-9a-f]{40}$"}}}, "allowedMutations": {"type": "array", "items": {"type": "string"}}, "prohibitions": {"type": "array", "items": {"type": "string"}}, "skills": {"type": "array", "items": {"type": "string"}}, "evidence": {"type": "array", "items": {"type": "string"}}}}, "capabilities": {"type": "array", "items": {"type": "string", "enum": ["runtime_environment"]}, "maxItems": 1, "uniqueItems": True}}},
    },
    {
        "name": "send_to_parent",
        "description": "Send a structured RESULT or NEEDS_INPUT to the owning Main. The target is derived from the signed capability; peers cannot be selected.",
        "inputSchema": {"type": "object", "required": ["capabilityRef", "result"], "properties": {"capabilityRef": {"type": "string", "pattern": "^evx1_"}, "result": {"type": "object"}}},
    },
    {
        "name": "request_user_decision",
        "description": "Ask the human a bounded A/B/C-style question through the owning Main.",
        "inputSchema": {"type": "object", "required": ["capabilityRef", "question", "options"], "properties": {"capabilityRef": {"type": "string", "pattern": "^evx1_"}, "question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 5}}},
    },
    {
        "name": "cancel_mission",
        "description": "Interrupt the exact Child mission bound to this capability before a replacement mission starts.",
        "inputSchema": {"type": "object", "required": ["capabilityRef", "targetId", "messageKey"], "properties": {"capabilityRef": {"type": "string", "pattern": "^evx1_"}, "targetId": {"type": "string", "format": "uuid"}, "messageKey": {"type": "string"}}},
    },
    {
        "name": "resume_mission",
        "description": "Resume the exact Child mission after its dependency or blocker is cleared.",
        "inputSchema": {"type": "object", "required": ["capabilityRef", "targetId", "messageKey"], "properties": {"capabilityRef": {"type": "string", "pattern": "^evx1_"}, "targetId": {"type": "string", "format": "uuid"}, "messageKey": {"type": "string"}}},
    },
    {
        "name": "publish_navigation_links",
        "description": "Publish bounded human navigation links to the owning Main; links are informational, never workflow authority.",
        "inputSchema": {"type": "object", "required": ["capabilityRef", "links"], "properties": {"capabilityRef": {"type": "string", "pattern": "^evx1_"}, "links": {"type": "object", "additionalProperties": {"type": "string"}}}},
    },
]


class McpServer:
    def __init__(self, service: MessagingService) -> None:
        self._service = service

    def handle(self, request: dict) -> dict | None:
        method = request.get("method")
        request_id = request.get("id")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return self._result(request_id, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "evex-agent-messaging", "version": "0.1.0"}})
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._call(request_id, request.get("params") or {})
        return self._error(request_id, -32601, "method not found")

    def _call(self, request_id: object, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "create_child":
                value = self._service.create_child(args["parentCapabilityRef"], args["taskKey"], args["role"], args["mission"], args.get("capabilities"))
            elif name == "send_to_parent":
                value = self._service.send_to_parent(args["capabilityRef"], args["result"])
            elif name == "request_user_decision":
                value = self._service.request_user_decision(args["capabilityRef"], args["question"], args["options"])
            elif name == "cancel_mission":
                value = self._service.cancel_mission(args["capabilityRef"], uuid.UUID(args["targetId"]), args["messageKey"])
            elif name == "resume_mission":
                value = self._service.resume_mission(args["capabilityRef"], uuid.UUID(args["targetId"]), args["messageKey"])
            elif name == "publish_navigation_links":
                value = self._service.publish_navigation_links(args["capabilityRef"], args["links"])
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


def serve_http(server: McpServer, host: str = "0.0.0.0", port: int = 3101) -> None:
    """Small stateless Streamable-HTTP-compatible JSON-RPC endpoint for in-cluster MCP use."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok\n")
            else:
                self.send_error(404)

        def do_POST(self):  # noqa: N802
            if self.path == "/completion-hook":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 2 or length > 4096:
                        raise ValueError("invalid completion-hook body size")
                    payload = json.loads(self.rfile.read(length))
                    capability_ref = payload.get("capabilityRef") if isinstance(payload, dict) else None
                    if not isinstance(capability_ref, str):
                        raise ValueError("capabilityRef is required")
                    value = server._service.terminal_wake(capability_ref)
                    body = json.dumps(value, separators=(",", ":")).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    self.send_error(503, "terminal wake unavailable")
                return
            if self.path != "/mcp":
                self.send_error(404)
                return
            try:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                response = server.handle(request)
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

    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main() -> int:
    secret = os.environ.get("EVEX_MESSAGING_SECRET", "").encode()
    if not secret:
        raise SystemExit("EVEX_MESSAGING_SECRET is required")
    base_url = os.environ.get("OPENHANDS_URL", "")
    api_key = os.environ.get("OPENHANDS_API_KEY", "")
    public_url = os.environ.get("OPENHANDS_PUBLIC_URL", "")
    if not base_url or not api_key or not public_url:
        raise SystemExit("OPENHANDS_URL, OPENHANDS_API_KEY, and OPENHANDS_PUBLIC_URL are required")
    completion_hook_url = os.environ.get(
        "EVEX_MESSAGING_COMPLETION_HOOK_URL",
        "http://evex-agent-messaging.evex-agents.svc.cluster.local:3101/completion-hook",
    )
    server = McpServer(
        MessagingService(
            OpenHandsProvider(
                base_url,
                api_key,
                public_url,
                completion_hook_url=completion_hook_url,
            ),
            secret,
        )
    )
    if os.environ.get("EVEX_MESSAGING_TRANSPORT", "stdio") == "http":
        serve_http(server, os.environ.get("EVEX_MESSAGING_HOST", "0.0.0.0"), int(os.environ.get("EVEX_MESSAGING_PORT", "3101")))
    else:
        serve(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
