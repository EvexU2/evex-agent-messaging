"""Provider adapter. Only this module knows OpenHands REST details."""

from __future__ import annotations

from dataclasses import dataclass
import json
import shlex
import time
import urllib.error
import urllib.request
import uuid


class ProviderError(RuntimeError):
    """Provider failure translated away from raw credentials/HTTP details."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class OpenHandsProvider:
    base_url: str
    api_key: str
    public_url: str
    timeout: float = 5.0
    sleeper: object = time.sleep
    completion_hook_url: str = "http://evex-agent-messaging.evex-agents.svc.cluster.local:3101/completion-hook"

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=json.dumps(body, separators=(",", ":")).encode() if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json", "X-Session-API-Key": self.api_key},
        )
        created = True
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                value = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise ProviderError("OpenHands messaging transport failed", status=exc.code) from exc
        except OSError as exc:
            raise ProviderError("OpenHands messaging transport failed") from exc
        if not isinstance(value, dict):
            raise ProviderError("OpenHands returned an invalid response")
        return value

    def create_child(
        self,
        parent_id: uuid.UUID,
        child_id: uuid.UUID,
        role: str,
        task_key: str,
        mission: str,
        capability_ref: str,
        capabilities: frozenset[str],
    ) -> dict:
        created = True
        try:
            existing = self._request("GET", f"/api/conversations/{child_id}")
            if not existing.get("last_user_message_id"):
                self._request("POST", f"/api/conversations/{child_id}/events", {"role": "user", "content": [{"type": "text", "text": f"MISSION\n{mission}"}], "run": True})
            return {"conversationUrl": f"{self.public_url.rstrip('/')}/conversations/{child_id}", "provider": "openhands", "created": False}
        except ProviderError as exc:
            if exc.status != 404:
                raise
        try:
            profiles = self._request("GET", "/api/agent-profiles")
            profile_id = profiles.get("active_agent_profile_id")
            if not isinstance(profile_id, str) or not profile_id:
                raise ProviderError("OpenHands has no active Agent Profile")
            settings = self._request("GET", "/api/settings")
            mcp_config = self._mission_mcp_config(settings, capabilities)
            payload = {
                "conversation_id": str(child_id),
                "agent_profile_id": profile_id,
                "workspace": {"working_dir": f"/home/openhands/workspace/delivery/child-{child_id}"},
                "tags": {"project": "evex-u", "evexrole": "role-child", "evextask": task_key, "evexparent": str(parent_id), "evexchildrole": role},
                "autotitle": False,
                "max_iterations": 300,
                "hook_config": {
                    "stop": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": self._completion_hook_command(capability_ref),
                                    "timeout": 50,
                                    "async": True,
                                }
                            ],
                        }
                    ]
                },
            }
            if mcp_config:
                payload["mcp_config"] = mcp_config
            result = self._request(
                "POST",
                "/api/conversations",
                payload,
            )
        except ProviderError as exc:
            if exc.status != 409:
                raise
            result = {"created": False}
            created = False
        if created:
            self._request("POST", f"/api/conversations/{child_id}/events", {"role": "user", "content": [{"type": "text", "text": f"MISSION\n{mission}"}], "run": True})
        return {"conversationUrl": f"{self.public_url.rstrip('/')}/conversations/{child_id}", "provider": "openhands", "created": created}

    def _completion_hook_command(self, capability_ref: str) -> str:
        body = json.dumps({"capabilityRef": capability_ref}, separators=(",", ":"))
        return (
            "curl --fail --silent --show-error --retry 2 --retry-delay 2 "
            "--retry-all-errors --max-time 45 --header 'Content-Type: application/json' "
            f"--data {shlex.quote(body)} {shlex.quote(self.completion_hook_url)}"
        )

    @staticmethod
    def _mission_mcp_config(settings: dict, capabilities: frozenset[str]) -> dict:
        agent_settings = settings.get("agent_settings") if isinstance(settings, dict) else None
        value = agent_settings.get("mcp_config") if isinstance(agent_settings, dict) else None
        if not isinstance(value, dict):
            return {}
        requested = {"evex_agent_messaging"}
        if "runtime_environment" in capabilities:
            requested.add("evex_runtime")
        servers = value.get("mcpServers")
        if isinstance(servers, dict):
            selected = {
                name: server
                for name, server in servers.items()
                if name in requested and isinstance(server, dict)
            }
            return {"mcpServers": selected} if selected else {}
        return {
            name: server
            for name, server in value.items()
            if name in requested and isinstance(server, dict)
        }

    def wait_until_terminal(self, target_id: uuid.UUID) -> str:
        path = f"/api/conversations/{target_id}"
        for _ in range(300):
            status = self._request("GET", path).get("execution_status")
            if status in {"finished", "error", "stuck"}:
                return status
            self.sleeper(0.1)
        raise ProviderError("OpenHands Child did not reach terminal state")

    def send_message(self, target_id: uuid.UUID, message_key: str, kind: str, text: str) -> dict:
        path = f"/api/conversations/{target_id}"
        for _ in range(300):
            status = self._request("GET", path).get("execution_status")
            if status in {"idle", "paused", "finished", "error", "stuck"}:
                break
            self.sleeper(0.1)
        else:
            raise ProviderError("OpenHands Main did not become callback-wakeable")
        self._request("POST", f"/api/conversations/{target_id}/events", {"role": "user", "content": [{"type": "text", "text": f"{kind}\n{text}"}], "run": True})
        return {"accepted": True, "messageKey": message_key}

    def cancel_mission(
        self,
        target_id: uuid.UUID,
        message_key: str,
        task_key: str,
        owning_main_id: uuid.UUID,
    ) -> dict:
        path = f"/api/conversations/{target_id}"
        current = self._request("GET", path)
        status = current.get("execution_status")
        if status in {"finished", "error", "stuck"}:
            return {"accepted": True, "messageKey": message_key, "taskKey": task_key, "terminal": True}
        if status == "running":
            self._request("POST", f"{path}/interrupt", {})
        for _ in range(20):
            current = self._request("GET", path)
            status = current.get("execution_status")
            if status in {"finished", "error", "stuck"}:
                return {"accepted": True, "messageKey": message_key, "taskKey": task_key, "terminal": True}
            if status in {"paused", "idle"}:
                envelope = {
                    "childId": str(target_id),
                    "messageKey": message_key,
                    "owningMainId": str(owning_main_id),
                    "targetId": str(target_id),
                    "taskKey": task_key,
                }
                self._request(
                    "POST",
                    f"{path}/events",
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "CANCEL_MISSION\n"
                                + json.dumps(envelope, sort_keys=True, separators=(",", ":")),
                            }
                        ],
                        "run": True,
                    },
                )
                self.wait_until_terminal(target_id)
                return {"accepted": True, "messageKey": message_key, "taskKey": task_key, "terminal": True}
            self.sleeper(0.1)
        raise ProviderError("OpenHands Child did not become cancellation-wakeable")

    def resume_mission(self, target_id: uuid.UUID, message_key: str, task_key: str) -> dict:
        self._request("POST", f"/api/conversations/{target_id}/events", {"role": "user", "content": [{"type": "text", "text": "RESUME_MISSION\n" + message_key}], "run": True})
        return {"accepted": True, "messageKey": message_key, "taskKey": task_key}
