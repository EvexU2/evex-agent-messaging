"""Provider-neutral contract for one Gateway-routed Discussion delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any
import uuid


DELIVERY_SCHEMA_VERSION = "evex.agent-delivery/1"
EVENT_SCHEMA_VERSION = "evex.github-event/1"
MAX_DELIVERY_BYTES = 32_768
WORKSPACE_REPOSITORY = "EvexU2/evex-u-workspace"

_REPOSITORY = re.compile(r"^EvexU2/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_EVENT_FIELDS = {
    "schemaVersion", "eventKey", "deliveryGuid", "event", "action",
    "repository", "resourceUrl", "resourceNumber", "actor", "installationId",
    "payloadDigest", "observedAt",
}
_TARGET_FIELDS = {
    "conversationId", "issueRepository", "issueNumber", "issueTitle",
    "deliveryRole", "parentIssue", "allowCreate", "recoveryMode", "source",
}


class DeliveryContractError(ValueError):
    """The private request is malformed or violates a routing invariant."""


@dataclass(frozen=True)
class DeliverySource:
    repository: str
    branch: str


@dataclass(frozen=True)
class DeliveryTarget:
    conversation_id: uuid.UUID
    issue_repository: str
    issue_number: int
    issue_title: str
    delivery_role: str
    parent_issue: int | None
    allow_create: bool
    recovery_mode: bool
    source: DeliverySource


@dataclass(frozen=True)
class MainDeliveryRequest:
    target: DeliveryTarget
    event: dict[str, Any]

    @classmethod
    def parse(cls, value: object) -> "MainDeliveryRequest":
        error = DeliveryContractError("invalid delivery request")
        if (
            not isinstance(value, dict)
            or set(value) != {"schemaVersion", "target", "event"}
            or value.get("schemaVersion") != DELIVERY_SCHEMA_VERSION
            or not isinstance(value.get("target"), dict)
            or not isinstance(value.get("event"), dict)
        ):
            raise error
        raw_target = value["target"]
        raw_event = value["event"]
        if set(raw_target) != _TARGET_FIELDS or set(raw_event) != _EVENT_FIELDS:
            raise error

        try:
            conversation_id = uuid.UUID(raw_target["conversationId"])
        except (AttributeError, TypeError, ValueError):
            raise error from None
        if str(conversation_id) != raw_target["conversationId"]:
            raise error

        repository = raw_target["issueRepository"]
        number = raw_target["issueNumber"]
        title = raw_target["issueTitle"]
        role = raw_target["deliveryRole"]
        parent = raw_target["parentIssue"]
        allow_create = raw_target["allowCreate"]
        recovery_mode = raw_target["recoveryMode"]
        source = raw_target["source"]
        if (
            not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None
            or type(number) is not int or number < 1
            or not isinstance(title, str) or not title.strip() or len(title.encode()) > 512
            or role not in {"issue", "subissue"}
            or type(allow_create) is not bool
            or type(recovery_mode) is not bool
            or not isinstance(source, dict) or set(source) != {"repository", "branch"}
        ):
            raise error
        source_repository, branch = source["repository"], source["branch"]
        if (
            not isinstance(source_repository, str)
            or _REPOSITORY.fullmatch(source_repository) is None
            or not isinstance(branch, str)
            or _BRANCH.fullmatch(branch) is None
        ):
            raise error
        if role == "issue":
            if (
                parent is not None
                or repository != WORKSPACE_REPOSITORY
                or source_repository != WORKSPACE_REPOSITORY
                or branch != "main"
            ):
                raise error
        elif (
            type(parent) is not int or parent < 1
            or source_repository != repository
        ):
            raise error
        if recovery_mode and (
            not allow_create
            or (raw_event["event"] == "issues" and raw_event["action"] == "labeled")
        ):
            raise error

        cls._validate_event(raw_event, error)
        return cls(
            DeliveryTarget(
                conversation_id=conversation_id,
                issue_repository=repository,
                issue_number=number,
                issue_title=title.strip(),
                delivery_role=role,
                parent_issue=parent,
                allow_create=allow_create,
                recovery_mode=recovery_mode,
                source=DeliverySource(source_repository, branch),
            ),
            dict(raw_event),
        )

    @staticmethod
    def _validate_event(value: dict[str, Any], error: DeliveryContractError) -> None:
        delivery_guid = value["deliveryGuid"]
        if delivery_guid is not None and (
            not isinstance(delivery_guid, str)
            or not delivery_guid
            or len(delivery_guid.encode()) > 128
        ):
            raise error
        for key, maximum in (
            ("eventKey", 512), ("event", 64), ("action", 64), ("actor", 128),
        ):
            item = value[key]
            if not isinstance(item, str) or not item.strip() or len(item.encode()) > maximum:
                raise error
        if (
            value["schemaVersion"] != EVENT_SCHEMA_VERSION
            or not isinstance(value["repository"], str)
            or _REPOSITORY.fullmatch(value["repository"]) is None
            or not isinstance(value["resourceUrl"], str)
            or not value["resourceUrl"].startswith("https://github.com/")
            or len(value["resourceUrl"].encode()) > 2048
            or type(value["resourceNumber"]) is not int
            or value["resourceNumber"] < 1
            or type(value["installationId"]) is not int
            or value["installationId"] < 1
            or not isinstance(value["payloadDigest"], str)
            or _DIGEST.fullmatch(value["payloadDigest"]) is None
            or not isinstance(value["observedAt"], str)
        ):
            raise error
        try:
            observed = datetime.fromisoformat(value["observedAt"].replace("Z", "+00:00"))
        except ValueError:
            raise error from None
        if observed.tzinfo is None:
            raise error
