"""Small OpenHands adapter for exact-target Discussion messages."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from typing import Any, Callable
import urllib.error
import urllib.request
import uuid


_MAX_RESPONSE_BYTES = 65_536
_DURABLE_ROLES = {"parent-main", "child-main", "spec"}
_CHECKOUT_LOCKS = tuple(threading.RLock() for _ in range(64))
_SPEC_MODEL = "gpt-5.6-sol"
_SPEC_REASONING = "high"


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
        if not self.base_url.strip() or not self.api_key.strip() or not self.public_url.strip():
            return False
        try:
            value = self._request("GET", "/api/agent-profiles")
        except ProviderError:
            return False
        return isinstance(value.get("active_agent_profile_id"), str)

    def create_spec_chat(
        self,
        parent_id: uuid.UUID,
        spec_chat_id: uuid.UUID,
        checkout: dict[str, str],
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
        if checkout["repository"] != issue_repository or not issue_number.isdigit():
            raise ProviderError("Spec Chat checkout does not match the owning Issue")
        parent_checkout = self._validated_parent_checkout(
            parent_value, parent_tags, issue_number, checkout
        )

        lock = _CHECKOUT_LOCKS[spec_chat_id.int % len(_CHECKOUT_LOCKS)]
        with lock:
            return self._create_spec_chat_locked(
                parent_id,
                spec_chat_id,
                issue_ref,
                issue_number,
                checkout,
                parent_checkout,
                capability_ref,
            )

    def _create_spec_chat_locked(
        self,
        parent_id: uuid.UUID,
        spec_chat_id: uuid.UUID,
        issue_ref: str,
        issue_number: str,
        checkout: dict[str, str],
        parent_checkout: Path,
        capability_ref: str,
    ) -> dict[str, Any]:
        prompt = (
            "EVEX_SPEC_CHAT\n"
            f"Issue: https://github.com/{issue_ref.replace('#', '/issues/')}\n"
            f"Parent Main: {parent_id}\n"
            "Your task now: run the interactive Spec Chat for this Issue using the admitted "
            "EVEX Spec skills. Start by reading the current Issue and living Specification."
        )
        created = False
        try:
            existing = self._request("GET", f"/api/conversations/{spec_chat_id}")
        except ProviderError as exc:
            if exc.status != 404:
                raise
            self._ensure_checkout(spec_chat_id, checkout, parent_checkout)
            profiles = self._request("GET", "/api/agent-profiles")
            profile_id = profiles.get("active_agent_profile_id")
            if not isinstance(profile_id, str) or not profile_id:
                raise ProviderError("OpenHands has no active Agent Profile")
            payload = {
                "conversation_id": str(spec_chat_id),
                "agent_profile_id": profile_id,
                "workspace": {"working_dir": str(self._checkout_path(spec_chat_id))},
                "tags": self._spec_tags(
                    parent_id, issue_ref, issue_number, checkout
                ),
                "autotitle": False,
                "max_iterations": 300,
                "mcp_config": {},
                "language": "de-DE",
                "secrets": {
                    "EVEX_AGENT_ROLE": {"kind": "StaticSecret", "value": "spec"},
                    "EVEX_AGENT_INSTANCE_ID": {
                        "kind": "StaticSecret",
                        "value": str(spec_chat_id),
                    },
                    "EVEX_AGENT_MESSAGING_CAPABILITY": {
                        "kind": "StaticSecret",
                        "value": capability_ref,
                    },
                    "EVEX_REASONING_EFFORT": {
                        "kind": "StaticSecret",
                        "value": _SPEC_REASONING,
                    },
                },
                "agent_launch_additions": {
                    "system_message_suffix_append": (
                        "EVEX role scope: interactive Spec Chat. Use the admitted checkout, "
                        "EVEX Spec skills, native read-only review subagents, and send_message "
                        "only to the bound Parent Main. Conduct new human dialogue in German "
                        "(de-DE) unless that Chat's human changes its language."
                    )
                },
            }
            try:
                self._request("POST", "/api/conversations", payload)
                created = True
            except ProviderError as create_error:
                try:
                    self._request("GET", f"/api/conversations/{spec_chat_id}")
                except ProviderError:
                    raise create_error
                created = create_error.status != 409
            self._request(
                "PATCH",
                f"/api/conversations/{spec_chat_id}",
                {"title": f"#{issue_number} · Spec"},
            )
            existing = None

        self._switch_and_verify_spec_model(spec_chat_id)
        verified = self._request("GET", f"/api/conversations/{spec_chat_id}")
        self._validate_existing_spec(
            verified, parent_id, spec_chat_id, issue_ref, issue_number, checkout
        )
        if not created:
            self._validate_existing_checkout(
                self._checkout_path(spec_chat_id), checkout, exact=False
            )
            self._request(
                "POST",
                f"/api/conversations/{spec_chat_id}/secrets",
                {"secrets": {"EVEX_AGENT_MESSAGING_CAPABILITY": {
                    "kind": "StaticSecret",
                    "value": capability_ref,
                }}},
            )
        if created or not self._has_initial_prompt(spec_chat_id, prompt):
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
                if not self._has_initial_prompt(spec_chat_id, prompt):
                    raise prompt_error
        return {
            "conversationUrl": (
                f"{self.public_url.rstrip('/')}/conversations/{spec_chat_id}"
            ),
            "provider": "openhands",
            "created": created,
        }

    @staticmethod
    def _spec_tags(
        parent_id: uuid.UUID,
        issue_ref: str,
        issue_number: str,
        checkout: dict[str, str],
    ) -> dict[str, str]:
        return {
            "project": "evex-u",
            "evexrole": "role-child",
            "evexdeliveryrole": "spec",
            "evextask": f"issue-{issue_number}-spec",
            "evexissue": issue_ref,
            "evexparent": str(parent_id),
            "evexrepository": checkout["repository"],
            "evexbranch": checkout["branch"],
            "evexbasehead": checkout["headSha"],
            "evexmodel": _SPEC_MODEL,
            "evexreasoning": _SPEC_REASONING,
            "evexlocale": "de-DE",
        }

    def _validate_existing_spec(
        self,
        value: dict,
        parent_id: uuid.UUID,
        spec_chat_id: uuid.UUID,
        issue_ref: str,
        issue_number: str,
        checkout: dict[str, str],
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
        expected_tags = self._spec_tags(parent_id, issue_ref, issue_number, checkout)
        if (
            identity != spec_chat_id
            or role != "spec"
            or any(tags.get(key) != expected for key, expected in expected_tags.items())
            or not workspace_matches
            or value.get("current_model_id") != _SPEC_MODEL
        ):
            raise ProviderError("Existing Spec Chat does not match authority")

    def _switch_and_verify_spec_model(self, spec_chat_id: uuid.UUID) -> None:
        self._request(
            "POST",
            f"/api/conversations/{spec_chat_id}/switch_acp_model",
            {"model": _SPEC_MODEL},
        )

    def _has_initial_prompt(self, spec_chat_id: uuid.UUID, expected: str) -> bool:
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
                and item.get("text") == expected
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
        checkout: dict[str, str],
    ) -> Path:
        if (
            tags.get("evexsourcerepository") != checkout["repository"]
            or tags.get("evexsourcebranch") != "main"
            or checkout["branch"] != f"spec/issue-{issue_number}"
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
        self._validate_existing_checkout(
            expected,
            {
                "repository": checkout["repository"],
                "branch": "main",
                "headSha": checkout["headSha"],
            },
            exact=True,
        )
        return expected

    def _ensure_checkout(
        self,
        spec_chat_id: uuid.UUID,
        checkout: dict[str, str],
        parent_checkout: Path,
    ) -> None:
        path = self._checkout_path(spec_chat_id)
        if path.is_symlink():
            raise ProviderError("Spec Chat checkout is not an isolated directory")
        if not path.exists():
            self._provision_checkout(path, checkout, parent_checkout)
        self._validate_existing_checkout(path, checkout, exact=True)

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
        self, path: Path, checkout: dict[str, str], *, exact: bool
    ) -> None:
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
        if exact and dirty:
            raise ProviderError("Spec Chat checkout must be clean before creation")

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
        owning_main_id: uuid.UUID,
    ) -> bool:
        target_identity, target_tags, target_role = self._identity(
            self._request("GET", f"/api/conversations/{target_id}")
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

    def send_message(
        self,
        sender_id: uuid.UUID,
        target_id: uuid.UUID,
        message_key: str,
        message: dict[str, Any],
        recipient_capability_ref: str | None = None,
    ) -> dict[str, Any]:
        if self.api_key and self.api_key in json.dumps(message, ensure_ascii=False):
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
        if recipient_capability_ref is not None:
            self._request(
                "POST",
                f"/api/conversations/{target_id}/secrets",
                {"secrets": {"EVEX_AGENT_MESSAGING_CAPABILITY": {
                    "kind": "StaticSecret",
                    "value": recipient_capability_ref,
                }}},
            )
        self._request(
            "POST",
            f"/api/conversations/{target_id}/events",
            {"role": "user", "content": [{"type": "text", "text": projection}], "run": True},
        )
        return {"accepted": True, "messageKey": message_key}
