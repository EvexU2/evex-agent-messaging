"""Minimal MCP server exposing one authenticated message operation."""

from __future__ import annotations

import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .provider import OpenHandsProvider
from .capability import REFERENCE_PREFIX
from .service import MessagingService


TOOLS = [{
    "name": "create_spec_chat",
    "description": "Create or reuse the one interactive Spec Chat owned by this Parent Main.",
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["checkout"],
        "properties": {
            "checkout": {
                "type": "object",
                "additionalProperties": False,
                "required": ["repository", "branch", "headSha"],
                "properties": {
                    "repository": {"type": "string", "minLength": 3, "maxLength": 200},
                    "branch": {"type": "string", "minLength": 1, "maxLength": 160},
                    "headSha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                },
            },
        },
    },
}, {
    "name": "send_message",
    "description": "Send one bounded structured message to one exact known durable Discussion target.",
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["targetId", "messageKey", "message"],
        "properties": {
            "targetId": {"type": "string", "format": "uuid"},
            "messageKey": {"type": "string", "minLength": 1, "maxLength": 200},
            "message": {
                "type": "object",
                "additionalProperties": False,
                "required": ["humanSummary", "aiEvidence"],
                "properties": {
                    "humanSummary": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "aiEvidence": {"type": "object"},
                },
            },
        },
    },
}]


class McpServer:
    def __init__(self, service: MessagingService) -> None:
        self._service = service

    def handle(self, request: dict, *, capability_ref: str | None = None) -> dict | None:
        method, request_id = request.get("method"), request.get("id")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return self._result(request_id, {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "evex-agent-messaging", "version": "0.3.0"},
            })
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOLS})
        if method != "tools/call":
            return self._error(request_id, -32601, "method not found")
        params = request.get("params") or {}
        arguments = params.get("arguments") or {}
        try:
            name = params.get("name")
            if name not in {"create_spec_chat", "send_message"}:
                return self._error(request_id, -32602, "unknown messaging tool")
            if not isinstance(capability_ref, str) or not capability_ref.startswith(REFERENCE_PREFIX):
                raise ValueError("transport capability is required")
            if name == "create_spec_chat":
                value = self._service.create_spec_chat(
                    capability_ref,
                    arguments["checkout"],
                )
            else:
                if "message" not in arguments and "text" in arguments:
                    return self._error(
                        request_id,
                        -32602,
                        "send_message requires the structured 'message' argument; 'text' is not accepted",
                    )
                value = self._service.send_message(
                    capability_ref,
                    uuid.UUID(arguments["targetId"]),
                    arguments["messageKey"],
                    arguments["message"],
                )
        except (KeyError, TypeError, ValueError) as exc:
            return self._error(request_id, -32602, "invalid messaging request")
        except Exception as exc:
            return self._error(request_id, -32000, "messaging operation failed")
        return self._result(request_id, {
            "content": [{"type": "text", "text": json.dumps(value, sort_keys=True, separators=(",", ":"))}],
            "structuredContent": value,
        })

    @staticmethod
    def _result(request_id: object, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: object, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(server: McpServer, stdin=None, stdout=None) -> None:
    stdin, stdout = stdin or sys.stdin.buffer, stdout or sys.stdout.buffer
    while True:
        headers = b""
        while b"\r\n\r\n" not in headers:
            chunk = stdin.readline()
            if not chunk:
                return
            headers += chunk
        try:
            length = int(next(
                line for line in headers.split(b"\r\n") if line.lower().startswith(b"content-length:")
            ).split(b":", 1)[1])
            response = server.handle(json.loads(stdin.read(length)))
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"invalid MCP request: {exc}"}}
        if response is not None:
            encoded = json.dumps(response, separators=(",", ":")).encode()
            stdout.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode() + encoded)
            stdout.flush()


def make_http_server(server: McpServer, host: str = "0.0.0.0", port: int = 3101) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                status, body = 200, b"ok\n"
            elif self.path == "/readyz":
                ready = server._service.readiness()
                status, body = (200, b"ok\n") if ready else (503, b"unavailable\n")
            else:
                self.send_error(404)
                return
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            if self.path != "/mcp":
                self.send_error(404)
                return
            try:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                response = server.handle(request, capability_ref=bearer_capability(self.headers.get("Authorization")))
                body = b"" if response is None else json.dumps(response, separators=(",", ":")).encode()
                self.send_response(202 if response is None else 200)
            except Exception as exc:
                body = json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"invalid MCP request: {exc}"}}).encode()
                self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, *_args):
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve_http(server: McpServer, host: str = "0.0.0.0", port: int = 3101) -> None:
    make_http_server(server, host, port).serve_forever()


def bearer_capability(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.startswith(f"Bearer {REFERENCE_PREFIX}"):
        return None
    token = value.removeprefix("Bearer ")
    return token if " " not in token else None


def main() -> int:
    secret = os.environ.get("EVEX_MESSAGING_SECRET", "")
    base_url = os.environ.get("OPENHANDS_URL", "")
    api_key = os.environ.get("OPENHANDS_API_KEY", "")
    if not all(value.strip() for value in (secret, base_url, api_key)):
        raise SystemExit("EVEX_MESSAGING_SECRET, OPENHANDS_URL, and OPENHANDS_API_KEY are required")
    public_url = os.environ.get("OPENHANDS_PUBLIC_URL", "")
    if not public_url.strip():
        raise SystemExit("OPENHANDS_PUBLIC_URL is required")
    server = McpServer(MessagingService(
        OpenHandsProvider(base_url, api_key, public_url=public_url),
        secret.encode(),
    ))
    if os.environ.get("EVEX_MESSAGING_TRANSPORT", "stdio") == "http":
        serve_http(server, os.environ.get("EVEX_MESSAGING_HOST", "0.0.0.0"), int(os.environ.get("EVEX_MESSAGING_PORT", "3101")))
    else:
        serve(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
