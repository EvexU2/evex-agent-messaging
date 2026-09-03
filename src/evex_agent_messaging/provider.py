"""Small OpenHands adapter for exact-target Discussion messages."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import hmac
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.request
import uuid

from .capability import capability_token, issue_capability_token, valid_native_id
from .delivery import MainDeliveryRequest, WORKSPACE_REPOSITORY


# Exact Conversation responses include growing usage statistics, not only identity tags.
# Keep a finite transport budget independent of the much smaller outgoing message limit.
_MAX_RESPONSE_BYTES = 1024 * 1024
_DISCUSSION_ROLES = {"issue", "subissue", "spec", "project-chat", "specialist"}
_CHECKOUT_LOCKS = tuple(threading.RLock() for _ in range(64))
_SPEC_REASONING = "high"
_SPEC_SKILL = "evex-delivery-spec"
_SUPPORTED_AGENT_KINDS = {"acp", "openhands"}
_WORKSPACE_REPOSITORY = "EvexU2/evex-u-workspace"
_MESSAGING_ADMISSION = re.compile(r"v3:messaging:[0-9a-f]{64}")
_ADMISSION_CAPABILITY = "evex_delivery_admission_v1"
_CURRENT_AGENT_CONFIG_MARKER_VERSION = "v3"
_CURRENT_AGENT_CONFIG_MARKER_CONTEXT = "evex-agent-config:v3"
_MAIN_MAX_ITERATIONS = 500
_WAKEABLE_EXECUTION_STATUSES = {"idle", "paused", "finished", "error", "stuck"}
_DELIVERY_LOCKS = tuple(threading.Lock() for _ in range(64))
_AUTOMATION_NAMESPACE = uuid.UUID("d6179e10-dc5d-4168-a6f8-cd398d55d9e8")
_SPECIALIST_TITLE_TYPES = {
    "plan": "Plan",
    "plan-review": "Plan Review",
    "project-review": "Review",
    "qa": "QA",
    "code-review": "Code Review",
    "spec-review": "Spec Review",
    "writer": "Writer",
}


def _repository_short_name(repository: str) -> str:
    name = repository.rsplit("/", 1)[-1]
    for prefix in ("evex-agent-", "evex-u-"):
        if name.startswith(prefix):
            return name.removeprefix(prefix)
    return name


def _issue_number(issue_ref: object) -> str | None:
    if not isinstance(issue_ref, str) or "#" not in issue_ref:
        return None
    number = issue_ref.rsplit("#", 1)[1]
    return number if number.isdigit() and int(number) > 0 else None


def _specialist_title(
    parent_role: str,
    parent_tags: dict[str, Any],
    agent_type: str,
    description: str,
) -> str:
    title_type = _SPECIALIST_TITLE_TYPES.get(agent_type)
    if title_type is None:
        raise ProviderError("Specialist title type is invalid")
    if parent_role == "project-chat":
        if agent_type != "project-review":
            raise ProviderError("Project Specialist title type is invalid")
        return f"Projekt · Review · {description}"

    root_number = _issue_number(parent_tags.get("evexparentissue"))
    direct_number = _issue_number(parent_tags.get("evexissue"))
    if root_number is not None:
        repository = parent_tags.get("evexsourcerepository")
        if direct_number is None or not isinstance(repository, str) or not repository:
            raise ProviderError("Source Specialist title identity is invalid")
        return (
            f"#{root_number} / #{direct_number} · {_repository_short_name(repository)} · "
            f"{title_type} · {description}"
        )
    if direct_number is None:
        raise ProviderError("Root Specialist title identity is invalid")
    return f"#{direct_number} · {title_type} · {description}"


class ProviderError(RuntimeError):
    _DELIVERY_REASONS = {
        "target_busy",
        "runtime_unavailable",
        "target_not_wakeable",
        "target_identity_mismatch",
    }

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason if reason in self._DELIVERY_REASONS else None


@dataclass(frozen=True)
class _DeliveryResponse:
    status: int
    body: dict[str, Any]


@dataclass
class OpenHandsProvider:
    base_url: str
    api_key: str
    timeout: float = 5.0
    transport: Callable[[str, str, dict | None], dict] | None = None
    public_url: str = ""
    workspace_root: str = "/home/openhands/workspace/delivery"
    admission_key: bytes = b""
    messaging_secret: bytes = b""
    delivery_budget: float = 5.0
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> dict:
        if self.transport is not None:
            value = self.transport(method, path, body)
            if not isinstance(value, dict):
                raise ProviderError("OpenHands returned an invalid response")
            return value
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=json.dumps(body, separators=(",", ":")).encode() if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json", "X-Session-API-Key": self.api_key},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise ProviderError("OpenHands messaging transport failed", status=exc.code) from exc
        except (OSError, http.client.IncompleteRead) as exc:
            raise ProviderError("OpenHands messaging transport failed") from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ProviderError("OpenHands response exceeds bounded byte budget")
        try:
            value = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise ProviderError("OpenHands returned an invalid response") from exc
        if not isinstance(value, dict):
            raise ProviderError("OpenHands returned an invalid response")
        return value

    def readiness(self) -> bool:
        if (
            not self.base_url.strip()
            or not self.api_key.strip()
            or not self.public_url.strip()
            or len(self.admission_key.strip()) < 32
        ):
            return False
        try:
            value = self._request("GET", "/api/agent-profiles")
        except ProviderError:
            return False
        try:
            self._selected_profile(value)
        except ProviderError:
            return False
        return True

    def deliver_main(self, request: MainDeliveryRequest) -> dict[str, Any]:
        """Create or wake one exact Main; OpenHands remains private to this adapter."""
        target = request.target
        lock = _DELIVERY_LOCKS[target.conversation_id.int % len(_DELIVERY_LOCKS)]
        if not lock.acquire(timeout=min(self.delivery_budget, 1.0)):
            raise ProviderError(
                "Main already has an in-flight delivery", reason="target_busy"
            )
        try:
            return self._deliver_main_once(request)
        finally:
            lock.release()

    def _deliver_main_once(self, request: MainDeliveryRequest) -> dict[str, Any]:
        target = request.target
        deadline = self.clock() + self.delivery_budget
        path = f"/api/conversations/{target.conversation_id}"
        current = self._delivery_request("GET", path, None, deadline)
        if current.status == 404:
            if not target.allow_create:
                return {
                    "accepted": False,
                    "reason": "target_missing_not_intake_authorized",
                }
            created, reconciled = self._create_main(request, deadline)
            if not created:
                current = reconciled or self._delivery_request("GET", path, None, deadline)
                if reconciled is None:
                    self._verify_main_identity(request, current)
        elif current.status == 200:
            self._verify_main_identity(request, current)
            created = False
        else:
            raise ProviderError(
                "Main lookup failed", reason="runtime_unavailable"
            )

        if not created:
            self._verify_main_wakeable(current)
        accepted = self._delivery_request(
            "POST",
            f"{path}/events",
            {
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        self._main_bootstrap_text(request)
                        if created
                        else self._main_event_text(request.event)
                    ),
                }],
                "run": True,
            },
            deadline,
        )
        if accepted.status not in {200, 201, 202, 204}:
            raise ProviderError(
                "Main event was not accepted", reason="runtime_unavailable"
            )
        return {
            "accepted": True,
            "conversationId": str(target.conversation_id),
            "outcome": "created" if created else "woken",
        }

    def _create_main(
        self, request: MainDeliveryRequest, deadline: float
    ) -> tuple[bool, _DeliveryResponse | None]:
        target = request.target
        self._require_main_admission_capability(deadline)
        profiles = self._delivery_request("GET", "/api/agent-profiles", None, deadline)
        if profiles.status != 200:
            raise ProviderError("Agent Profile lookup failed", reason="runtime_unavailable")
        profile_id = self._selected_profile(profiles.body)
        capability_ref = self._main_delivery_capability(request)
        if not capability_ref:
            raise ProviderError("Messaging capability is unavailable", reason="runtime_unavailable")
        workspace = self._main_workspace(request)
        tags = {**self._main_tags(request), "evexagentprofile": profile_id}
        descriptor = self._admission_descriptor(
            target.conversation_id,
            profile_id,
            workspace["working_dir"],
            tags,
        )
        secrets = self._main_secrets(request, capability_ref)
        secrets["EVEX_DELIVERY_ADMISSION"] = {
            "kind": "StaticSecret",
            "value": self._admission_token(descriptor),
        }
        payload = {
            "workspace": workspace,
            "worktree": False,
            "conversation_id": str(target.conversation_id),
            "agent_profile_id": profile_id,
            "tags": tags,
            "max_iterations": _MAIN_MAX_ITERATIONS,
            "stuck_detection": True,
            "autotitle": False,
            "secrets": secrets,
        }
        try:
            response = self._delivery_request(
                "POST", "/api/conversations", payload, deadline
            )
        except ProviderError as mutation_error:
            # Never replay an uncertain mutation. One exact safe read may establish
            # that the deterministic target now exists with the intended identity.
            current = self._delivery_request(
                "GET", f"/api/conversations/{target.conversation_id}", None, deadline
            )
            if current.status != 200:
                raise mutation_error
            try:
                self._verify_main_identity(
                    request, current, expected_profile_id=profile_id
                )
            except ProviderError:
                current = self._reconcile_missing_main_admission(
                    request, current, profile_id, secrets, deadline
                )
            self._update_main_title(request, deadline)
            return True, current
        if response.status == 409:
            current = self._delivery_request(
                "GET", f"/api/conversations/{target.conversation_id}", None, deadline
            )
            try:
                self._verify_main_identity(
                    request, current, expected_profile_id=profile_id
                )
                return False, current
            except ProviderError:
                current = self._reconcile_missing_main_admission(
                    request, current, profile_id, secrets, deadline
                )
                self._update_main_title(request, deadline)
                return True, current
        if response.status not in {200, 201}:
            raise ProviderError("Main creation failed", reason="runtime_unavailable")

        path = f"/api/conversations/{target.conversation_id}"
        self._update_main_title(request, deadline)
        verified = self._delivery_request("GET", path, None, deadline)
        self._verify_main_identity(request, verified, expected_profile_id=profile_id)
        return True, None

    def _reconcile_missing_main_admission(
        self,
        request: MainDeliveryRequest,
        current: _DeliveryResponse,
        profile_id: str,
        secrets: dict[str, dict[str, str]],
        deadline: float,
    ) -> _DeliveryResponse:
        target = request.target
        expected_tags = {**self._main_tags(request), "evexagentprofile": profile_id}
        tags = current.body.get("tags")
        launched = current.body.get("launched_agent_profile")
        if (
            current.status != 200
            or current.body.get("id") != str(target.conversation_id)
            or not isinstance(tags, dict)
            or "evexadmission" in tags
            or tags != expected_tags
            or current.body.get("workspace") != self._main_workspace(request)
            or not isinstance(launched, dict)
            or launched.get("agent_profile_id") != profile_id
        ):
            raise ProviderError(
                "Main identity mismatch", reason="target_identity_mismatch"
            )
        path = f"/api/conversations/{target.conversation_id}"
        repair = {
            "secrets": {
                key: secrets[key]
                for key in (
                    "EVEX_AGENT_MESSAGING_CAPABILITY",
                    "EVEX_DELIVERY_ADMISSION",
                )
            }
        }
        try:
            written = self._delivery_request("POST", f"{path}/secrets", repair, deadline)
            if written.status not in {200, 204}:
                raise ProviderError(
                    "Main admission repair failed", reason="target_identity_mismatch"
                )
        except ProviderError as mutation_error:
            verified = self._delivery_request("GET", path, None, deadline)
            try:
                self._verify_main_identity(
                    request, verified, expected_profile_id=profile_id
                )
            except ProviderError:
                raise mutation_error
            return verified
        verified = self._delivery_request("GET", path, None, deadline)
        self._verify_main_identity(request, verified, expected_profile_id=profile_id)
        return verified

    def _update_main_title(
        self, request: MainDeliveryRequest, deadline: float
    ) -> None:
        patched = self._delivery_request(
            "PATCH",
            f"/api/conversations/{request.target.conversation_id}",
            {"title": self._main_title(request)},
            deadline,
        )
        if patched.status not in {200, 204}:
            raise ProviderError("Main title update failed", reason="runtime_unavailable")

    def _delivery_request(
        self,
        method: str,
        path: str,
        body: dict | None,
        deadline: float,
    ) -> _DeliveryResponse:
        response: _DeliveryResponse | None = None
        for attempt, delay in enumerate((0.0, 0.1, 0.25)):
            if attempt:
                self.sleeper(delay)
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ProviderError("Delivery budget exhausted", reason="runtime_unavailable")
            try:
                value = self._request(
                    method, path, body, timeout=min(remaining, self.timeout)
                )
                response = _DeliveryResponse(200, value)
            except ProviderError as exc:
                if exc.status is None:
                    raise ProviderError(
                        "OpenHands transport failed", reason="runtime_unavailable"
                    ) from exc
                response = _DeliveryResponse(exc.status, {})
            if method != "GET" or response.status not in {502, 503, 504}:
                return response
        assert response is not None
        return response

    def _require_main_admission_capability(self, deadline: float) -> None:
        response = self._delivery_request("GET", "/server_info", None, deadline)
        capabilities = response.body.get("capabilities")
        if (
            response.status != 200
            or not isinstance(capabilities, list)
            or _ADMISSION_CAPABILITY not in capabilities
        ):
            raise ProviderError(
                "Delivery admission capability is unavailable",
                reason="runtime_unavailable",
            )

    def _main_delivery_capability(self, request: MainDeliveryRequest) -> str:
        target = request.target
        if not self.messaging_secret:
            return ""
        if target.delivery_role == "issue":
            return issue_capability_token(self.messaging_secret, target.conversation_id)
        assert target.parent_issue is not None
        owner = uuid.uuid5(
            _AUTOMATION_NAMESPACE,
            f"{WORKSPACE_REPOSITORY}#issue-{target.parent_issue}:main",
        )
        return capability_token(
            self.messaging_secret,
            owning_issue_id=owner,
            sender_id=target.conversation_id,
            task_key=f"issue-{target.issue_number}",
            role="subissue",
        )

    @staticmethod
    def _main_tags(request: MainDeliveryRequest) -> dict[str, str]:
        target = request.target
        skill = (
            "evex-delivery-issue"
            if target.delivery_role == "issue"
            else "evex-delivery-subissue"
        )
        tags = {
            "project": "evex-u",
            "evexrole": "main",
            "evexdeliveryrole": target.delivery_role,
            "evexskills": skill,
            "evextask": f"issue-{target.issue_number}",
            "evexissue": f"{target.issue_repository}#{target.issue_number}",
            "evexsourcerepository": target.source.repository,
            "evexsourcebranch": target.source.branch,
        }
        if target.delivery_role == "subissue":
            tags["evexparentissue"] = (
                f"{WORKSPACE_REPOSITORY}#{target.parent_issue}"
            )
        return tags

    def _main_workspace(self, request: MainDeliveryRequest) -> dict[str, str]:
        target = request.target
        repository_name = target.source.repository.split("/", 1)[1]
        return {
            "working_dir": (
                f"{self.workspace_root.rstrip('/')}/issue-{target.issue_number}-source/"
                f"{repository_name}"
            )
        }

    def _main_secrets(
        self, request: MainDeliveryRequest, capability_ref: str
    ) -> dict[str, dict[str, str]]:
        target = request.target
        return {
            "EVEX_AGENT_MESSAGING_CAPABILITY": {
                "kind": "StaticSecret", "value": capability_ref,
            },
            "EVEX_AGENT_ROLE": {
                "kind": "StaticSecret",
                "value": target.delivery_role,
            },
            "EVEX_AGENT_INSTANCE_ID": {
                "kind": "StaticSecret", "value": str(target.conversation_id),
            },
            "EVEX_REASONING_EFFORT": {"kind": "StaticSecret", "value": "medium"},
            "EVEX_SOURCE_REPOSITORY": {
                "kind": "StaticSecret", "value": target.source.repository,
            },
            "EVEX_SOURCE_BRANCH": {
                "kind": "StaticSecret", "value": target.source.branch,
            },
            "EVEX_SOURCE_CHECKOUT": {
                "kind": "StaticSecret",
                "value": self._main_workspace(request)["working_dir"],
            },
        }

    def _verify_main_identity(
        self,
        request: MainDeliveryRequest,
        response: _DeliveryResponse,
        *,
        expected_profile_id: str | None = None,
    ) -> None:
        target = request.target
        required = self._main_tags(request)
        tags = response.body.get("tags")
        workspace = response.body.get("workspace")
        unexpected = (
            {
                str(key) for key in tags
                if str(key).startswith("evex")
                and key not in required
                and key not in {"evexagentprofile", "evexadmission"}
            }
            if isinstance(tags, dict) else set()
        )
        if (
            response.status != 200
            or response.body.get("id") != str(target.conversation_id)
            or not isinstance(tags, dict)
            or any(tags.get(key) != value for key, value in required.items())
            or unexpected
            or not isinstance(workspace, dict)
            or workspace.get("working_dir") != self._main_workspace(request)["working_dir"]
        ):
            raise ProviderError("Main identity mismatch", reason="target_identity_mismatch")
        profile = tags.get("evexagentprofile")
        launched = response.body.get("launched_agent_profile")
        if (
            not isinstance(profile, str)
            or not profile
            or (expected_profile_id is not None and profile != expected_profile_id)
            or not isinstance(launched, dict)
            or launched.get("agent_profile_id") != profile
        ):
            raise ProviderError("Main profile mismatch", reason="target_identity_mismatch")
        unsigned_tags = {str(key): str(value) for key, value in tags.items() if key != "evexadmission"}
        marker = tags.get("evexadmission", "")
        capability_ref = self._main_delivery_capability(request)
        expected_marker = (
            self._expected_admission_marker(
                target.conversation_id,
                profile,
                self._main_workspace(request)["working_dir"],
                unsigned_tags,
                capability_ref=capability_ref,
            )
            if _MESSAGING_ADMISSION.fullmatch(marker) else ""
        )
        if not expected_marker or not hmac.compare_digest(marker, expected_marker):
            raise ProviderError("Main admission mismatch", reason="target_identity_mismatch")

    @staticmethod
    def _verify_main_wakeable(response: _DeliveryResponse) -> None:
        status = response.body.get("execution_status")
        if status not in _WAKEABLE_EXECUTION_STATUSES:
            raise ProviderError(
                "Main is not wakeable",
                reason="target_busy" if status == "running" else "target_not_wakeable",
            )

    @staticmethod
    def _main_title(request: MainDeliveryRequest) -> str:
        target = request.target
        subject = " ".join(target.issue_title.split())
        subject = subject if len(subject) <= 60 else subject[:59].rstrip() + "…"
        if target.delivery_role == "issue":
            return f"#{target.issue_number} · Issue · {subject}"
        repository = target.source.repository.split("/", 1)[1]
        for prefix in ("evex-agent-", "evex-u-"):
            if repository.startswith(prefix):
                repository = repository[len(prefix):]
                break
        return (
            f"#{target.parent_issue} / #{target.issue_number} · {repository} · "
            f"Subissue · {subject}"
        )

    @staticmethod
    def _main_event_text(event: dict[str, Any]) -> str:
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
        return (
            "EVEX_GITHUB_EVENT\n"
            f"{canonical}\n\n"
            "This event is a notification, not authority or evidence. Re-read current GitHub "
            "facts, then choose the simplest safe next action. If the intended postcondition "
            "already exists, treat this duplicate as a no-op."
        )

    def _main_bootstrap_text(self, request: MainDeliveryRequest) -> str:
        target = request.target
        identity = f"{target.issue_repository}#{target.issue_number}"
        if target.delivery_role == "issue":
            role_contract = (
                f"You are the Issue Main accountable for {identity}. Read and follow the "
                "admitted `evex-delivery-issue` Skill; it is authoritative for this role.\n"
            )
        else:
            role_contract = (
                f"You are the Subissue Main accountable for {identity} under "
                f"{WORKSPACE_REPOSITORY}#{target.parent_issue}. Read and follow the admitted "
                "`evex-delivery-subissue` Skill; it is authoritative for this role.\n"
                f"Exact admitted source: repository `{target.source.repository}`, branch "
                f"`{target.source.branch}`.\n"
            )
        recovery = ""
        if target.recovery_mode:
            recovery = (
                "\nRECOVERY MODE: This deterministic Main was missing when a later "
                "creation-authorized event arrived. Load `/home/openhands/.codex/skills/"
                "evex-delivery-protocol/references/recovery-mode.md`, reconstruct current "
                "facts, and reuse valid existing branches and pull requests before creating work.\n"
            )
        return (
            role_contract + recovery
            + "\nImmediate task: re-read current facts for the Issue and linked resources, "
            "state privately the objective and smallest useful next action, then act or delegate.\n"
            f"- Issue: https://github.com/{target.issue_repository}/issues/{target.issue_number}\n"
            f"- Deterministic Main ID: `{target.conversation_id}`\n"
            f"- This Main: {self.public_url.rstrip('/')}/conversations/{target.conversation_id}\n\n"
            + self._main_event_text(request.event)
        )

    def provisioning_allowed(self, credential: str | None) -> bool:
        # This existing service credential authenticates the trigger, never the PM.
        return (
            isinstance(credential, str) and bool(self.api_key.strip())
            and hmac.compare_digest(credential.encode(), self.api_key.encode())
        )

    def project_binding(self, conversation_id: uuid.UUID) -> str:
        value = self._request("GET", f"/api/conversations/{conversation_id}")
        admission = self._project_admission(value, conversation_id)
        if admission["role"] != "project":
            raise ProviderError("Project capability admission is unavailable or invalid")
        return admission["project"]["id"]

    def install_project_capability(
        self, conversation_id: uuid.UUID, project_id: str, capability_ref: str,
    ) -> dict[str, Any]:
        # The host must revalidate admission and compare live/durable state under its
        # existing secrets lock. No client-side comparison, receipt, or retry is safe.
        response = self._request("POST", f"/api/conversations/{conversation_id}/secrets", {
            "secrets": {"EVEX_AGENT_MESSAGING_CAPABILITY": {
                "kind": "StaticSecret", "value": capability_ref,
            }},
        })
        binding = response.get("evexProjectCapability")
        if (
            set(response) != {"success", "evexProjectCapability"} or response["success"] is not True
            or not isinstance(binding, dict)
            or set(binding) != {"schemaVersion", "conversationId", "projectId", "bindingVerified"}
            or type(binding["schemaVersion"]) is not int or binding["schemaVersion"] != 1
            or binding["conversationId"] != str(conversation_id) or binding["projectId"] != project_id
            or binding["bindingVerified"] is not True
        ):
            raise ProviderError("Project capability binding is unverified")
        return binding

    def create_spec_chat(
        self,
        parent_id: uuid.UUID,
        spec_chat_id: uuid.UUID,
        capability_ref: str,
    ) -> dict[str, Any]:
        parent_value = self._request("GET", f"/api/conversations/{parent_id}")
        parent_identity, parent_tags, parent_role = self._identity(parent_value)
        issue_ref = parent_tags.get("evexissue")
        if (
            parent_identity != parent_id
            or parent_role != "issue"
            or not isinstance(issue_ref, str)
            or "#" not in issue_ref
        ):
            raise ProviderError("Issue Conversation identity is invalid")
        issue_repository, issue_number = issue_ref.rsplit("#", 1)
        if issue_repository != _WORKSPACE_REPOSITORY or not issue_number.isdigit():
            raise ProviderError("Issue Conversation root identity is invalid")
        branch = f"spec/issue-{issue_number}"
        parent_checkout, parent_head = self._validated_parent_checkout(
            parent_value, parent_tags, issue_number
        )

        lock = _CHECKOUT_LOCKS[spec_chat_id.int % len(_CHECKOUT_LOCKS)]
        with lock:
            return self._create_spec_chat_locked(
                parent_id,
                spec_chat_id,
                issue_ref,
                issue_number,
                issue_repository,
                branch,
                parent_checkout,
                parent_head,
                capability_ref,
            )

    def start_specialist(
        self,
        parent_id: uuid.UUID,
        specialist_id: uuid.UUID,
        capability_ref: str,
        mission: dict[str, Any],
    ) -> dict[str, Any]:
        required = {
            "missionKey", "missionId", "prompt", "promptDigest", "agentType",
            "description", "skills", "reasoning", "descriptorDigest", "parentRole",
        }
        if set(mission) != required:
            raise ProviderError("Specialist Mission is invalid")
        parent = self._request("GET", f"/api/conversations/{parent_id}")
        identity, parent_tags, parent_role = self._identity(parent)
        expected_parent_role = {
            "issue": "issue",
            "subissue": "subissue",
            "spec": "spec",
            "project": "project-chat",
            "specialist": "specialist",
        }.get(mission["parentRole"])
        profile = parent.get("launched_agent_profile")
        profile_id = profile.get("agent_profile_id") if isinstance(profile, dict) else None
        workspace = parent.get("workspace")
        working_dir = workspace.get("working_dir") if isinstance(workspace, dict) else None
        if (
            identity != parent_id
            or parent_role != expected_parent_role
            or not isinstance(profile_id, str)
            or not profile_id
            or parent_tags.get("evexagentprofile") != profile_id
            or not isinstance(working_dir, str)
            or not working_dir
        ):
            raise ProviderError("Specialist Owner identity is invalid")

        tags = {
            key: value
            for key, value in parent_tags.items()
            if key in {
                "project", "evextask", "evexissue", "evexparentissue", "evexsourcerepository",
                "evexsourcebranch", "evexrepository", "evexbranch",
            }
        }
        tags.update({
            "evexrole": str(mission["agentType"]),
            "evexdeliveryrole": "specialist",
            "evexagenttype": str(mission["agentType"]),
            "evexparent": str(parent_id),
            "evexskills": ",".join(mission["skills"]),
            "evexdescription": str(mission["description"]),
            "evexagentprofile": profile_id,
            "evexreasoning": str(mission["reasoning"]),
            "evexmission": str(mission["missionId"]),
            "evexprompt": str(mission["promptDigest"]),
            "evexmissionconfig": str(mission["descriptorDigest"]),
        })
        descriptor = self._admission_descriptor(
            specialist_id,
            profile_id,
            working_dir,
            tags,
            parent_id=parent_id,
        )
        title = _specialist_title(
            parent_role,
            parent_tags,
            str(mission["agentType"]),
            str(mission["description"]),
        )
        payload = {
            "conversation_id": str(specialist_id),
            "workspace": workspace,
            "worktree": False,
            "parent_conversation_id": str(parent_id),
            "agent_profile_id": profile_id,
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": str(mission["prompt"])}],
                "run": True,
            },
            "tags": tags,
            "secrets": {
                "EVEX_DELIVERY_ADMISSION": {
                    "kind": "StaticSecret",
                    "value": self._admission_token(descriptor),
                },
                "EVEX_AGENT_MESSAGING_CAPABILITY": {
                    "kind": "StaticSecret",
                    "value": capability_ref,
                },
            },
            "autotitle": False,
            "max_iterations": 300,
        }
        created = False
        try:
            existing = self._request("GET", f"/api/conversations/{specialist_id}")
        except ProviderError as exc:
            if exc.status != 404:
                raise
            try:
                self._request("POST", "/api/conversations", payload)
            except ProviderError as create_error:
                try:
                    self._request("GET", f"/api/conversations/{specialist_id}")
                except ProviderError:
                    raise create_error
            else:
                self._set_title(specialist_id, title)
                created = True
            existing = self._request("GET", f"/api/conversations/{specialist_id}")

        self._validate_existing_specialist(
            existing,
            parent_id,
            specialist_id,
            profile_id,
            workspace,
            tags,
            capability_ref,
        )
        if not created:
            self._request(
                "POST",
                f"/api/conversations/{specialist_id}/secrets",
                {"secrets": {"EVEX_AGENT_MESSAGING_CAPABILITY": {
                    "kind": "StaticSecret",
                    "value": capability_ref,
                }}},
            )
        return {
            "conversationUrl": f"{self.public_url.rstrip('/')}/conversations/{specialist_id}",
            "provider": "openhands",
            "created": created,
            "status": str(existing.get("execution_status") or "running"),
        }

    def _validate_existing_specialist(
        self,
        value: dict[str, Any],
        parent_id: uuid.UUID,
        specialist_id: uuid.UUID,
        profile_id: str,
        workspace: dict[str, Any],
        expected_tags: dict[str, str],
        capability_ref: str,
    ) -> None:
        identity, tags, role = self._identity(value)
        actual_profile = value.get("launched_agent_profile")
        actual_profile_id = (
            actual_profile.get("agent_profile_id")
            if isinstance(actual_profile, dict)
            else None
        )
        unsigned_tags = {key: item for key, item in tags.items() if key != "evexadmission"}
        marker = tags.get("evexadmission", "")
        expected_marker = self._expected_admission_marker(
            specialist_id,
            profile_id,
            str(workspace["working_dir"]),
            expected_tags,
            capability_ref=capability_ref,
            parent_id=parent_id,
        )
        if (
            identity != specialist_id
            or role != "specialist"
            or value.get("parent_conversation_id") != str(parent_id)
            or value.get("workspace") != workspace
            or actual_profile_id != profile_id
            or unsigned_tags != expected_tags
            or not isinstance(marker, str)
            or not hmac.compare_digest(marker, expected_marker)
        ):
            raise ProviderError("Existing Specialist does not match authority")

    def _create_spec_chat_locked(
        self,
        parent_id: uuid.UUID,
        spec_chat_id: uuid.UUID,
        issue_ref: str,
        issue_number: str,
        repository: str,
        branch: str,
        parent_checkout: Path,
        parent_head: str,
        capability_ref: str,
    ) -> dict[str, Any]:
        checkout = {
            "repository": repository,
            "branch": branch,
            "headSha": parent_head,
        }
        prompt_identity = (
            "EVEX_SPEC_CHAT\n"
            f"Issue: https://github.com/{issue_ref.replace('#', '/issues/')}\n"
            f"Issue Conversation: {parent_id}\n"
        )
        prompt: str | None = None
        created = False
        create_confirmed = False
        try:
            existing = self._request("GET", f"/api/conversations/{spec_chat_id}")
        except ProviderError as exc:
            if exc.status != 404:
                raise
            checkout["headSha"] = self._ensure_checkout(
                spec_chat_id, checkout, parent_checkout
            )
            self._require_admission_capability()
            profiles = self._request("GET", "/api/agent-profiles")
            profile_id = self._selected_profile(profiles)
            prompt = self._spec_prompt(
                prompt_identity, self._agent_kind(profiles, profile_id)
            )
            workspace = {"working_dir": str(self._checkout_path(spec_chat_id))}
            tags = self._spec_tags(
                parent_id, issue_ref, issue_number, checkout, profile_id
            )
            descriptor = self._admission_descriptor(
                spec_chat_id, profile_id, workspace["working_dir"], tags
            )
            title = f"#{issue_number} · Spezifikation"
            payload = {
                "conversation_id": str(spec_chat_id),
                "agent_profile_id": profile_id,
                "workspace": workspace,
                "worktree": False,
                "tags": tags,
                "autotitle": False,
                "max_iterations": 300,
                "secrets": {
                    "EVEX_AGENT_INSTANCE_ID": {
                        "kind": "StaticSecret",
                        "value": str(spec_chat_id),
                    },
                    "EVEX_AGENT_MESSAGING_CAPABILITY": {
                        "kind": "StaticSecret",
                        "value": capability_ref,
                    },
                    "EVEX_DELIVERY_ADMISSION": {
                        "kind": "StaticSecret",
                        "value": self._admission_token(descriptor),
                    },
                },
            }
            try:
                self._request("POST", "/api/conversations", payload)
            except ProviderError as create_error:
                try:
                    self._request("GET", f"/api/conversations/{spec_chat_id}")
                except ProviderError:
                    raise create_error
                created = create_error.status != 409
                prompt = None
            else:
                self._set_title(spec_chat_id, title)
                created = True
                create_confirmed = True
            existing = None

        verified = self._request("GET", f"/api/conversations/{spec_chat_id}")
        self._validate_existing_spec(
            verified,
            parent_id,
            spec_chat_id,
            issue_ref,
            issue_number,
            checkout,
            profile_id if created else None,
            capability_ref,
        )
        observed_head = self._validate_existing_checkout(
            self._checkout_path(spec_chat_id), checkout, exact=False
        )
        if not created:
            self._request(
                "POST",
                f"/api/conversations/{spec_chat_id}/secrets",
                {"secrets": {"EVEX_AGENT_MESSAGING_CAPABILITY": {
                    "kind": "StaticSecret",
                    "value": capability_ref,
                }}},
            )
        if not create_confirmed and not self._has_initial_prompt(
            spec_chat_id, prompt_identity
        ):
            launched = verified.get("launched_agent_profile")
            launched_profile_id = (
                launched.get("agent_profile_id") if isinstance(launched, dict) else None
            )
            profiles = self._request("GET", "/api/agent-profiles")
            prompt = self._spec_prompt(
                prompt_identity,
                self._agent_kind(profiles, launched_profile_id),
            )
        if prompt is not None:
            try:
                self._request(
                    "POST",
                    f"/api/conversations/{spec_chat_id}/events",
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": prompt}],
                        "run": True,
                    },
                )
            except ProviderError as prompt_error:
                if not self._has_initial_prompt(spec_chat_id, prompt_identity):
                    raise prompt_error
        return {
            "conversationUrl": (
                f"{self.public_url.rstrip('/')}/conversations/{spec_chat_id}"
            ),
            "provider": "openhands",
            "created": created,
            "checkout": {
                "repository": repository,
                "branch": branch,
                "headSha": observed_head,
            },
        }

    def _set_title(self, conversation_id: uuid.UUID, title: str) -> None:
        result = self._request(
            "PATCH",
            f"/api/conversations/{conversation_id}",
            {"title": title},
        )
        if result.get("success") is False:
            raise ProviderError("OpenHands Conversation title update was rejected")

    def _admission_token(self, descriptor: dict[str, Any]) -> str:
        admission_key = self.admission_key.strip()
        if len(admission_key) < 32:
            raise ProviderError("OpenHands delivery admission signer is unavailable")
        canonical = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
        message = f"evex-delivery-admission:v1\0messaging\0{canonical}".encode()
        signature = hmac.new(admission_key, message, hashlib.sha256).hexdigest()
        return f"v1:messaging:{signature}"

    @staticmethod
    def _admission_descriptor(
        spec_chat_id: uuid.UUID,
        profile_id: str,
        working_dir: str,
        tags: dict[str, str],
        *,
        parent_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        return {
            "conversation_id": str(spec_chat_id),
            "parent_conversation_id": str(parent_id) if parent_id is not None else "",
            "profile_id": profile_id,
            "working_dir": working_dir,
            "worktree": False,
            "tags": tags,
        }

    def _expected_admission_marker(
        self,
        spec_chat_id: uuid.UUID,
        profile_id: str,
        working_dir: str,
        tags: dict[str, str],
        *,
        capability_ref: str,
        parent_id: uuid.UUID | None = None,
    ) -> str:
        token = self._admission_token(
            self._admission_descriptor(
                spec_chat_id, profile_id, working_dir, tags, parent_id=parent_id
            )
        )
        material = (
            f"{_CURRENT_AGENT_CONFIG_MARKER_CONTEXT}\0{token}"
            f"\0{capability_ref}"
        )
        digest = hashlib.sha256(material.encode()).hexdigest()
        return f"{_CURRENT_AGENT_CONFIG_MARKER_VERSION}:messaging:{digest}"

    def _require_admission_capability(self) -> None:
        info = self._request("GET", "/server_info")
        capabilities = info.get("capabilities")
        if (
            not isinstance(capabilities, list)
            or _ADMISSION_CAPABILITY not in capabilities
        ):
            raise ProviderError(
                "OpenHands delivery admission capability is unavailable"
            )

    @staticmethod
    def _spec_tags(
        parent_id: uuid.UUID,
        issue_ref: str,
        issue_number: str,
        checkout: dict[str, str],
        profile_id: str,
    ) -> dict[str, str]:
        return {
            "project": "evex-u",
            "evexrole": "spec",
            "evexdeliveryrole": "spec",
            "evexskills": _SPEC_SKILL,
            "evexagentprofile": profile_id,
            "evextask": f"issue-{issue_number}-spec",
            "evexissue": issue_ref,
            "evexparent": str(parent_id),
            "evexrepository": checkout["repository"],
            "evexbranch": checkout["branch"],
            "evexreasoning": _SPEC_REASONING,
        }

    def _validate_existing_spec(
        self,
        value: dict,
        parent_id: uuid.UUID,
        spec_chat_id: uuid.UUID,
        issue_ref: str,
        issue_number: str,
        checkout: dict[str, str],
        expected_profile_id: str | None,
        capability_ref: str,
    ) -> None:
        identity, tags, role = self._identity(value)
        workspace = value.get("workspace")
        working_dir = workspace.get("working_dir") if isinstance(workspace, dict) else None
        try:
            workspace_matches = (
                isinstance(working_dir, str)
                and Path(working_dir).resolve() == self._checkout_path(spec_chat_id)
            )
        except OSError:
            workspace_matches = False
        new_metadata = {"evexskills", "evexagentprofile"}
        present_new_metadata = new_metadata.intersection(tags)
        profile = value.get("launched_agent_profile")
        launched_profile_id = (
            profile.get("agent_profile_id") if isinstance(profile, dict) else None
        )
        bound_profile_id = tags.get("evexagentprofile")
        expected_tags = self._spec_tags(
            parent_id,
            issue_ref,
            issue_number,
            checkout,
            bound_profile_id if isinstance(bound_profile_id, str) else "",
        )
        unexpected_reserved = {
            str(key)
            for key in tags
            if str(key).startswith("evex")
            and key not in expected_tags
            and key != "evexadmission"
        }
        if (
            identity != spec_chat_id
            or role != "spec"
            or present_new_metadata != new_metadata
            or any(tags.get(key) != expected for key, expected in expected_tags.items())
            or unexpected_reserved
            or not workspace_matches
            or not isinstance(launched_profile_id, str)
            or not launched_profile_id
            or (
                launched_profile_id != tags.get("evexagentprofile")
                or (
                    expected_profile_id is not None
                    and launched_profile_id != expected_profile_id
                )
            )
        ):
            raise ProviderError("Existing Spec Chat does not match authority")
        marker = tags.get("evexadmission", "")
        unsigned_tags = {
            str(key): str(item)
            for key, item in tags.items()
            if key != "evexadmission"
        }
        marker_match = _MESSAGING_ADMISSION.fullmatch(marker)
        expected_marker = (
            self._expected_admission_marker(
                spec_chat_id,
                str(launched_profile_id),
                str(working_dir),
                unsigned_tags,
                capability_ref=capability_ref,
            )
            if marker_match is not None
            else ""
        )
        if marker_match is None or not hmac.compare_digest(marker, expected_marker):
            raise ProviderError("Existing Spec Chat admission does not match authority")

    @staticmethod
    def _selected_profile(profiles: dict) -> str:
        profile_id = profiles.get("active_agent_profile_id")
        available = {
            item.get("id"): item
            for item in profiles.get("profiles", [])
            if isinstance(item, dict)
        }
        if (
            not isinstance(profile_id, str)
            or not profile_id
            or available.get(profile_id, {}).get("agent_kind")
            not in _SUPPORTED_AGENT_KINDS
        ):
            raise ProviderError("OpenHands has no supported active Agent Profile")
        try:
            if str(uuid.UUID(profile_id)) != profile_id:
                raise ValueError("non-canonical UUID")
        except ValueError as exc:
            raise ProviderError("OpenHands has no supported active Agent Profile") from exc
        return profile_id

    @staticmethod
    def _agent_kind(profiles: dict, profile_id: object) -> str:
        available = {
            item.get("id"): item
            for item in profiles.get("profiles", [])
            if isinstance(item, dict)
        }
        kind = (
            available.get(profile_id, {}).get("agent_kind")
            if isinstance(profile_id, str)
            else None
        )
        if kind not in _SUPPORTED_AGENT_KINDS:
            raise ProviderError("OpenHands has no supported Agent Profile kind")
        return kind

    @staticmethod
    def _spec_prompt(identity: str, agent_kind: str) -> str:
        if agent_kind == "acp":
            activation = (
                "First load the admitted `evex-delivery-spec` package from its registered runtime "
                "skill location and follow its EVEX references and skills. Do not call "
                "`invoke_skill`; that entry point is not exposed by this ACP runtime."
            )
        elif agent_kind == "openhands":
            activation = (
                'First call invoke_skill(name="evex-delivery-spec") and follow its '
                "runtime-installed EVEX references and skills."
            )
        else:
            raise ProviderError("OpenHands has no supported Agent Profile kind")
        identity_lines = identity.splitlines()
        if len(identity_lines) != 3 or identity_lines[0] != "EVEX_SPEC_CHAT":
            raise ProviderError("OpenHands Spec prompt identity is invalid")
        return (
            "EVEX_SPEC_CHAT\n"
            "Du bist Eve. Antworte in jeder menschenlesbaren Ausgabe auf Deutsch, freundlich, "
            "motivierend und nicht-technisch; dauerhafte Artefakte bleiben auf Englisch.\n"
            "Your task now: run the interactive Spec Chat for the Issue identified below. "
            + activation
            + " Never load skills from the source checkout or search it for skill-support "
            "documents. After loading the skill, read the current Issue and only the repository "
            "files required by the EVEX skills.\n"
            + "\n".join(identity_lines[1:])
            + "\n"
        )

    def _has_initial_prompt(self, spec_chat_id: uuid.UUID, expected: str) -> bool:
        expected_lines = expected.splitlines()
        if not expected_lines or expected_lines[0] != "EVEX_SPEC_CHAT":
            raise ProviderError("OpenHands Spec prompt identity is invalid")
        expected_identity = tuple(
            line for line in expected_lines
            if line.startswith("Issue: ") or line.startswith("Issue Conversation: ")
        )
        if len(expected_identity) != 2:
            raise ProviderError("OpenHands Spec prompt identity is invalid")
        events = self._request(
            "GET",
            f"/api/conversations/{spec_chat_id}/events/search"
            "?limit=1&source=user&sort_order=TIMESTAMP",
        )
        for event in events.get("items", []):
            message = event.get("llm_message") if isinstance(event, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if event.get("kind") == "MessageEvent" and event.get("source") == "user" and any(
                isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
                and (
                    item["text"] == expected
                    or (
                        item["text"].startswith("EVEX_SPEC_CHAT\n")
                        and all(identity in item["text"].splitlines() for identity in expected_identity)
                    )
                )
                for item in content or []
            ):
                return True
        return False

    def _checkout_path(self, spec_chat_id: uuid.UUID) -> Path:
        return Path(self.workspace_root).resolve() / f"spec-{spec_chat_id}"

    def _validated_parent_checkout(
        self,
        parent: dict,
        tags: dict[str, Any],
        issue_number: str,
    ) -> tuple[Path, str]:
        if (
            tags.get("evexsourcerepository") != _WORKSPACE_REPOSITORY
            or tags.get("evexsourcebranch") != "main"
        ):
            raise ProviderError("Issue Conversation checkout authority does not match Spec request")
        workspace = parent.get("workspace")
        working_dir = workspace.get("working_dir") if isinstance(workspace, dict) else None
        expected = (
            Path(self.workspace_root).resolve()
            / f"issue-{issue_number}-source"
            / "evex-u-workspace"
        )
        path = Path(working_dir) if isinstance(working_dir, str) else None
        if path != expected or path.is_symlink():
            raise ProviderError("Issue Conversation checkout path does not match authority")
        head = self._validate_existing_checkout(
            expected,
            {
                "repository": _WORKSPACE_REPOSITORY,
                "branch": "main",
                "headSha": "",
            },
            exact=False,
            require_clean=True,
        )
        return expected, head

    def _ensure_checkout(
        self,
        spec_chat_id: uuid.UUID,
        checkout: dict[str, str],
        parent_checkout: Path,
    ) -> str:
        path = self._checkout_path(spec_chat_id)
        if path.is_symlink():
            raise ProviderError("Spec Chat checkout is not an isolated directory")
        if not path.exists():
            self._provision_checkout(path, checkout, parent_checkout)
            return self._validate_existing_checkout(path, checkout, exact=True)
        return self._validate_existing_checkout(
            path, checkout, exact=False, require_clean=True
        )

    def _provision_checkout(
        self,
        path: Path,
        checkout: dict[str, str],
        parent_checkout: Path,
    ) -> None:
        repository = checkout["repository"]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{path.name}.", dir=path.parent))
        try:
            subprocess.run(
                [
                    "git",
                    "--no-optional-locks",
                    "clone",
                    "--quiet",
                    "--no-checkout",
                    "--no-hardlinks",
                    str(parent_checkout),
                    str(temporary),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self._git(
                temporary,
                "remote",
                "set-url",
                "origin",
                f"https://github.com/{repository}.git",
            )
            self._git(temporary, "check-ref-format", "--branch", checkout["branch"])
            self._git(temporary, "cat-file", "-e", f"{checkout['headSha']}^{{commit}}")
            self._git(
                temporary,
                "checkout",
                "-b",
                checkout["branch"],
                checkout["headSha"],
            )
            temporary.rename(path)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProviderError("Spec Chat checkout provisioning failed") from exc
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _validate_existing_checkout(
        self,
        path: Path,
        checkout: dict[str, str],
        *,
        exact: bool,
        require_clean: bool = False,
    ) -> str:
        try:
            if path.is_symlink() or not path.is_dir() or path.resolve() != path:
                raise ProviderError("Spec Chat checkout is not an isolated directory")
            top = self._git(path, "rev-parse", "--show-toplevel")
            remote = self._git(path, "remote", "get-url", "origin")
            branch = self._git(path, "branch", "--show-current")
            head = self._git(path, "rev-parse", "HEAD")
            dirty = self._git(path, "status", "--porcelain")
        except OSError as exc:
            raise ProviderError("Spec Chat checkout validation failed") from exc
        if Path(top).resolve() != path.resolve():
            raise ProviderError("Spec Chat checkout top-level is invalid")
        if self._repository_from_remote(remote).lower() != checkout["repository"].lower():
            raise ProviderError("Spec Chat checkout origin does not match authority")
        if branch != checkout["branch"]:
            raise ProviderError("Spec Chat checkout branch does not match authority")
        if exact and head != checkout["headSha"]:
            raise ProviderError("Spec Chat checkout head does not match authority")
        if (exact or require_clean) and dirty:
            raise ProviderError("Spec Chat checkout must be clean before creation")
        return head

    @staticmethod
    def _git(path: Path, *arguments: str) -> str:
        try:
            result = subprocess.run(
                ["git", "--no-optional-locks", "-C", str(path), *arguments],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProviderError("Spec Chat checkout validation failed") from exc
        return result.stdout.strip()

    @staticmethod
    def _repository_from_remote(remote: str) -> str:
        value = remote.strip()
        if value.startswith("git@github.com:"):
            value = value.removeprefix("git@github.com:")
        elif value.startswith("https://github.com/"):
            value = value.removeprefix("https://github.com/")
        else:
            raise ProviderError("Spec Chat checkout origin must be GitHub")
        value = value.removesuffix(".git")
        if value.count("/") != 1:
            raise ProviderError("Spec Chat checkout origin is invalid")
        return value

    @staticmethod
    def _identity(value: dict) -> tuple[uuid.UUID, dict[str, Any], str]:
        tags = value.get("tags")
        role = tags.get("evexdeliveryrole") if isinstance(tags, dict) else None
        try:
            conversation_id = uuid.UUID(str(value.get("id") or value.get("conversation_id")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ProviderError("OpenHands Discussion identity is invalid") from exc
        if (
            not isinstance(tags, dict)
            or tags.get("project") != "evex-u"
            or role not in _DISCUSSION_ROLES
        ):
            raise ProviderError("OpenHands Discussion identity is invalid")
        return conversation_id, tags, role

    def target_allowed(
        self,
        sender_id: uuid.UUID,
        target_id: uuid.UUID,
        role: str,
        owning_issue_id: uuid.UUID | None,
        project_id: str | None = None,
    ) -> bool:
        target = self._request("GET", f"/api/conversations/{target_id}")
        try:
            target_identity, target_tags, target_role = self._identity(target)
        except ProviderError:
            target_identity, target_tags, target_role = None, {}, ""
        if (
            target_identity == target_id
            and target_role == "specialist"
            and target_tags.get("evexparent") == str(sender_id)
            and target.get("parent_conversation_id") == str(sender_id)
        ):
            return True
        if role in {"subissue", "spec", "specialist"}:
            # The signed capability is the relationship authority. This read
            # proves only that the exact Discussion still exists; mutable
            # presentation tags cannot revoke or redirect its durable binding.
            identities = [
                target[key] for key in ("id", "conversation_id") if key in target
            ]
            return (
                target_id == owning_issue_id
                and all(identity == str(target_id) for identity in identities)
            )
        if role == "project" or (role == "issue" and "evexProjectAdmission" in target):
            # Read BOTH exact objects for every Project send; no cache or tag fallback.
            sender = self._request("GET", f"/api/conversations/{sender_id}")
            sender_admission = self._project_admission(sender, sender_id)
            target_admission = self._project_admission(target, target_id)
            if role == "project":
                project, parent = sender_admission, target_admission
                if project_id != project["project"]["id"]:
                    return False
            else:
                project, parent = target_admission, sender_admission
                if sender_id != owning_issue_id:
                    return False
            return (
                project["role"] == "project" and parent["role"] == "issue"
                and project["project"] == parent["project"]
            )
        if target_identity is None:
            target_identity, target_tags, target_role = self._identity(target)
        if target_identity != target_id:
            return False
        if role != "issue":
            return False
        sender_identity, sender_tags, sender_role = self._identity(
            self._request("GET", f"/api/conversations/{sender_id}")
        )
        if sender_identity != sender_id or sender_role != "issue":
            return False
        same_parent_issue = target_tags.get("evexparentissue") == sender_tags.get("evexissue")
        explicit_parent = target_tags.get("evexparent") == str(sender_id)
        return (target_role == "subissue" and same_parent_issue) or (
            target_role == "spec" and (same_parent_issue or explicit_parent)
        )

    @staticmethod
    def _canonical_uuid(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            return str(uuid.UUID(value)) == value
        except ValueError:
            return False

    @classmethod
    def _project_admission(cls, value: dict, conversation_id: uuid.UUID) -> dict:
        """Consume only the authenticated host's fresh server-computed projection.

        Its presence attests host-verified role, attributable PM-event provenance and
        current native GitHub facts. Tags, viewers and generic turn state cannot fill gaps.
        """
        error = ProviderError("Project relationship admission is unavailable or invalid")
        identities = [value[key] for key in ("id", "conversation_id") if key in value]
        admission = value.get("evexProjectAdmission")
        if (
            not identities or any(identity != str(conversation_id) for identity in identities)
            or not isinstance(admission, dict)
            or set(admission) != {"schemaVersion", "conversationId", "role", "lifecycle", "project", "root"}
            or type(admission["schemaVersion"]) is not int or admission["schemaVersion"] != 1
            or admission["conversationId"] != str(conversation_id)
            or admission["role"] not in ("project", "issue")
            or admission["lifecycle"] != "eligible"
        ):
            raise error
        project = admission["project"]
        if (
            not isinstance(project, dict)
            or set(project) != {"id", "accountablePmId", "nominatedChatId", "state", "accountability", "subjectAccess"}
            or not valid_native_id(project["id"]) or not valid_native_id(project["accountablePmId"])
            or not cls._canonical_uuid(project["nominatedChatId"])
            or project["state"] != "open" or project["accountability"] != "unique"
            or project["subjectAccess"] != "allowed"
        ):
            raise error
        root = admission["root"]
        if admission["role"] == "project":
            if root is not None or project["nominatedChatId"] != str(conversation_id):
                raise error
        elif (
            not isinstance(root, dict)
            or set(root) != {"id", "repository", "number", "issueConversationId", "accountableProjectId", "accountablePmId", "pmAssigned", "membershipProjectId", "state", "projectChatAccess"}
            or not valid_native_id(root["id"]) or root["repository"] != _WORKSPACE_REPOSITORY
            or type(root["number"]) is not int or root["number"] <= 0
            or root["issueConversationId"] != str(conversation_id)
            or root["accountableProjectId"] != project["id"]
            or root["membershipProjectId"] != project["id"]
            or root["accountablePmId"] != project["accountablePmId"]
            or root["pmAssigned"] is not True or root["state"] != "eligible"
            or root["projectChatAccess"] != "allowed"
        ):
            raise error
        return admission

    def send_message(
        self,
        sender_id: uuid.UUID,
        target_id: uuid.UUID,
        message_key: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        if self.api_key and (
            self.api_key in message_key
            or self.api_key in json.dumps(message, ensure_ascii=False)
        ):
            raise ProviderError("message contains a configured credential")
        envelope = json.dumps(
            {
                "aiEvidence": message["aiEvidence"],
                "humanSummary": message["humanSummary"],
                "messageKey": message_key,
                "senderId": str(sender_id),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        envelope = envelope.replace("<", "\\u003c").replace(">", "\\u003e")
        projection = f'{message["humanSummary"]}\n<!-- evex-agent-message:v1 {envelope} -->'
        self._request(
            "POST",
            f"/api/conversations/{target_id}/events",
            {"role": "user", "content": [{"type": "text", "text": projection}], "run": True},
        )
        return {"accepted": True, "messageKey": message_key}
