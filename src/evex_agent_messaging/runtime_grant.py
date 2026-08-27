"""Opaque Environment grants shared with the Runtime MCP verifier."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import uuid
from typing import Any

from .capability import CapabilityError


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAMESPACE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_MAX_CLAIMS_BYTES = 16_384


def _canonical(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CapabilityError("Environment grant must be bounded JSON") from exc
    if len(encoded) > _MAX_CLAIMS_BYTES:
        raise CapabilityError("Environment grant must be bounded JSON")
    return encoded


def _utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CapabilityError("Environment grant expiry must be UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CapabilityError("Environment grant expiry must be UTC") from exc
    return parsed.astimezone(timezone.utc)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def environment_grant_claims(
    binding: object, *, principal_id: uuid.UUID, now: datetime
) -> dict[str, Any]:
    """Validate Main-produced Environment identity and add non-widenable claims."""
    required = {
        "environmentId", "generation", "candidateRevisionId", "configurationDigest",
        "scenarios", "namespace", "toolboxPod", "manifestDigest", "expiresAt",
    }
    if not isinstance(binding, dict) or set(binding) != required:
        raise CapabilityError("Environment grant binding is incomplete or widened")
    for name in (
        "environmentId", "generation", "candidateRevisionId",
        "configurationDigest", "manifestDigest",
    ):
        if not isinstance(binding.get(name), str) or _DIGEST.fullmatch(binding[name]) is None:
            raise CapabilityError(f"Environment grant {name} is invalid")
    scenarios = binding.get("scenarios")
    if (
        not isinstance(scenarios, list)
        or not 1 <= len(scenarios) <= 32
        or any(not isinstance(item, str) or not item.strip() or len(item) > 200 for item in scenarios)
        or len(set(scenarios)) != len(scenarios)
    ):
        raise CapabilityError("Environment grant scenarios are invalid")
    namespace = binding.get("namespace")
    if not isinstance(namespace, str) or _NAMESPACE.fullmatch(namespace) is None:
        raise CapabilityError("Environment grant namespace is invalid")
    if binding.get("toolboxPod") != "toolbox":
        raise CapabilityError("Environment grant Toolbox identity is invalid")
    if _utc(binding.get("expiresAt")) <= now.astimezone(timezone.utc):
        raise CapabilityError("Environment grant is expired")
    claims = {
        "schemaVersion": 1,
        "principalId": str(principal_id),
        **binding,
        "capabilities": {"toolbox": True},
    }
    return json.loads(_canonical(claims))


def mint_environment_grant(secret: bytes, claims: object) -> str:
    """Mint the cross-runtime v1 canonical-JSON/HMAC token."""
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise CapabilityError("Runtime MCP grant secret must contain at least 32 bytes")
    encoded = _canonical(claims)
    payload = _b64(encoded)
    signed = f"v1.{payload}".encode("ascii")
    signature = hmac.new(secret, signed, hashlib.sha256).digest()
    return f"v1.{payload}.{_b64(signature)}"
