"""Minimal MCP stdio server with provider-neutral agent messaging tools."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import sys
import uuid

from .provider import OpenHandsProvider
from .service import MessagingService


TOOLS = [
    {
        "name": "create_child_conversation",
        "description": "Create or recover one deterministic Child Conversation from a signed Main capability. Use only for a bounded mission; never use it to create peer or nested delivery owners.",
        "inputSchema": {"type": "object", "required": ["parentCapability", "taskKey", "role", "mission"], "properties": {"parentCapability": {"type": "string"}, "taskKey": {"type": "string"}, "role": {"type": "string", "enum": ["spec", "planner", "writer", "reviewer", "qa", "repair", "waiter"]}, "mission": {"type": "string"}}},
    },
    {
        "name": "send_agent_message",
        "description": "Send one bounded RESULT, NEEDS_INPUT, control, or recovery message through the authorized Main/Child tree. Include a stable messageKey so the owning Main can deduplicate replays.",
        "inputSchema": {"type": "object", "required": ["capability", "targetId", "messageKey", "kind", "text"], "properties": {"capability": {"type": "string"}, "targetId": {"type": "string", "format": "uuid"}, "messageKey": {"type": "string"}, "kind": {"type": "string", "enum": ["RESULT", "NEEDS_INPUT", "CANCEL_MISSION", "RECOVERY_WAKE"]}, "text": {"type": "string"}}},
    },
    {
        "name": "cancel_agent_mission",
        "description": "Interrupt the exact Child mission bound to this capability before a replacement mission starts.",
        "inputSchema": {"type": "object", "required": ["capability", "targetId", "messageKey"], "properties": {"capability": {"type": "string"}, "targetId": {"type": "string", "format": "uuid"}, "messageKey": {"type": "string"}}},
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
            if name == "create_child_conversation":
                value = self._service.create_child(args["parentCapability"], args["taskKey"], args["role"], args["mission"])
            elif name == "send_agent_message":
                value = self._service.send_message(args["capability"], uuid.UUID(args["targetId"]), args["messageKey"], args["kind"], args["text"])
            elif name == "cancel_agent_mission":
                value = self._service.cancel_mission(args["capability"], uuid.UUID(args["targetId"]), args["messageKey"])
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


def main() -> int:
    secret = os.environ.get("EVEX_MESSAGING_SECRET", "").encode()
    if not secret:
        raise SystemExit("EVEX_MESSAGING_SECRET is required")
    base_url = os.environ.get("OPENHANDS_URL", "")
    api_key = os.environ.get("OPENHANDS_API_KEY", "")
    public_url = os.environ.get("OPENHANDS_PUBLIC_URL", "")
    if not base_url or not api_key or not public_url:
        raise SystemExit("OPENHANDS_URL, OPENHANDS_API_KEY, and OPENHANDS_PUBLIC_URL are required")
    serve(McpServer(MessagingService(OpenHandsProvider(base_url, api_key, public_url), secret)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
