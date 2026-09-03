"""Compact HMAC capability for one durable Discussion."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import hmac
import re
import struct
import uuid


REFERENCE_PREFIX = "evx2_"
PROJECT_REFERENCE_PREFIX = "evx3_"
NATIVE_ID_MAX_BYTES = 256
_NATIVE_ID = re.compile(rf"^[\x21-\x7e]{{1,{NATIVE_ID_MAX_BYTES}}}$")
SPEC_CHAT_NAMESPACE = uuid.UUID("ab1fbaf1-cc0e-4a2e-9cf2-6e4cb2ee8c89")
TASK_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEADER = struct.Struct(">B16s16sBBH")
_PROJECT_HEADER = struct.Struct(">B16sBH")
_SIGNATURE_BYTES = 32
_ROLE_IDS = {"issue": 1, "subissue": 2, "spec": 3, "specialist": 4}
_ROLES = {value: key for key, value in _ROLE_IDS.items()}
_SEND_MESSAGE_BIT = 2
_CREATE_SPEC_CHAT_BIT = 1


class CapabilityError(ValueError):
    """The capability is malformed, forged, or out of scope."""


@dataclass(frozen=True)
class Capability:
    owning_issue_id: uuid.UUID
    sender_id: uuid.UUID
    task_key: str
    role: str


@dataclass(frozen=True)
class ProjectCapability:
    sender_id: uuid.UUID
    project_id: str
    role: str = "project"


def valid_native_id(value: object) -> bool:
    """Treat native node IDs as opaque, bounded visible ASCII, not invented prefixes."""
    return isinstance(value, str) and _NATIVE_ID.fullmatch(value) is not None


def project_capability_token(secret: bytes, sender_id: uuid.UUID, project_id: str) -> str:
    if not secret or not isinstance(sender_id, uuid.UUID) or not valid_native_id(project_id):
        raise CapabilityError("invalid Project capability inputs")
    project = project_id.encode("ascii")
    payload = _PROJECT_HEADER.pack(3, sender_id.bytes, _SEND_MESSAGE_BIT, len(project)) + project
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    return PROJECT_REFERENCE_PREFIX + _b64(payload + signature)


def deterministic_spec_chat_id(owning_issue_id: uuid.UUID) -> uuid.UUID:
    if not isinstance(owning_issue_id, uuid.UUID):
        raise CapabilityError("invalid owning Main")
    return uuid.uuid5(SPEC_CHAT_NAMESPACE, f"{owning_issue_id}:spec")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def capability_token(
    secret: bytes,
    *,
    owning_issue_id: uuid.UUID,
    sender_id: uuid.UUID,
    task_key: str,
    role: str,
) -> str:
    if (
        not secret
        or not isinstance(owning_issue_id, uuid.UUID)
        or not isinstance(sender_id, uuid.UUID)
        or not TASK_KEY_RE.fullmatch(task_key)
        or role not in _ROLE_IDS
        or (role == "issue" and owning_issue_id != sender_id)
        or (role in {"subissue", "spec", "specialist"} and owning_issue_id == sender_id)
    ):
        raise CapabilityError("invalid capability inputs")
    task = task_key.encode()
    actions = _SEND_MESSAGE_BIT | (_CREATE_SPEC_CHAT_BIT if role == "issue" else 0)
    payload = _HEADER.pack(
        2,
        owning_issue_id.bytes,
        sender_id.bytes,
        _ROLE_IDS[role],
        actions,
        len(task),
    ) + task
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    return REFERENCE_PREFIX + _b64(payload + signature)


def issue_capability_token(
    secret: bytes,
    issue_id: uuid.UUID,
) -> str:
    return capability_token(
        secret,
        owning_issue_id=issue_id,
        sender_id=issue_id,
        task_key="root",
        role="issue",
    )


def inspect_capability(token: str, secret: bytes) -> Capability | ProjectCapability:
    error = CapabilityError("unknown or invalid capability reference")
    if not isinstance(token, str) or not secret:
        raise error
    if token.startswith(PROJECT_REFERENCE_PREFIX):
        prefix, header = PROJECT_REFERENCE_PREFIX, _PROJECT_HEADER
    elif token.startswith(REFERENCE_PREFIX):
        prefix, header = REFERENCE_PREFIX, _HEADER
    else:
        raise error
    try:
        # Bound input before decoding while preserving every existing v2 token byte.
        if len(token) > 512:
            raise error
        encoded = token[len(prefix) :]
        raw = _unb64(encoded)
        if _b64(raw) != encoded:
            raise error
        if len(raw) < header.size + _SIGNATURE_BYTES:
            raise error
        payload, signature = raw[:-_SIGNATURE_BYTES], raw[-_SIGNATURE_BYTES:]
        if not hmac.compare_digest(hmac.new(secret, payload, hashlib.sha256).digest(), signature):
            raise error
        if prefix == PROJECT_REFERENCE_PREFIX:
            version, sender, actions, project_length = header.unpack(payload[:header.size])
            project = payload[header.size:].decode("ascii")
            if (
                version != 3 or actions != _SEND_MESSAGE_BIT
                or len(project) != project_length or not valid_native_id(project)
            ):
                raise error
            return ProjectCapability(uuid.UUID(bytes=sender), project)
        version, owner, sender, role_id, actions, task_length = _HEADER.unpack(
            payload[: _HEADER.size]
        )
        task = payload[_HEADER.size :].decode()
        role = _ROLES[role_id]
        if (
            version != 2
            or actions
            != (_SEND_MESSAGE_BIT | (_CREATE_SPEC_CHAT_BIT if role == "issue" else 0))
            or len(task.encode()) != task_length
            or not TASK_KEY_RE.fullmatch(task)
        ):
            raise error
        capability = Capability(
            uuid.UUID(bytes=owner),
            uuid.UUID(bytes=sender),
            task,
            role,
        )
        if (role == "issue") != (capability.owning_issue_id == capability.sender_id):
            raise error
        return capability
    except (KeyError, TypeError, ValueError, UnicodeError, struct.error) as exc:
        raise error from exc
