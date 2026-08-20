"""Provider adapter. Only this module knows OpenHands REST details."""

from __future__ import annotations

from dataclasses import dataclass
import json
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

    def create_child(self, parent_id: uuid.UUID, child_id: uuid.UUID, role: str, task_key: str, mission: str) -> dict:
        created = True
        try:
            self._request("GET", f"/api/conversations/{child_id}")
            return {"conversationUrl": f"{self.public_url.rstrip('/')}/conversations/{child_id}", "provider": "openhands", "created": False}
        except ProviderError as exc:
            if exc.status != 404:
                raise
        try:
            profiles = self._request("GET", "/api/agent-profiles")
            profile_id = profiles.get("active_agent_profile_id")
            if not isinstance(profile_id, str) or not profile_id:
                raise ProviderError("OpenHands has no active Agent Profile")
            result = self._request(
                "POST",
                "/api/conversations",
                {
                    "conversation_id": str(child_id),
                    "agent_profile_id": profile_id,
                    "workspace": {"working_dir": f"/home/openhands/workspace/delivery/child-{child_id}"},
                    "tags": {"project": "evex-u", "evexrole": "role-child", "evextask": task_key, "evexparent": str(parent_id), "evexchildrole": role},
                    "autotitle": False,
                    "max_iterations": 300,
                },
            )
        except ProviderError as exc:
            if exc.status != 409:
                raise
            result = {"created": False}
            created = False
        if created:
            self._request("POST", f"/api/conversations/{child_id}/events", {"role": "user", "content": [{"type": "text", "text": f"MISSION\n{mission}"}], "run": True})
        return {"conversationUrl": f"{self.public_url.rstrip('/')}/conversations/{child_id}", "provider": "openhands", "created": created}

    def send_message(self, target_id: uuid.UUID, message_key: str, kind: str, text: str) -> dict:
        self._request("POST", f"/api/conversations/{target_id}/events", {"role": "user", "content": [{"type": "text", "text": f"{kind}\n{text}"}], "run": True})
        return {"accepted": True, "messageKey": message_key}

    def cancel_mission(self, target_id: uuid.UUID, message_key: str, task_key: str) -> dict:
        self._request("POST", f"/api/conversations/{target_id}/interrupt", {})
        return {"accepted": True, "messageKey": message_key, "taskKey": task_key}

    def resume_mission(self, target_id: uuid.UUID, message_key: str, task_key: str) -> dict:
        self._request("POST", f"/api/conversations/{target_id}/events", {"role": "user", "content": [{"type": "text", "text": "RESUME_MISSION\n" + message_key}], "run": True})
        return {"accepted": True, "messageKey": message_key, "taskKey": task_key}
