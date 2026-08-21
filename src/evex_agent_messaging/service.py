"""Provider-neutral messaging operations and capability enforcement."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
import json
import uuid

from .capability import (
    CapabilityError,
    capability_token,
    deterministic_child_id,
    inspect_capability,
    verify_capability,
)


class MessagingProvider(Protocol):
    def create_child(self, parent_id: uuid.UUID, child_id: uuid.UUID, role: str, task_key: str, mission: dict[str, Any], capability_ref: str, capabilities: frozenset[str]) -> dict[str, Any]: ...
    def send_message(self, target_id: uuid.UUID, message_key: str, kind: str, text: str) -> dict[str, Any]: ...
    def cancel_mission(self, target_id: uuid.UUID, message_key: str, task_key: str, owning_main_id: uuid.UUID) -> dict[str, Any]: ...
    def resume_mission(self, target_id: uuid.UUID, message_key: str, task_key: str) -> dict[str, Any]: ...
    def wait_until_terminal(self, target_id: uuid.UUID) -> str: ...
    def terminal_response(self, target_id: uuid.UUID) -> str: ...


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
        mission: dict[str, Any],
        capabilities: list[str] | None = None,
    ) -> dict[str, Any]:
        parent = verify_capability(
            parent_capability,
            self._secret,
            now=self._clock(),
            action="create_child",
            target_id=self._capability_target(parent_capability),
        )
        if parent.role not in {"main", "deputy"}:
            raise CapabilityError("only a Main may create a Child")
        if role not in {"spec", "planner", "writer", "reviewer", "qa", "repair"}:
            raise CapabilityError("unsupported Child role")
        mission_payload = self._validated_mission(mission)
        requested_capabilities = capabilities or []
        if (
            not isinstance(requested_capabilities, list)
            or len(requested_capabilities) > 1
            or any(value != "runtime_environment" for value in requested_capabilities)
        ):
            raise CapabilityError("unsupported Child capability")
        if requested_capabilities and role not in {"qa", "repair"}:
            raise CapabilityError("runtime environment is limited to QA or repair Children")
        child_id = deterministic_child_id(parent.child_id, task_key)
        now = self._clock()
        token = capability_token(
            self._secret,
            owning_main_id=parent.owning_main_id,
            child_id=child_id,
            task_key=task_key,
            role=role,
            allowed_actions={"send_message", "cancel_mission", "resume_mission"},
            issued_at=now,
            expires_at=now + timedelta(hours=24),
        )
        bound_mission = {
            **mission_payload,
            "owningMainId": str(parent.owning_main_id),
            "childId": str(child_id),
            "taskKey": task_key,
            "role": role,
            "callback": {"tool": "send_to_parent"},
            "capabilities": requested_capabilities,
        }
        result = self._provider.create_child(
            parent.child_id,
            child_id,
            role,
            task_key,
            bound_mission,
            token,
            frozenset(requested_capabilities),
        )
        return {**result, "childId": str(child_id), "capabilityRef": token}

    @staticmethod
    def _validated_mission(mission: object) -> dict[str, Any]:
        required = {
            "immediateTask",
            "links",
            "checkout",
            "allowedMutations",
            "prohibitions",
            "skills",
            "evidence",
        }
        reserved = {"owningMainId", "childId", "taskKey", "role", "callback", "capabilities"}
        if not isinstance(mission, dict) or not required.issubset(mission) or reserved.intersection(mission):
            raise CapabilityError("mission is incomplete or contains provider-owned authority")
        immediate_task = mission.get("immediateTask")
        checkout = mission.get("checkout")
        if not isinstance(immediate_task, str) or not immediate_task.startswith("Your task now:"):
            raise CapabilityError("mission immediateTask must begin with 'Your task now:'")
        if not isinstance(checkout, dict) or set(checkout) != {"repository", "branch", "headSha"}:
            raise CapabilityError("mission checkout must contain repository, branch, and headSha")
        repository, branch, head_sha = (checkout.get(key) for key in ("repository", "branch", "headSha"))
        if (
            not isinstance(repository, str)
            or repository.count("/") != 1
            or not all(part and part.replace("-", "").replace("_", "").isalnum() for part in repository.split("/"))
            or not isinstance(branch, str)
            or not branch.strip()
            or not isinstance(head_sha, str)
            or len(head_sha) != 40
            or any(character not in "0123456789abcdef" for character in head_sha)
        ):
            raise CapabilityError("mission checkout authority is invalid")
        for key in ("links",):
            if not isinstance(mission.get(key), dict):
                raise CapabilityError(f"mission {key} must be an object")
        for key in ("allowedMutations", "prohibitions", "skills", "evidence"):
            value = mission.get(key)
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise CapabilityError(f"mission {key} must be a string array")
        try:
            copied = json.loads(json.dumps(mission, separators=(",", ":")))
        except (TypeError, ValueError) as exc:
            raise CapabilityError("mission must be JSON-compatible") from exc
        if len(json.dumps(copied, separators=(",", ":"))) > 12000:
            raise CapabilityError("mission must be bounded")
        return copied

    def terminal_wake(self, token: str) -> dict[str, Any]:
        """Wake the owner after a native Stop hook observes terminal Child state."""
        child_id = self._capability_target(token)
        capability = verify_capability(
            token,
            self._secret,
            now=self._clock(),
            action="send_message",
            target_id=child_id,
        )
        status = self._provider.wait_until_terminal(child_id)
        terminal_response = self._provider.terminal_response(child_id)
        message_key = f"terminal:{child_id}:{capability.task_key}"
        envelope = {
            "messageKey": message_key,
            "owningMainId": str(capability.owning_main_id),
            "childId": str(child_id),
            "taskKey": capability.task_key,
            "kind": "RECOVERY_WAKE",
            "status": status,
            "terminalResponse": terminal_response,
        }
        return self._provider.send_message(
            capability.owning_main_id,
            message_key,
            "RECOVERY_WAKE",
            _compact(envelope),
        )

    def send_message(
        self, token: str, target_id: uuid.UUID, message_key: str, kind: str, text: str
    ) -> dict[str, Any]:
        capability = verify_capability(token, self._secret, now=self._clock(), action="send_message", target_id=target_id)
        if kind not in {"RESULT", "NEEDS_INPUT", "CANCEL_MISSION", "RESUME_MISSION", "RECOVERY_WAKE", "NAVIGATION"}:
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

    def cancel_mission(
        self, token: str, target_id: uuid.UUID, task_key: str, message_key: str
    ) -> dict[str, Any]:
        capability = self._main_child_control_capability(
            token, target_id, task_key, "cancel_mission"
        )
        return self._provider.cancel_mission(
            target_id, message_key, task_key, capability.owning_main_id
        )

    def resume_mission(
        self, token: str, target_id: uuid.UUID, task_key: str, message_key: str
    ) -> dict[str, Any]:
        self._main_child_control_capability(
            token, target_id, task_key, "resume_mission"
        )
        return self._provider.resume_mission(target_id, message_key, task_key)

    def _main_child_control_capability(
        self, token: str, target_id: uuid.UUID, task_key: str, action: str
    ):
        capability = verify_capability(
            token,
            self._secret,
            now=self._clock(),
            action=action,
            target_id=self._capability_target(token),
        )
        if capability.role not in {"main", "deputy"}:
            raise CapabilityError("only a Main may control a Child")
        if deterministic_child_id(capability.child_id, task_key) != target_id:
            raise CapabilityError("target is not the Main's deterministic Child")
        return capability

    def send_to_parent(self, token: str, result: dict[str, Any]) -> dict[str, Any]:
        """Send a typed result to the owning Main; the caller cannot choose a peer target."""
        if not isinstance(result, dict):
            raise CapabilityError("result must be an object")
        message_key = result.get("messageKey")
        kind = result.get("kind", "RESULT")
        text = _compact(result)
        capability = verify_capability(token, self._secret, now=self._clock(), action="send_message", target_id=self._capability_target(token))
        if kind not in {"RESULT", "NEEDS_INPUT"} or not isinstance(message_key, str) or not message_key:
            raise CapabilityError("result requires a messageKey and RESULT/NEEDS_INPUT kind")
        envelope = {"messageKey": message_key, "owningMainId": str(capability.owning_main_id), "childId": str(capability.child_id), "taskKey": capability.task_key, "kind": kind, "text": text}
        return self._provider.send_message(capability.owning_main_id, message_key, kind, _compact(envelope))

    def request_user_decision(self, token: str, question: str, options: list[str]) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise CapabilityError("question must be non-empty and bounded")
        if not isinstance(options, list) or not 2 <= len(options) <= 5 or any(not isinstance(x, str) or not x.strip() for x in options):
            raise CapabilityError("options must contain 2-5 non-empty strings")
        capability = verify_capability(token, self._secret, now=self._clock(), action="send_message", target_id=self._capability_target(token))
        import hashlib
        message_key = "decision:" + hashlib.sha256(_compact({"question": question.strip(), "options": options}).encode()).hexdigest()[:24]
        envelope = {"messageKey": message_key, "owningMainId": str(capability.owning_main_id), "childId": str(capability.child_id), "taskKey": capability.task_key, "kind": "NEEDS_INPUT", "text": _compact({"question": question.strip(), "options": options})}
        return self._provider.send_message(capability.owning_main_id, message_key, "NEEDS_INPUT", _compact(envelope))

    def publish_navigation_links(self, token: str, links: dict[str, str]) -> dict[str, Any]:
        if not isinstance(links, dict) or not links or len(links) > 12 or any(not isinstance(k, str) or not isinstance(v, str) for k, v in links.items()):
            raise CapabilityError("links must be a bounded map of strings")
        capability = verify_capability(token, self._secret, now=self._clock(), action="send_message", target_id=self._capability_target(token))
        import hashlib
        message_key = "navigation:" + hashlib.sha256(_compact(links).encode()).hexdigest()[:24]
        envelope = {"messageKey": message_key, "owningMainId": str(capability.owning_main_id), "childId": str(capability.child_id), "taskKey": capability.task_key, "kind": "NAVIGATION", "text": _compact({"links": links})}
        return self._provider.send_message(capability.owning_main_id, message_key, "NAVIGATION", _compact(envelope))

    def _capability_target(self, token: str) -> uuid.UUID:
        return inspect_capability(token, self._secret).child_id


def _compact(value: dict[str, Any]) -> str:
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
