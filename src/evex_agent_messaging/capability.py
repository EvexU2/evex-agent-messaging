"""Compact HMAC capability for one durable Discussion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import re
import struct
import uuid


REFERENCE_PREFIX = "evx1_"
SPEC_CHAT_NAMESPACE = uuid.UUID("ab1fbaf1-cc0e-4a2e-9cf2-6e4cb2ee8c89")
TASK_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEADER = struct.Struct(">B16s16sIIBBH")
_SIGNATURE_BYTES = 32
_ROLE_IDS = {"main": 1, "deputy": 2, "spec": 3}
_ROLES = {value: key for key, value in _ROLE_IDS.items()}
_SEND_MESSAGE_BIT = 2
_CREATE_SPEC_CHAT_BIT = 1


class CapabilityError(ValueError):
    """The capability is malformed, forged, expired, or out of scope."""


@dataclass(frozen=True)
class Capability:
    owning_main_id: uuid.UUID
    sender_id: uuid.UUID
    task_key: str
    role: str
    issued_at: datetime
    expires_at: datetime


def deterministic_spec_chat_id(owning_main_id: uuid.UUID) -> uuid.UUID:
    if not isinstance(owning_main_id, uuid.UUID):
        raise CapabilityError("invalid owning Main")
    return uuid.uuid5(SPEC_CHAT_NAMESPACE, f"{owning_main_id}:spec")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _epoch(value: datetime) -> int:
    timestamp = int(value.astimezone(timezone.utc).timestamp())
    if not 0 <= timestamp <= 0xFFFFFFFF:
        raise CapabilityError("invalid capability inputs")
    return timestamp


def capability_token(
    secret: bytes,
    *,
    owning_main_id: uuid.UUID,
    sender_id: uuid.UUID,
    task_key: str,
    role: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    if (
        not secret
        or not isinstance(owning_main_id, uuid.UUID)
        or not isinstance(sender_id, uuid.UUID)
        or not TASK_KEY_RE.fullmatch(task_key)
        or role not in _ROLE_IDS
        or expires_at <= issued_at
        or (role == "main" and owning_main_id != sender_id)
        or (role in {"deputy", "spec"} and owning_main_id == sender_id)
    ):
        raise CapabilityError("invalid capability inputs")
    task = task_key.encode()
    actions = _SEND_MESSAGE_BIT | (_CREATE_SPEC_CHAT_BIT if role == "main" else 0)
    payload = _HEADER.pack(
        1,
        owning_main_id.bytes,
        sender_id.bytes,
        _epoch(issued_at),
        _epoch(expires_at),
        _ROLE_IDS[role],
        actions,
        len(task),
    ) + task
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    return REFERENCE_PREFIX + _b64(payload + signature)


def main_capability_token(
    secret: bytes,
    main_id: uuid.UUID,
    *,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    return capability_token(
        secret,
        owning_main_id=main_id,
        sender_id=main_id,
        task_key="root",
        role="main",
        issued_at=issued_at,
        expires_at=expires_at,
    )


def inspect_capability(token: str, secret: bytes) -> Capability:
    error = CapabilityError("unknown or invalid capability reference")
    if not isinstance(token, str) or not token.startswith(REFERENCE_PREFIX) or not secret:
        raise error
    try:
        encoded = token[len(REFERENCE_PREFIX) :]
        raw = _unb64(encoded)
        if _b64(raw) != encoded:
            raise error
        if len(raw) < _HEADER.size + _SIGNATURE_BYTES:
            raise error
        payload, signature = raw[:-_SIGNATURE_BYTES], raw[-_SIGNATURE_BYTES:]
        if not hmac.compare_digest(hmac.new(secret, payload, hashlib.sha256).digest(), signature):
            raise error
        version, owner, sender, issued, expires, role_id, actions, task_length = _HEADER.unpack(
            payload[: _HEADER.size]
        )
        task = payload[_HEADER.size :].decode()
        role = _ROLES[role_id]
        if (
            version != 1
            or actions
            != (_SEND_MESSAGE_BIT | (_CREATE_SPEC_CHAT_BIT if role == "main" else 0))
            or len(task.encode()) != task_length
            or not TASK_KEY_RE.fullmatch(task)
        ):
            raise error
        capability = Capability(
            uuid.UUID(bytes=owner),
            uuid.UUID(bytes=sender),
            task,
            role,
            datetime.fromtimestamp(issued, timezone.utc),
            datetime.fromtimestamp(expires, timezone.utc),
        )
        if (role == "main") != (capability.owning_main_id == capability.sender_id):
            raise error
        return capability
    except (KeyError, TypeError, ValueError, UnicodeError, struct.error) as exc:
        raise error from exc


def verify_capability(token: str, secret: bytes, *, now: datetime) -> Capability:
    capability = inspect_capability(token, secret)
    current = now.astimezone(timezone.utc)
    if capability.issued_at > current or capability.expires_at <= current:
        raise CapabilityError("capability reference is expired or out of scope")
    return capability
