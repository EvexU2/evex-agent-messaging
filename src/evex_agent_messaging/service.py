"""Provider-neutral messaging operations and capability enforcement."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
import uuid

from .capability import (
    CapabilityError,
    capability_token,
    deterministic_child_id,
    verify_capability,
)


class MessagingProvider(Protocol):
    def create_child(self, parent_id: uuid.UUID, child_id: uuid.UUID, role: str, task_key: str, mission: str) -> dict[str, Any]: ...
    def send_message(self, target_id: uuid.UUID, message_key: str, kind: str, text: str) -> dict[str, Any]: ...
    def cancel_mission(self, target_id: uuid.UUID, message_key: str, task_key: str) -> dict[str, Any]: ...


class MessagingService:
    """Small stateless facade; semantic replay is keyed in the message body, not persisted here."""

    def __init__(self, provider: MessagingProvider, secret: bytes, *, clock=None) -> None:
        if not secret:
            raise ValueError("messaging secret is required")
        self._provider = provider
        self._secret = secret
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def create_child(
        self,
        parent_capability: str,
        task_key: str,
        role: str,
        mission: str,
    ) -> dict[str, Any]:
        parent = verify_capability(
            parent_capability,
            self._secret,
            now=self._clock(),
            action="create_child",
            target_id=self._parent_target(parent_capability),
        )
        if parent.role not in {"main", "deputy"}:
            raise CapabilityError("only a Main may create a Child")
        if role not in {"spec", "planner", "writer", "reviewer", "qa", "repair", "waiter"}:
            raise CapabilityError("unsupported Child role")
        if not isinstance(mission, str) or not mission.strip() or len(mission) > 12000:
            raise CapabilityError("mission must be a non-empty bounded string")
        child_id = deterministic_child_id(parent.child_id, task_key)
        result = self._provider.create_child(parent.child_id, child_id, role, task_key, mission.strip())
        now = self._clock()
        token = capability_token(
            self._secret,
            owning_main_id=parent.owning_main_id,
            child_id=child_id,
            task_key=task_key,
            role=role,
            allowed_actions={"send_message", "cancel_mission"},
            issued_at=now,
            expires_at=now + timedelta(days=7),
        )
        return {**result, "childId": str(child_id), "capability": token}

    def send_message(
        self, token: str, target_id: uuid.UUID, message_key: str, kind: str, text: str
    ) -> dict[str, Any]:
        capability = verify_capability(token, self._secret, now=self._clock(), action="send_message", target_id=target_id)
        if kind not in {"RESULT", "NEEDS_INPUT", "CANCEL_MISSION", "RECOVERY_WAKE"}:
            raise CapabilityError("unsupported message kind")
        if not isinstance(message_key, str) or not message_key or len(message_key) > 200:
            raise CapabilityError("messageKey must be bounded and non-empty")
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise CapabilityError("message text must be bounded and non-empty")
        envelope = {
            "messageKey": message_key,
            "owningMainId": str(capability.owning_main_id),
            "childId": str(capability.child_id),
            "taskKey": capability.task_key,
            "kind": kind,
            "text": text,
        }
        return self._provider.send_message(target_id, message_key, kind, _compact(envelope))

    def cancel_mission(self, token: str, target_id: uuid.UUID, message_key: str) -> dict[str, Any]:
        capability = verify_capability(token, self._secret, now=self._clock(), action="cancel_mission", target_id=target_id)
        return self._provider.cancel_mission(target_id, message_key, capability.task_key)

    @staticmethod
    def _parent_target(token: str) -> uuid.UUID:
        # The parent token is self-contained; verify_capability needs the bound target.
        # A Main capability binds child_id to its own deterministic Main identity.
        encoded = token.split(".", 1)[0]
        import base64, json
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        return uuid.UUID(str(payload["childId"]))


def _compact(value: dict[str, Any]) -> str:
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
