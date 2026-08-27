"""One provider-neutral cross-Discussion message operation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol
import uuid

from .capability import CapabilityError, verify_capability


class MessagingProvider(Protocol):
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
    ) -> dict[str, Any]: ...

    def readiness(self) -> bool: ...


class MessagingService:
    def __init__(self, provider: MessagingProvider, secret: bytes, *, clock=None) -> None:
        if not secret:
            raise ValueError("messaging secret is required")
        self._provider = provider
        self._secret = secret
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def readiness(self) -> bool:
        try:
            return bool(self._provider.readiness())
        except Exception:
            return False

    def send_message(
        self,
        token: str,
        target_id: uuid.UUID,
        message_key: str,
        text: str,
    ) -> dict[str, Any]:
        capability = verify_capability(token, self._secret, now=self._clock())
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
        return self._provider.send_message(
            capability.sender_id,
            target_id,
            message_key,
            text,
        )
