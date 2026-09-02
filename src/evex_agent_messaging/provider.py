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
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .capability import valid_native_id


# Exact Conversation responses include growing usage statistics, not only identity tags.
# Keep a finite transport budget independent of the much smaller outgoing message limit.
_MAX_RESPONSE_BYTES = 1024 * 1024
_DURABLE_ROLES = {"parent-main", "child-main", "spec"}
_CHECKOUT_LOCKS = tuple(threading.RLock() for _ in range(64))
_SPEC_REASONING = "high"
_SPEC_SKILL = "evex-delivery-spec"
_SUPPORTED_AGENT_KINDS = {"acp", "openhands"}
_WORKSPACE_REPOSITORY = "EvexU2/evex-u-workspace"
_MESSAGING_ADMISSION = re.compile(r"v1:messaging:[0-9a-f]{64}")
_ADMISSION_CAPABILITY = "evex_delivery_admission_v1"
_ADMISSION_MIGRATION_TAG = "evexadmissionrequest"


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class OpenHandsProvider:
    base_url: str
    api_key: str
    timeout: float = 5.0
    transport: Callable[[str, str, dict | None], dict] | None = None
    public_url: str = ""
    workspace_root: str = "/home/openhands/workspace/delivery"
    admission_key: bytes = b""

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
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
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
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
            or parent_role != "parent-main"
            or not isinstance(issue_ref, str)
            or "#" not in issue_ref
        ):
            raise ProviderError("Parent Main identity is invalid")
        issue_repository, issue_number = issue_ref.rsplit("#", 1)
        if issue_repository != _WORKSPACE_REPOSITORY or not issue_number.isdigit():
            raise ProviderError("Parent Main Issue identity is invalid")
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
            f"Parent Main: {parent_id}\n"
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
                created = True
                create_confirmed = True
            except ProviderError as create_error:
                try:
                    self._request("GET", f"/api/conversations/{spec_chat_id}")
                except ProviderError:
                    raise create_error
                created = create_error.status != 409
                prompt = None
            self._request(
                "PATCH",
                f"/api/conversations/{spec_chat_id}",
                {"title": f"#{issue_number} · Spec"},
            )
            existing = None

        verified = self._request("GET", f"/api/conversations/{spec_chat_id}")
        verified = self._migrate_spec_if_needed(
            verified,
            parent_id,
            spec_chat_id,
            issue_ref,
            issue_number,
            checkout,
        )
        self._validate_existing_spec(
            verified,
            parent_id,
            spec_chat_id,
            issue_ref,
            issue_number,
            checkout,
            profile_id if created else None,
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
                        "run": False,
                    },
                )
            except ProviderError as prompt_error:
                if not self._has_initial_prompt(spec_chat_id, prompt_identity):
                    raise prompt_error
        verified_tags = verified.get("tags")
        if (
            isinstance(verified_tags, dict)
            and verified_tags.get("evexdeliveryrole") == "spec"
            and verified_tags.get("evexskills") == _SPEC_SKILL
        ):
            self._ensure_spec_goal(spec_chat_id, self._spec_goal(issue_ref))
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
    ) -> dict[str, Any]:
        return {
            "conversation_id": str(spec_chat_id),
            "parent_conversation_id": "",
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
    ) -> str:
        token = self._admission_token(
            self._admission_descriptor(
                spec_chat_id, profile_id, working_dir, tags
            )
        )
        return f"v1:messaging:{hashlib.sha256(token.encode()).hexdigest()}"

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

    def _migrate_spec_if_needed(
        self,
        value: dict,
        parent_id: uuid.UUID,
        spec_chat_id: uuid.UUID,
        issue_ref: str,
        issue_number: str,
        checkout: dict[str, str],
    ) -> dict:
        tags = value.get("tags")
        if (
            not isinstance(tags, dict)
            or "evexagentprofile" not in tags
            or "evexadmission" in tags
        ):
            return value
        profile_id = tags.get("evexagentprofile")
        workspace = value.get("workspace")
        working_dir = workspace.get("working_dir") if isinstance(workspace, dict) else None
        if not isinstance(profile_id, str) or not isinstance(working_dir, str):
            return value
        self._validate_existing_spec(
            value,
            parent_id,
            spec_chat_id,
            issue_ref,
            issue_number,
            checkout,
            profile_id,
            allow_missing_admission=True,
        )
        self._require_admission_capability()
        unsigned_tags = {str(key): str(item) for key, item in tags.items()}
        canonical_tags = self._spec_tags(
            parent_id, issue_ref, issue_number, checkout, profile_id
        )
        token = self._admission_token(
            self._admission_descriptor(
                spec_chat_id, profile_id, working_dir, canonical_tags
            )
        )
        self._request(
            "PATCH",
            f"/api/conversations/{spec_chat_id}",
            {"tags": {**unsigned_tags, _ADMISSION_MIGRATION_TAG: token}},
        )
        return self._request("GET", f"/api/conversations/{spec_chat_id}")

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
            "evexrole": "role-child",
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
        *,
        allow_missing_admission: bool = False,
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
        legacy = role == "spec" and not present_new_metadata
        profile = value.get("launched_agent_profile")
        launched_profile_id = (
            profile.get("agent_profile_id") if isinstance(profile, dict) else None
        )
        if legacy:
            expected_tags = {
                key: expected
                for key, expected in self._spec_tags(
                    parent_id,
                    issue_ref,
                    issue_number,
                    checkout,
                    launched_profile_id if isinstance(launched_profile_id, str) else "",
                ).items()
                if key not in {"evexdeliveryrole", "evexskills", "evexagentprofile"}
            }
        else:
            bound_profile_id = tags.get("evexagentprofile")
            expected_tags = self._spec_tags(
                parent_id,
                issue_ref,
                issue_number,
                checkout,
                bound_profile_id if isinstance(bound_profile_id, str) else "",
            )
        unexpected_reserved = (
            {
                str(key)
                for key in tags
                if str(key).startswith("evex")
                and key not in expected_tags
                and key != "evexadmission"
            }
            if not legacy
            else set()
        )
        if (
            identity != spec_chat_id
            or role != "spec"
            or (not legacy and present_new_metadata != new_metadata)
            or any(tags.get(key) != expected for key, expected in expected_tags.items())
            or unexpected_reserved
            or not workspace_matches
            or not isinstance(launched_profile_id, str)
            or not launched_profile_id
            or (
                legacy
                and (
                    tags.get("evexmodel") != "gpt-5.6-sol"
                    or value.get("current_model_id") != "gpt-5.6-sol"
                )
            )
            or (
                not legacy
                and (
                    launched_profile_id != tags.get("evexagentprofile")
                    or (
                        expected_profile_id is not None
                        and launched_profile_id != expected_profile_id
                    )
                )
            )
        ):
            raise ProviderError("Existing Spec Chat does not match authority")
        if not legacy:
            marker = tags.get("evexadmission", "")
            if allow_missing_admission and not marker:
                return
            unsigned_tags = {
                str(key): str(item)
                for key, item in tags.items()
                if key != "evexadmission"
            }
            expected_marker = self._expected_admission_marker(
                spec_chat_id,
                str(launched_profile_id),
                str(working_dir),
                unsigned_tags,
            )
            if (
                _MESSAGING_ADMISSION.fullmatch(marker) is None
                or not hmac.compare_digest(marker, expected_marker)
            ):
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
        return (
            identity
            + "Your task now: run the interactive Spec Chat for this Issue. "
            + activation
            + " Never load skills from the source checkout or search it for skill-support "
            "documents. After loading the skill, read the current Issue and only the repository "
            "files required by the EVEX skills."
        )

    @staticmethod
    def _spec_goal(issue_ref: str) -> str:
        return (
            f"Deliver reviewed Specification authority for {issue_ref} through the "
            "interactive question, review, repair, and human-approval loop. Complete "
            "the OpenHands goal only after the exact Spec result is terminally projected "
            "to the bound Parent Main."
        )

    def _ensure_spec_goal(self, spec_chat_id: uuid.UUID, objective: str) -> None:
        status = self._goal_status(spec_chat_id, objective)
        if status in {"running", "complete"}:
            return
        if status == "interrupted":
            try:
                self._request(
                    "POST",
                    f"/api/conversations/{spec_chat_id}/goal/resume",
                    {},
                )
            except ProviderError as resume_error:
                if self._goal_status(spec_chat_id, objective) not in {
                    "running",
                    "complete",
                }:
                    raise resume_error
                return
            if self._goal_status(spec_chat_id, objective) not in {
                "running",
                "complete",
            }:
                raise ProviderError("OpenHands Spec goal did not resume")
            return
        if status is not None:
            raise ProviderError("OpenHands Spec goal is terminal or invalid")
        try:
            self._request(
                "POST",
                f"/api/conversations/{spec_chat_id}/goal",
                {"objective": objective, "max_iterations": 100},
            )
        except ProviderError as goal_error:
            if self._goal_status(spec_chat_id, objective) not in {
                "running",
                "complete",
            }:
                raise goal_error
            return
        if self._goal_status(spec_chat_id, objective) not in {"running", "complete"}:
            raise ProviderError("OpenHands Spec goal was not durably established")

    def _goal_status(self, spec_chat_id: uuid.UUID, expected: str) -> str | None:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            query = {
                "limit": 100,
                "kind": "ConversationStateUpdateEvent",
                "sort_order": "TIMESTAMP_DESC",
            }
            if cursor is not None:
                query["page_id"] = cursor
            events = self._request(
                "GET",
                f"/api/conversations/{spec_chat_id}/events/search?"
                + urllib.parse.urlencode(query),
            )
            items = events.get("items")
            if not isinstance(items, list):
                raise ProviderError("OpenHands goal state is unavailable")
            for event in items:
                value = event.get("value") if isinstance(event, dict) else None
                if not (
                    event.get("kind") == "ConversationStateUpdateEvent"
                    and event.get("key") == "goal"
                    and isinstance(value, dict)
                ):
                    continue
                if str(value.get("objective") or "").strip() != expected:
                    raise ProviderError("OpenHands Spec goal authority does not match")
                status = value.get("status")
                return status if isinstance(status, str) else ""
            next_cursor = events.get("next_page_id")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ProviderError("OpenHands goal cursor is invalid")
            if next_cursor in seen_cursors:
                raise ProviderError("OpenHands goal pagination repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return None

    def _has_initial_prompt(self, spec_chat_id: uuid.UUID, expected: str) -> bool:
        expected_lines = expected.splitlines()
        if len(expected_lines) < 3 or expected_lines[0] != "EVEX_SPEC_CHAT":
            raise ProviderError("OpenHands Spec prompt identity is invalid")
        stable_identity = "\n".join(expected_lines[:3]) + "\n"
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
                    or item["text"].startswith(stable_identity)
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
            raise ProviderError("Parent Main checkout authority does not match Spec request")
        workspace = parent.get("workspace")
        working_dir = workspace.get("working_dir") if isinstance(workspace, dict) else None
        expected = (
            Path(self.workspace_root).resolve()
            / f"issue-{issue_number}-source"
            / "evex-u-workspace"
        )
        path = Path(working_dir) if isinstance(working_dir, str) else None
        if path != expected or path.is_symlink():
            raise ProviderError("Parent Main checkout path does not match authority")
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
            or role not in _DURABLE_ROLES
        ):
            raise ProviderError("OpenHands Discussion identity is invalid")
        return conversation_id, tags, role

    def target_allowed(
        self,
        sender_id: uuid.UUID,
        target_id: uuid.UUID,
        role: str,
        owning_main_id: uuid.UUID | None,
        project_id: str | None = None,
    ) -> bool:
        target = self._request("GET", f"/api/conversations/{target_id}")
        if role == "project" or (role == "main" and "evexProjectAdmission" in target):
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
                if sender_id != owning_main_id:
                    return False
            return (
                project["role"] == "project" and parent["role"] == "parent-main"
                and project["project"] == parent["project"]
            )
        target_identity, target_tags, target_role = self._identity(
            target
        )
        if target_identity != target_id:
            return False
        if role in {"deputy", "spec"}:
            return target_id == owning_main_id and target_role in {"parent-main", "child-main"}
        if role != "main":
            return False
        sender_identity, sender_tags, sender_role = self._identity(
            self._request("GET", f"/api/conversations/{sender_id}")
        )
        if sender_identity != sender_id or sender_role != "parent-main":
            return False
        same_parent_issue = target_tags.get("evexparentissue") == sender_tags.get("evexissue")
        explicit_parent = target_tags.get("evexparent") == str(sender_id)
        return (target_role == "child-main" and same_parent_issue) or (
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
            or admission["role"] not in ("project", "parent-main")
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
            or set(root) != {"id", "repository", "number", "parentMainId", "accountableProjectId", "accountablePmId", "pmAssigned", "membershipProjectId", "state", "projectChatAccess"}
            or not valid_native_id(root["id"]) or root["repository"] != _WORKSPACE_REPOSITORY
            or type(root["number"]) is not int or root["number"] <= 0
            or root["parentMainId"] != str(conversation_id)
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
        projection = f'{message["humanSummary"]}\n<!-- evex-agent-message:v1 {envelope} -->'
        self._request(
            "POST",
            f"/api/conversations/{target_id}/events",
            {"role": "user", "content": [{"type": "text", "text": projection}], "run": True},
        )
        return {"accepted": True, "messageKey": message_key}
