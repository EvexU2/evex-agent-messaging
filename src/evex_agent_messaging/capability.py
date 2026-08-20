"""Self-contained, signed capabilities for one Main/Child task tree."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
import re
import uuid


CAPABILITY_NAMESPACE = uuid.UUID("ab1fbaf1-cc0e-4a2e-9cf2-6e4cb2ee8c89")
TASK_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class CapabilityError(ValueError):
    """The supplied capability is missing, expired, forged, or out of scope."""


@dataclass(frozen=True)
class Capability:
    owning_main_id: uuid.UUID
    child_id: uuid.UUID
    task_key: str
    role: str
    allowed_actions: frozenset[str]
    issued_at: datetime
    expires_at: datetime


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise CapabilityError(f"invalid {name}")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise CapabilityError(f"invalid {name}") from exc


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
    if not secret or not isinstance(child_id, uuid.UUID) or not TASK_KEY_RE.fullmatch(task_key):
        raise CapabilityError("invalid capability inputs")
    if expires_at <= issued_at:
        raise CapabilityError("capability expiry must be after issue time")
    payload = {
        "owningMainId": str(owning_main_id),
        "childId": str(child_id),
        "taskKey": task_key,
        "role": role,
        "allowedActions": sorted(allowed_actions),
        "issuedAt": _timestamp(issued_at),
        "expiresAt": _timestamp(expires_at),
    }
    encoded = _b64(_canonical(payload))
    signature = hmac.new(secret, encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_b64(signature)}"


def main_capability_token(secret: bytes, main_id: uuid.UUID, *, issued_at: datetime, expires_at: datetime) -> str:
    """Mint the only capability that may create Children; called by the trusted dispatcher, not MCP tools."""
    return capability_token(
        secret,
        owning_main_id=main_id,
        child_id=main_id,
        task_key="root",
        role="main",
        allowed_actions={"create_child"},
        issued_at=issued_at,
        expires_at=expires_at,
    )


def verify_capability(
    token: str,
    secret: bytes,
    *,
    now: datetime,
    action: str,
    target_id: uuid.UUID,
) -> Capability:
    if not isinstance(token, str) or token.count(".") != 1:
        raise CapabilityError("invalid capability")
    encoded, signature = token.split(".", 1)
    expected = hmac.new(secret, encoded.encode(), hashlib.sha256).digest()
    try:
        valid = hmac.compare_digest(expected, _unb64(signature))
        payload = json.loads(_unb64(encoded))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise CapabilityError("invalid capability") from exc
    if not valid or not isinstance(payload, dict):
        raise CapabilityError("invalid capability signature")
    try:
        main_id = uuid.UUID(str(payload["owningMainId"]))
        child_id = uuid.UUID(str(payload["childId"]))
        task_key = str(payload["taskKey"])
        role = str(payload["role"])
        actions = frozenset(str(item) for item in payload["allowedActions"])
        issued_at = _parse_timestamp(payload["issuedAt"], "issuedAt")
        expires_at = _parse_timestamp(payload["expiresAt"], "expiresAt")
    except (KeyError, TypeError, ValueError) as exc:
        raise CapabilityError("invalid capability fields") from exc
    now = now.astimezone(timezone.utc)
    if expires_at <= now or issued_at > now or target_id != child_id or action not in actions:
        raise CapabilityError("capability is expired or out of scope")
    return Capability(main_id, child_id, task_key, role, actions, issued_at, expires_at)
