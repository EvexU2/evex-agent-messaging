"""Compact self-contained capability references for one Main/Child task tree."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import re
import struct
import uuid


CAPABILITY_NAMESPACE = uuid.UUID("ab1fbaf1-cc0e-4a2e-9cf2-6e4cb2ee8c89")
TASK_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_URLSAFE_B64_RE = re.compile(r"^[A-Za-z0-9_-]*$")
REFERENCE_PREFIX = "evx1_"
_HEADER = struct.Struct(">B16s16sIIBBH")
_SIGNATURE_BYTES = 32
_ROLE_IDS = {
    "main": 1,
    "deputy": 2,
    "spec": 3,
    "plan-author": 4,
    "writer": 5,
    "reviewer": 6,
    "qa": 7,
    "repair": 8,
}
_ROLES = {value: key for key, value in _ROLE_IDS.items()}
_ACTION_BITS = {
    "create_child": 1,
    "send_message": 2,
    "cancel_mission": 4,
    "resume_mission": 8,
    "read_usage": 16,
}


class CapabilityError(ValueError):
    """The supplied capability reference is invalid, expired, forged, or out of scope."""


@dataclass(frozen=True)
class Capability:
    owning_main_id: uuid.UUID
    child_id: uuid.UUID
    task_key: str
    role: str
    allowed_actions: frozenset[str]
    issued_at: datetime
    expires_at: datetime


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    if (
        not isinstance(value, str)
        or len(value) % 4 == 1
        or _URLSAFE_B64_RE.fullmatch(value) is None
    ):
        raise ValueError("invalid unpadded URL-safe base64")
    return base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )


def _epoch(value: datetime) -> int:
    timestamp = int(value.astimezone(timezone.utc).timestamp())
    if timestamp < 0 or timestamp > 0xFFFFFFFF:
        raise CapabilityError("invalid capability inputs")
    return timestamp


def deterministic_child_id(owning_main_id: uuid.UUID, task_key: str) -> uuid.UUID:
    if not isinstance(owning_main_id, uuid.UUID) or not TASK_KEY_RE.fullmatch(task_key):
        raise CapabilityError("invalid owning Main or task key")
    return uuid.uuid5(CAPABILITY_NAMESPACE, f"{owning_main_id}:{task_key}")


def capability_token(
    secret: bytes,
    *,
    owning_main_id: uuid.UUID,
    child_id: uuid.UUID,
    task_key: str,
    role: str,
    allowed_actions: set[str] | frozenset[str],
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    if (
        not secret
        or not isinstance(owning_main_id, uuid.UUID)
        or not isinstance(child_id, uuid.UUID)
        or not TASK_KEY_RE.fullmatch(task_key)
        or role not in _ROLE_IDS
        or not allowed_actions
        or any(action not in _ACTION_BITS for action in allowed_actions)
        or expires_at <= issued_at
    ):
        raise CapabilityError("invalid capability inputs")
    task = task_key.encode("utf-8")
    actions = sum(_ACTION_BITS[action] for action in allowed_actions)
    payload = _HEADER.pack(
        1,
        owning_main_id.bytes,
        child_id.bytes,
        _epoch(issued_at),
        _epoch(expires_at),
        _ROLE_IDS[role],
        actions,
        len(task),
    ) + task
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    return REFERENCE_PREFIX + _b64(payload + signature)


def main_capability_token(secret: bytes, main_id: uuid.UUID, *, issued_at: datetime, expires_at: datetime) -> str:
    """Mint the only reference that may create Children; trusted dispatcher use only."""
    return capability_token(
        secret,
        owning_main_id=main_id,
        child_id=main_id,
        task_key="root",
        role="main",
        allowed_actions={"create_child", "cancel_mission", "resume_mission", "read_usage"},
        issued_at=issued_at,
        expires_at=expires_at,
    )


def inspect_capability(token: str, secret: bytes) -> Capability:
    """Authenticate and decode an opaque reference without action/target/time policy."""
    error = CapabilityError("unknown or invalid capability reference")
    if not isinstance(token, str) or not token.startswith(REFERENCE_PREFIX) or not secret:
        raise error
    try:
        suffix = token[len(REFERENCE_PREFIX):]
        raw = _unb64(suffix)
        if _b64(raw) != suffix:
            raise error
        if len(raw) < _HEADER.size + _SIGNATURE_BYTES:
            raise error
        payload, signature = raw[:-_SIGNATURE_BYTES], raw[-_SIGNATURE_BYTES:]
        if not hmac.compare_digest(hmac.new(secret, payload, hashlib.sha256).digest(), signature):
            raise error
        version, main_raw, child_raw, issued, expires, role_id, action_bits, task_length = _HEADER.unpack(
            payload[: _HEADER.size]
        )
        task_raw = payload[_HEADER.size :]
        if version != 1 or len(task_raw) != task_length:
            raise error
        task_key = task_raw.decode("utf-8")
        role = _ROLES[role_id]
        actions = frozenset(name for name, bit in _ACTION_BITS.items() if action_bits & bit)
        if not TASK_KEY_RE.fullmatch(task_key) or not actions or action_bits != sum(_ACTION_BITS[a] for a in actions):
            raise error
        return Capability(
            uuid.UUID(bytes=main_raw),
            uuid.UUID(bytes=child_raw),
            task_key,
            role,
            actions,
            datetime.fromtimestamp(issued, timezone.utc),
            datetime.fromtimestamp(expires, timezone.utc),
        )
    except (CapabilityError, KeyError, TypeError, ValueError, UnicodeError, struct.error) as exc:
        raise error from exc


def verify_capability(
    token: str,
    secret: bytes,
    *,
    now: datetime,
    action: str,
    target_id: uuid.UUID,
) -> Capability:
    capability = inspect_capability(token, secret)
    normalized_now = now.astimezone(timezone.utc)
    if (
        capability.expires_at <= normalized_now
        or capability.issued_at > normalized_now
        or target_id != capability.child_id
        or action not in capability.allowed_actions
    ):
        raise CapabilityError("capability reference is expired or out of scope")
    return capability
