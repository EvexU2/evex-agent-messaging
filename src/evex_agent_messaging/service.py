"""Provider-neutral direct Conversation creation and messaging."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Protocol
import uuid

from .capability import (
    CapabilityError,
    ProjectCapability,
    TASK_KEY_RE,
    capability_token,
    deterministic_spec_chat_id,
    inspect_capability,
    project_capability_token,
)
from .delivery import MainDeliveryRequest


_MESSAGE_KEY = re.compile(r"^[\x21-\x7e]{1,200}$")
_CREDENTIAL = re.compile(
    r"(?:\b(?:sk|gh[pousr])_[A-Za-z0-9_-]{8,}|\bgithub_pat_[A-Za-z0-9_]{8,}|"
    r"evx[23]_[A-Za-z0-9_-]+|"
    r"\bBearer\s+\S+|\b(?:authorization|x-session-api-key)\s*:|"
    r"\b(?:EVEX_MESSAGING_SECRET|OPENHANDS_API_KEY)\b)",
    re.IGNORECASE,
)
_MACHINE_DELIMITER = re.compile(r"<!--|-->")
_MAX_MESSAGE_BYTES = 20_000
_MAX_SUMMARY_BYTES = 2_000
_MAX_EVIDENCE_ITEMS = 100
_MAX_EVIDENCE_ITEM_BYTES = 2_000
_MAX_ARTIFACT_BYTES = 64_000
_MAX_TERMINAL_MESSAGE_BYTES = 80_000
_SPECIALIST_REASONING = {"spec-review": "high"}
_SPECIALIST_SKILLS = {
    "plan": "evex-delivery-planning",
    "plan-review": "evex-delivery-planning",
    "project-review": "evex-project-review",
    "qa": "evex-delivery-qa",
    "code-review": "evex-delivery-reviewer",
    "spec-review": "evex-spec-review",
    "writer": "evex-delivery-writer",
}
_ROLE_SPECIALISTS = {
    "issue": {"plan", "plan-review", "code-review", "qa"},
    "subissue": {"writer", "code-review", "qa"},
    "spec": {"spec-review"},
    "project": {"project-review"},
    "specialist": set(_SPECIALIST_SKILLS),
}


class MessagingProvider(Protocol):
    def deliver_main(self, request: MainDeliveryRequest) -> dict[str, Any]: ...

    def provisioning_allowed(self, credential: str | None) -> bool: ...

    def project_binding(self, conversation_id: uuid.UUID) -> str: ...

    def install_project_capability(
        self, conversation_id: uuid.UUID, project_id: str, capability_ref: str,
    ) -> dict[str, Any]: ...

    def create_spec_chat(
        self,
        parent_id: uuid.UUID,
        spec_chat_id: uuid.UUID,
        capability_ref: str,
    ) -> dict[str, Any]: ...

    def start_specialist(
        self,
        parent_id: uuid.UUID,
        specialist_id: uuid.UUID,
        capability_ref: str,
        mission: dict[str, Any],
    ) -> dict[str, Any]: ...

    def target_allowed(
        self,
        sender_id: uuid.UUID,
        target_id: uuid.UUID,
        role: str,
        owning_issue_id: uuid.UUID | None,
        project_id: str | None = None,
    ) -> bool: ...

    def send_message(
        self,
        sender_id: uuid.UUID,
        target_id: uuid.UUID,
        message_key: str,
        message: dict[str, Any],
    ) -> dict[str, Any]: ...

    def readiness(self) -> bool: ...


class MessagingService:
    def __init__(
        self,
        provider: MessagingProvider,
        secret: bytes,
        *,
        delivery_secret: bytes = b"",
    ) -> None:
        if not secret:
            raise ValueError("messaging secret is required")
        self._provider = provider
        self._secret = secret
        self._delivery_secret = delivery_secret

    def delivery_allowed(self, credential: str | None) -> bool:
        return (
            isinstance(credential, str)
            and len(self._delivery_secret) >= 32
            and hmac.compare_digest(credential.encode(), self._delivery_secret)
        )

    def deliver_main(self, credential: str | None, request: object) -> dict[str, Any]:
        """Private Gateway operation; never exposed through the MCP tool list."""
        if not self.delivery_allowed(credential):
            raise PermissionError("delivery credential denied")
        parsed = MainDeliveryRequest.parse(request)
        result = self._provider.deliver_main(parsed)
        if not isinstance(result, dict):
            raise RuntimeError("provider returned an invalid delivery result")
        if result.get("accepted") is True:
            if (
                set(result) != {"accepted", "conversationId", "outcome"}
                or result.get("conversationId") != str(parsed.target.conversation_id)
                or result.get("outcome") not in {"created", "woken"}
            ):
                raise RuntimeError("provider returned an invalid delivery result")
        elif result != {
            "accepted": False,
            "reason": "target_missing_not_intake_authorized",
        }:
            raise RuntimeError("provider returned an invalid delivery result")
        return result

    def readiness(self) -> bool:
        try:
            return bool(self._provider.readiness())
        except Exception:
            return False

    def provisioning_allowed(self, credential: str | None) -> bool:
        return self._provider.provisioning_allowed(credential)

    def provision_project_capability(self, request: object) -> dict[str, Any]:
        """Private host trigger only; never called by public MCP operations."""
        error = CapabilityError("invalid Project capability request")
        if (
            not isinstance(request, dict) or set(request) != {"schemaVersion", "conversationId"}
            or type(request["schemaVersion"]) is not int or request["schemaVersion"] != 1
            or not isinstance(request["conversationId"], str)
        ):
            raise error
        try:
            conversation_id = uuid.UUID(request["conversationId"])
        except ValueError:
            raise error from None
        if str(conversation_id) != request["conversationId"]:
            raise error
        project_id = self._provider.project_binding(conversation_id)
        token = project_capability_token(self._secret, conversation_id, project_id)
        return self._provider.install_project_capability(conversation_id, project_id, token)

    def create_spec_chat(
        self,
        token: str,
    ) -> dict[str, Any]:
        capability = inspect_capability(token, self._secret)
        if (
            capability.role != "issue"
            or capability.sender_id != capability.owning_issue_id
        ):
            raise CapabilityError("only an Issue Conversation may create the Spec Chat")
        spec_chat_id = deterministic_spec_chat_id(capability.sender_id)
        spec_capability = capability_token(
            self._secret,
            owning_issue_id=capability.sender_id,
            sender_id=spec_chat_id,
            task_key="spec",
            role="spec",
        )
        result = self._provider.create_spec_chat(
            capability.sender_id,
            spec_chat_id,
            spec_capability,
        )
        return {**result, "specChatId": str(spec_chat_id)}

    def start_specialist(
        self,
        token: str,
        *,
        mission_key: str,
        prompt: str,
        agent_type: str,
        description: str,
        skills: object,
    ) -> dict[str, Any]:
        parent = inspect_capability(token, self._secret)
        role = parent.role
        if agent_type not in _ROLE_SPECIALISTS.get(role, set()):
            raise CapabilityError(f"{role} may not start {agent_type} Specialists")
        if not isinstance(mission_key, str) or TASK_KEY_RE.fullmatch(mission_key) is None:
            raise CapabilityError("missionKey is invalid")
        normalized_prompt = self._bounded_text(prompt, "prompt", 32_768)
        normalized_description = self._bounded_text(
            description, "description", 60, collapse_whitespace=True
        )
        skill_names = self._skill_names(skills)
        role_skill = _SPECIALIST_SKILLS[agent_type]
        if role_skill not in skill_names:
            skill_names.insert(0, role_skill)
        conflicting = (set(skill_names) & set(_SPECIALIST_SKILLS.values())) - {role_skill}
        if conflicting:
            raise CapabilityError("Specialist may not combine canonical role skills")
        if len(",".join(skill_names)) > 256:
            raise CapabilityError("skills exceed the OpenHands tag limit")

        specialist_id = uuid.uuid5(parent.sender_id, f"evex-specialist:{mission_key}")
        mission_id = hashlib.sha256(
            f"{parent.sender_id}\0{mission_key}".encode()
        ).hexdigest()
        prompt_digest = hashlib.sha256(normalized_prompt.encode()).hexdigest()
        reasoning = _SPECIALIST_REASONING.get(agent_type, "medium")
        descriptor_digest = hashlib.sha256(
            json.dumps(
                {
                    "agentType": agent_type,
                    "description": normalized_description,
                    "prompt": normalized_prompt,
                    "reasoning": reasoning,
                    "skills": skill_names,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        specialist_capability = capability_token(
            self._secret,
            owning_issue_id=parent.sender_id,
            sender_id=specialist_id,
            task_key=mission_id,
            role="specialist",
        )
        result = self._provider.start_specialist(
            parent.sender_id,
            specialist_id,
            specialist_capability,
            {
                "missionKey": mission_key,
                "missionId": mission_id,
                "prompt": normalized_prompt,
                "promptDigest": prompt_digest,
                "agentType": agent_type,
                "description": normalized_description,
                "skills": skill_names,
                "reasoning": reasoning,
                "descriptorDigest": descriptor_digest,
                "parentRole": role,
            },
        )
        return {**result, "conversationId": str(specialist_id)}

    @staticmethod
    def _bounded_text(
        value: object,
        label: str,
        maximum: int,
        *,
        collapse_whitespace: bool = False,
    ) -> str:
        if not isinstance(value, str) or not value.strip():
            raise CapabilityError(f"{label} is required")
        normalized = value.strip()
        if collapse_whitespace:
            normalized = " ".join(normalized.replace("·", "-").split())
        if len(normalized) > maximum:
            raise CapabilityError(f"{label} exceeds {maximum} characters")
        return normalized

    @staticmethod
    def _skill_names(skills: object) -> list[str]:
        if not isinstance(skills, list) or len(skills) > 32:
            raise CapabilityError("skills must be a bounded array")
        result: list[str] = []
        for value in skills:
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 64
                or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in value)
            ):
                raise CapabilityError("skills must contain canonical names")
            if value not in result:
                result.append(value)
        return result

    def send_message(
        self,
        token: str,
        target_id: uuid.UUID,
        message_key: str,
        message: object,
    ) -> dict[str, Any]:
        capability = inspect_capability(token, self._secret)
        if target_id == capability.sender_id:
            raise CapabilityError("message target is not allowed")
        if (
            not isinstance(message_key, str)
            or _MESSAGE_KEY.fullmatch(message_key) is None
            or _CREDENTIAL.search(message_key)
        ):
            raise CapabilityError("messageKey must be bounded and non-empty")
        bounded_message = self._validated_message(message)
        if isinstance(capability, ProjectCapability):
            allowed = self._provider.target_allowed(
                capability.sender_id, target_id, capability.role, None, capability.project_id,
            )
        else:
            allowed = self._provider.target_allowed(
                capability.sender_id, target_id, capability.role, capability.owning_issue_id,
            )
        if not allowed:
            raise CapabilityError("message target is not allowed")
        return self._provider.send_message(
            capability.sender_id,
            target_id,
            message_key,
            bounded_message,
        )

    @staticmethod
    def _validated_message(message: object) -> dict[str, Any]:
        if not isinstance(message, dict) or set(message) != {"humanSummary", "aiEvidence"}:
            raise CapabilityError("message must be a structured human summary and AI evidence")
        summary, evidence = message.get("humanSummary"), message.get("aiEvidence")
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or len(summary.encode()) > _MAX_SUMMARY_BYTES
            or _MACHINE_DELIMITER.search(summary)
            or _CREDENTIAL.search(summary)
        ):
            raise CapabilityError("message summary is invalid")
        if not isinstance(evidence, dict) or not {"outcome", "evidence", "findings", "nextBoundary"} <= set(evidence) or set(evidence) - {"outcome", "revision", "evidence", "findings", "nextBoundary", "artifact"}:
            raise CapabilityError("message AI evidence is invalid")
        for key in ("outcome", "nextBoundary"):
            value = evidence.get(key)
            if not isinstance(value, str) or not value.strip() or len(value.encode()) > _MAX_EVIDENCE_ITEM_BYTES:
                raise CapabilityError("message AI evidence is invalid")
        if "revision" in evidence:
            revision = evidence["revision"]
            if not isinstance(revision, str) or not revision.strip() or len(revision.encode()) > _MAX_EVIDENCE_ITEM_BYTES:
                raise CapabilityError("message AI evidence is invalid")
        if "artifact" in evidence:
            artifact = evidence["artifact"]
            if not isinstance(artifact, str) or not artifact.strip():
                raise CapabilityError("message aiEvidence.artifact must be a non-empty string")
            if len(artifact.encode()) > _MAX_ARTIFACT_BYTES:
                raise CapabilityError(
                    f"message aiEvidence.artifact exceeds {_MAX_ARTIFACT_BYTES} UTF-8 bytes"
                )
        for key in ("evidence", "findings"):
            values = evidence.get(key)
            if not isinstance(values, list):
                raise CapabilityError(f"message aiEvidence.{key} must be an array")
            if len(values) > _MAX_EVIDENCE_ITEMS:
                raise CapabilityError(
                    f"message aiEvidence.{key} exceeds {_MAX_EVIDENCE_ITEMS} items"
                )
            for index, value in enumerate(values):
                if not isinstance(value, str) or not value.strip():
                    raise CapabilityError(
                        f"message aiEvidence.{key}[{index}] must be a non-empty string"
                    )
                if len(value.encode()) > _MAX_EVIDENCE_ITEM_BYTES:
                    raise CapabilityError(
                        f"message aiEvidence.{key}[{index}] exceeds "
                        f"{_MAX_EVIDENCE_ITEM_BYTES} UTF-8 bytes"
                    )
        try:
            canonical = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            raise CapabilityError("message AI evidence is invalid") from None
        maximum = _MAX_TERMINAL_MESSAGE_BYTES if "artifact" in evidence else _MAX_MESSAGE_BYTES
        if len(canonical.encode()) > maximum:
            raise CapabilityError(f"message exceeds {maximum} UTF-8 bytes")
        evidence_without_artifact = {key: value for key, value in evidence.items() if key != "artifact"}
        non_artifact = json.dumps(
            {"humanSummary": summary, "aiEvidence": evidence_without_artifact},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if _MACHINE_DELIMITER.search(non_artifact):
            raise CapabilityError("message contains a reserved machine delimiter outside artifact")
        if _CREDENTIAL.search(canonical):
            raise CapabilityError("message contains a credential-like value")
        return json.loads(canonical)
