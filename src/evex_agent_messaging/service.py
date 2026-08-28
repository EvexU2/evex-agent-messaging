"""One provider-neutral cross-Discussion message operation."""

from __future__ import annotations

import re
from typing import Any, Protocol
import uuid

from .capability import (
    CapabilityError,
    capability_token,
    deterministic_spec_chat_id,
    inspect_capability,
    main_capability_token,
)


_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_HEAD = re.compile(r"^[0-9a-f]{40}$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,160}$")


class MessagingProvider(Protocol):
    def create_spec_chat(
        self,
        parent_id: uuid.UUID,
        spec_chat_id: uuid.UUID,
        checkout: dict[str, str],
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
        text: str,
        recipient_capability_ref: str | None = None,
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
        checkout: object,
    ) -> dict[str, Any]:
        capability = inspect_capability(token, self._secret)
        if (
            capability.role != "main"
            or capability.sender_id != capability.owning_main_id
        ):
            raise CapabilityError("only a Parent Main may create the Spec Chat")
        bound_checkout = self._validated_checkout(checkout)
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
            bound_checkout,
            spec_capability,
        )
        return {**result, "specChatId": str(spec_chat_id)}

    @staticmethod
    def _validated_checkout(checkout: object) -> dict[str, str]:
        if not isinstance(checkout, dict) or set(checkout) != {
            "repository",
            "branch",
            "headSha",
        }:
            raise CapabilityError("checkout must contain repository, branch, and headSha")
        repository, branch, head = (
            checkout.get("repository"),
            checkout.get("branch"),
            checkout.get("headSha"),
        )
        if (
            not isinstance(repository, str)
            or _REPOSITORY.fullmatch(repository) is None
            or not isinstance(branch, str)
            or _BRANCH.fullmatch(branch) is None
            or branch.startswith(("/", "-"))
            or branch.endswith(("/", "."))
            or ".." in branch
            or "//" in branch
            or "@{" in branch
            or not isinstance(head, str)
            or _HEAD.fullmatch(head) is None
        ):
            raise CapabilityError("Spec Chat checkout authority is invalid")
        return {"repository": repository, "branch": branch, "headSha": head}

    def send_message(
        self,
        token: str,
        target_id: uuid.UUID,
        message_key: str,
        text: str,
    ) -> dict[str, Any]:
        capability = inspect_capability(token, self._secret)
        if target_id == capability.sender_id:
            raise CapabilityError("message target is not allowed")
        if not isinstance(message_key, str) or not 1 <= len(message_key.encode()) <= 200:
            raise CapabilityError("messageKey must be bounded and non-empty")
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 20_000:
            raise CapabilityError("message text must be bounded and non-empty")
        if not self._provider.target_allowed(
            capability.sender_id,
            target_id,
            capability.role,
            capability.owning_main_id,
        ):
            raise CapabilityError("message target is not allowed")
        recipient_capability_ref = None
        if (
            capability.role == "main"
            and target_id == deterministic_spec_chat_id(capability.owning_main_id)
        ):
            recipient_capability_ref = capability_token(
                self._secret,
                owning_main_id=capability.owning_main_id,
                sender_id=target_id,
                task_key="spec",
                role="spec",
            )
        elif (
            capability.role in {"deputy", "spec"}
            and target_id == capability.owning_main_id
        ):
            recipient_capability_ref = main_capability_token(
                self._secret,
                capability.owning_main_id,
            )
        return self._provider.send_message(
            capability.sender_id,
            target_id,
            message_key,
            text,
            recipient_capability_ref,
        )
