"""Minimal MCP server exposing direct Conversation creation and messaging."""

from __future__ import annotations

import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .provider import OpenHandsProvider, ProviderError
from .capability import CapabilityError, PROJECT_REFERENCE_PREFIX, REFERENCE_PREFIX
from .delivery import DeliveryContractError, MAX_DELIVERY_BYTES
from .service import MessagingService, SPECIALIST_DESCRIPTION_MAX_LENGTH


_CAPABILITY_PREFIXES = (REFERENCE_PREFIX, PROJECT_REFERENCE_PREFIX)
_MAX_PROVISION_BYTES = 1024


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


TOOLS = [{
    "name": "create_spec_chat",
    "description": "Create or reuse the one interactive Spec Chat owned by this Issue Conversation.",
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    },
}, {
    "name": "start_specialist",
    "description": (
        "Create or reuse one bounded Specialist Conversation owned by this exact sender. "
        "After creation, communicate only with send_message."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["missionKey", "prompt", "agentType", "description"],
        "properties": {
            "missionKey": {"type": "string", "minLength": 1, "maxLength": 128},
            "prompt": {"type": "string", "minLength": 1, "maxLength": 32768},
            "agentType": {
                "type": "string",
                "enum": [
                    "plan", "plan-review", "project-review", "qa",
                    "code-review", "spec-review", "writer",
                ],
            },
            "description": {
                "type": "string",
                "minLength": 1,
                "maxLength": SPECIALIST_DESCRIPTION_MAX_LENGTH,
                "description": (
                    f"Use a short chat-title outcome label of at most "
                    f"{SPECIALIST_DESCRIPTION_MAX_LENGTH} characters; "
                    "put Mission detail in prompt."
                ),
            },
            "reasoning": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": (
                    "Use low only for a fully proven bounded Plan or Plan Review; "
                    "when omitted, Spec Review uses high and other Specialists use medium."
                ),
            },
            "skills": {
                "type": "array",
                "maxItems": 32,
                "items": {"type": "string", "minLength": 1, "maxLength": 64},
                "default": [],
            },
        },
    },
}, {
    "name": "send_message",
    "description": "Send one bounded structured message to one exact known allowed Conversation target.",
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["targetId", "messageKey", "message"],
        "properties": {
            "targetId": {"type": "string", "format": "uuid"},
            "messageKey": {"type": "string", "minLength": 1, "maxLength": 200},
            "message": {
                "type": "object",
                "description": (
                    "Pass the structured message as a JSON object, never a JSON-encoded string. "
                    "This same operation carries questions, findings, follow-ups, cancellation, "
                    "and terminal Specialist results."
                ),
                "additionalProperties": False,
                "required": ["humanSummary", "aiEvidence"],
                "properties": {
                    "humanSummary": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "aiEvidence": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["outcome", "evidence", "findings", "nextBoundary"],
                        "properties": {
                            "outcome": {"type": "string", "minLength": 1, "maxLength": 2000},
                            "revision": {"type": "string", "minLength": 1, "maxLength": 2000},
                            "evidence": {
                                "type": "array",
                                "maxItems": 100,
                                "description": (
                                    "Use compact stable references and short observations; "
                                    "do not copy full artifact bodies. Every item must fit "
                                    "within 2000 UTF-8 bytes."
                                ),
                                "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                            },
                            "findings": {
                                "type": "array",
                                "maxItems": 100,
                                "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                            },
                            "nextBoundary": {"type": "string", "minLength": 1, "maxLength": 2000},
                            "artifact": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 64000,
                                "description": (
                                    "Optional complete result artifact when the receiver needs exact "
                                    "content, for example a reviewed Plan."
                                ),
                            },
                        },
                    },
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
                "serverInfo": {"name": "evex-agent-messaging", "version": "0.5.0"},
            })
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOLS})
        if method != "tools/call":
            return self._error(request_id, -32601, "method not found")
        params = request.get("params") or {}
        arguments = params.get("arguments", {})
        try:
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments must be an object")
            name = params.get("name")
            if name not in {"create_spec_chat", "start_specialist", "send_message"}:
                return self._error(request_id, -32602, "unknown messaging tool")
            if not isinstance(capability_ref, str) or not capability_ref.startswith(_CAPABILITY_PREFIXES):
                raise ValueError("transport capability is required")
            if name == "create_spec_chat":
                if arguments:
                    return self._error(
                        request_id,
                        -32602,
                        "create_spec_chat accepts no arguments",
                    )
                value = self._service.create_spec_chat(capability_ref)
            elif name == "start_specialist":
                value = self._service.start_specialist(
                    capability_ref,
                    mission_key=arguments["missionKey"],
                    prompt=arguments["prompt"],
                    agent_type=arguments["agentType"],
                    description=arguments["description"],
                    reasoning=arguments.get("reasoning"),
                    skills=arguments.get("skills", []),
                )
            else:
                if "message" not in arguments and "text" in arguments:
                    return self._error(
                        request_id,
                        -32602,
                        "send_message requires the structured 'message' argument; 'text' is not accepted",
                    )
                if isinstance(arguments.get("message"), str):
                    return self._error(
                        request_id,
                        -32602,
                        "send_message 'message' must be an object, not a JSON-encoded string",
                    )
                value = self._service.send_message(
                    capability_ref,
                    uuid.UUID(arguments["targetId"]),
                    arguments["messageKey"],
                    arguments["message"],
                )
        except CapabilityError as exc:
            return self._error(request_id, -32602, str(exc))
        except (KeyError, TypeError, ValueError) as exc:
            return self._error(request_id, -32602, "invalid messaging request")
        except ProviderError as exc:
            message = str(exc)
            if exc.status is not None:
                message = f"{message} (HTTP {exc.status})"
            return self._error(request_id, -32000, message)
        except Exception:
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
            if self.path == "/internal/agent-deliveries":
                self._deliver_main()
                return
            if self.path == "/internal/project-capability":
                self._provision_project_capability()
                return
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

        def _provision_project_capability(self):
            self.close_connection = True
            status, result = 403, {"error": "Project capability request denied"}
            try:
                authorized = server._service.provisioning_allowed(
                    _bearer_credential(self.headers.get("Authorization"))
                )
                if authorized:
                    lengths = self.headers.get_all("Content-Length", [])
                    if (
                        len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                        or not 0 < int(lengths[0]) <= _MAX_PROVISION_BYTES
                        or self.headers.get("Transfer-Encoding") is not None
                    ):
                        raise ValueError("invalid content length")
                    raw = self.rfile.read(int(lengths[0]))
                    if len(raw) != int(lengths[0]):
                        raise ValueError("incomplete request")
                    request = json.loads(raw, object_pairs_hook=_unique_object)
                    result = server._service.provision_project_capability(request)
                    status = 200
            except (TypeError, ValueError):
                status, result = 400, {"error": "invalid Project capability request"}
            except Exception:
                status, result = 503, {"error": "Project capability operation failed"}
            body = json.dumps(result, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _deliver_main(self):
            self.close_connection = True
            credential = _bearer_credential(self.headers.get("Authorization"))
            if not server._service.delivery_allowed(credential):
                self._write_delivery_response(403, {"reason": "delivery_forbidden"})
                return
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if (
                    len(lengths) != 1
                    or not lengths[0].isascii()
                    or not lengths[0].isdigit()
                    or not 0 < int(lengths[0]) <= MAX_DELIVERY_BYTES
                    or self.headers.get("Transfer-Encoding") is not None
                    or self.headers.get("Content-Type") != "application/json"
                ):
                    raise DeliveryContractError("invalid delivery request")
                raw = self.rfile.read(int(lengths[0]))
                if len(raw) != int(lengths[0]):
                    raise DeliveryContractError("invalid delivery request")
                request = json.loads(raw, object_pairs_hook=_unique_object)
                result = server._service.deliver_main(credential, request)
                status = 200
            except (DeliveryContractError, json.JSONDecodeError, TypeError, ValueError):
                status, result = 400, {"reason": "invalid_delivery_request"}
            except PermissionError:
                status, result = 403, {"reason": "delivery_forbidden"}
            except ProviderError as exc:
                reason = exc.reason or "runtime_unavailable"
                status = 503 if reason == "runtime_unavailable" else 409
                result = {"reason": reason}
            except Exception:
                status, result = 503, {"reason": "runtime_unavailable"}
            self._write_delivery_response(status, result)

        def _write_delivery_response(self, status: int, result: dict):
            body = json.dumps(result, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve_http(server: McpServer, host: str = "0.0.0.0", port: int = 3101) -> None:
    make_http_server(server, host, port).serve_forever()


def bearer_capability(value: str | None) -> str | None:
    token = _bearer_credential(value)
    return token if token is not None and token.startswith(_CAPABILITY_PREFIXES) else None


def _bearer_credential(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.startswith("Bearer "):
        return None
    token = value.removeprefix("Bearer ")
    return token if token and not any(character.isspace() for character in token) else None


def main() -> int:
    secret = os.environ.get("EVEX_MESSAGING_SECRET", "")
    base_url = os.environ.get("OPENHANDS_URL", "")
    api_key = os.environ.get("OPENHANDS_API_KEY", "")
    admission_key = os.environ.get("EVEX_DELIVERY_ADMISSION_KEY", "").strip()
    delivery_secret = os.environ.get("EVEX_GATEWAY_DELIVERY_SECRET", "").strip()
    if not all(value.strip() for value in (
        secret, base_url, api_key, admission_key, delivery_secret,
    )):
        raise SystemExit(
            "EVEX_MESSAGING_SECRET, EVEX_GATEWAY_DELIVERY_SECRET, "
            "EVEX_DELIVERY_ADMISSION_KEY, OPENHANDS_URL, and OPENHANDS_API_KEY are required"
        )
    if len(admission_key) < 32:
        raise SystemExit("EVEX_DELIVERY_ADMISSION_KEY must be at least 32 characters")
    if len(delivery_secret) < 32:
        raise SystemExit("EVEX_GATEWAY_DELIVERY_SECRET must be at least 32 characters")
    public_url = os.environ.get("OPENHANDS_PUBLIC_URL", "")
    if not public_url.strip():
        raise SystemExit("OPENHANDS_PUBLIC_URL is required")
    server = McpServer(MessagingService(
        OpenHandsProvider(
            base_url,
            api_key,
            public_url=public_url,
            admission_key=admission_key.encode(),
            messaging_secret=secret.encode(),
        ),
        secret.encode(),
        delivery_secret=delivery_secret.encode(),
    ))
    if os.environ.get("EVEX_MESSAGING_TRANSPORT", "stdio") == "http":
        serve_http(server, os.environ.get("EVEX_MESSAGING_HOST", "0.0.0.0"), int(os.environ.get("EVEX_MESSAGING_PORT", "3101")))
    else:
        serve(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
