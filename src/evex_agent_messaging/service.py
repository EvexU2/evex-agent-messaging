"""One provider-neutral cross-Discussion message operation."""

from __future__ import annotations

import re
import json
from typing import Any, Protocol
import uuid

from .capability import (
    CapabilityError,
    capability_token,
    deterministic_spec_chat_id,
    inspect_capability,
)


_MESSAGE_KEY = re.compile(r"^[\x21-\x7e]{1,200}$")
_CREDENTIAL = re.compile(
    r"(?:\b(?:sk|gh[pousr])_[A-Za-z0-9_-]{8,}|\bgithub_pat_[A-Za-z0-9_]{8,}|"
    r"\bBearer\s+\S+|\b(?:authorization|x-session-api-key)\s*:|"
    r"\b(?:EVEX_MESSAGING_SECRET|OPENHANDS_API_KEY)\b)",
    re.IGNORECASE,
)
_MACHINE_DELIMITER = re.compile(r"<!--|-->")
_MAX_MESSAGE_BYTES = 20_000
_MAX_SUMMARY_BYTES = 2_000
_MAX_EVIDENCE_ITEMS = 100
_MAX_EVIDENCE_ITEM_BYTES = 2_000


class MessagingProvider(Protocol):
    def create_spec_chat(
        self,
        parent_id: uuid.UUID,
        spec_chat_id: uuid.UUID,
        capability_ref: str,
    ) -> dict[str, Any]: ...

    def target_allowed(
        self,
        sender_id: uuid.UUID,
        target_id: uuid.UUID,
        role: str,
        owning_main_id: uuid.UUID,
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
    def __init__(self, provider: MessagingProvider, secret: bytes) -> None:
        if not secret:
            raise ValueError("messaging secret is required")
        self._provider = provider
        self._secret = secret

    def readiness(self) -> bool:
        try:
            return bool(self._provider.readiness())
        except Exception:
            return False

    def create_spec_chat(
        self,
        token: str,
    ) -> dict[str, Any]:
        capability = inspect_capability(token, self._secret)
        if (
            capability.role != "main"
            or capability.sender_id != capability.owning_main_id
        ):
            raise CapabilityError("only a Parent Main may create the Spec Chat")
        spec_chat_id = deterministic_spec_chat_id(capability.sender_id)
        spec_capability = capability_token(
            self._secret,
            owning_main_id=capability.sender_id,
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
        if not self._provider.target_allowed(
            capability.sender_id,
            target_id,
            capability.role,
            capability.owning_main_id,
        ):
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
        if not isinstance(evidence, dict) or not {"outcome", "evidence", "findings", "nextBoundary"} <= set(evidence) or set(evidence) - {"outcome", "revision", "evidence", "findings", "nextBoundary"}:
            raise CapabilityError("message AI evidence is invalid")
        for key in ("outcome", "nextBoundary"):
            value = evidence.get(key)
            if not isinstance(value, str) or not value.strip() or len(value.encode()) > _MAX_EVIDENCE_ITEM_BYTES:
                raise CapabilityError("message AI evidence is invalid")
        if "revision" in evidence:
            revision = evidence["revision"]
            if not isinstance(revision, str) or not revision.strip() or len(revision.encode()) > _MAX_EVIDENCE_ITEM_BYTES:
                raise CapabilityError("message AI evidence is invalid")
        for key in ("evidence", "findings"):
            values = evidence.get(key)
            if not isinstance(values, list) or len(values) > _MAX_EVIDENCE_ITEMS or any(
                not isinstance(value, str) or not value.strip() or len(value.encode()) > _MAX_EVIDENCE_ITEM_BYTES
                for value in values
            ):
                raise CapabilityError("message AI evidence is invalid")
        try:
            canonical = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            raise CapabilityError("message AI evidence is invalid") from None
        if len(canonical.encode()) > _MAX_MESSAGE_BYTES or _MACHINE_DELIMITER.search(canonical) or _CREDENTIAL.search(canonical):
            raise CapabilityError("message is unsafe or exceeds its byte budget")
        return json.loads(canonical)
