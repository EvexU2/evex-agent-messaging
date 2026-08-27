"""Small OpenHands adapter for exact-target Discussion messages."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
from typing import Any, Callable
import urllib.error
import urllib.request
import uuid


_MAX_RESPONSE_BYTES = 65_536
_DURABLE_ROLES = {"parent-main", "child-main", "spec"}


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class OpenHandsProvider:
    base_url: str
    api_key: str
    timeout: float = 5.0
    transport: Callable[[str, str, dict | None], dict] | None = None

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        if self.transport is not None:
            value = self.transport(method, path, body)
            if not isinstance(value, dict):
                raise ProviderError("OpenHands returned an invalid response")
            return value
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=json.dumps(body, separators=(",", ":")).encode() if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json", "X-Session-API-Key": self.api_key},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise ProviderError("OpenHands messaging transport failed", status=exc.code) from exc
        except (OSError, http.client.IncompleteRead) as exc:
            raise ProviderError("OpenHands messaging transport failed") from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ProviderError("OpenHands response exceeds bounded byte budget")
        try:
            value = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise ProviderError("OpenHands returned an invalid response") from exc
        if not isinstance(value, dict):
            raise ProviderError("OpenHands returned an invalid response")
        return value

    def readiness(self) -> bool:
        if not self.base_url.strip() or not self.api_key.strip():
            return False
        try:
            value = self._request("GET", "/api/agent-profiles")
        except ProviderError:
            return False
        return isinstance(value.get("active_agent_profile_id"), str)

    @staticmethod
    def _identity(value: dict) -> tuple[uuid.UUID, dict[str, Any], str]:
        tags = value.get("tags")
        role = tags.get("evexdeliveryrole") if isinstance(tags, dict) else None
        try:
            conversation_id = uuid.UUID(str(value.get("id") or value.get("conversation_id")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ProviderError("OpenHands Discussion identity is invalid") from exc
        if (
            not isinstance(tags, dict)
            or tags.get("project") != "evex-u"
            or role not in _DURABLE_ROLES
        ):
            raise ProviderError("OpenHands Discussion identity is invalid")
        return conversation_id, tags, role

    def target_allowed(
        self,
        sender_id: uuid.UUID,
        target_id: uuid.UUID,
        role: str,
        owning_main_id: uuid.UUID,
    ) -> bool:
        target_identity, target_tags, target_role = self._identity(
            self._request("GET", f"/api/conversations/{target_id}")
        )
        if target_identity != target_id:
            return False
        if role in {"deputy", "spec"}:
            return target_id == owning_main_id and target_role in {"parent-main", "child-main"}
        if role != "main":
            return False
        sender_identity, sender_tags, sender_role = self._identity(
            self._request("GET", f"/api/conversations/{sender_id}")
        )
        if sender_identity != sender_id or sender_role != "parent-main":
            return False
        same_parent_issue = target_tags.get("evexparentissue") == sender_tags.get("evexissue")
        explicit_parent = target_tags.get("evexparent") == str(sender_id)
        return (target_role == "child-main" and same_parent_issue) or (
            target_role == "spec" and (same_parent_issue or explicit_parent)
        )

    def send_message(
        self,
        sender_id: uuid.UUID,
        target_id: uuid.UUID,
        message_key: str,
        text: str,
    ) -> dict[str, Any]:
        envelope = json.dumps(
            {"messageKey": message_key, "senderId": str(sender_id), "text": text},
            sort_keys=True,
            separators=(",", ":"),
        )
        self._request(
            "POST",
            f"/api/conversations/{target_id}/events",
            {"role": "user", "content": [{"type": "text", "text": envelope}], "run": True},
        )
        return {"accepted": True, "messageKey": message_key}
