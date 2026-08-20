"""Provider adapter. Only this module knows OpenHands REST details."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid


class ProviderError(RuntimeError):
    """Provider failure translated away from raw credentials/HTTP details."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


_CHECKOUT_LOCKS = tuple(threading.Lock() for _ in range(64))


@dataclass
class OpenHandsProvider:
    base_url: str
    api_key: str
    public_url: str
    timeout: float = 5.0
    sleeper: object = time.sleep
    completion_hook_url: str = "http://evex-agent-messaging.evex-agents.svc.cluster.local:3101/completion-hook"
    workspace_root: str = "/home/openhands/workspace/delivery"

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=json.dumps(body, separators=(",", ":")).encode() if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json", "X-Session-API-Key": self.api_key},
        )
        created = True
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                value = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise ProviderError("OpenHands messaging transport failed", status=exc.code) from exc
        except OSError as exc:
            raise ProviderError("OpenHands messaging transport failed") from exc
        if not isinstance(value, dict):
            raise ProviderError("OpenHands returned an invalid response")
        return value

    def create_child(
        self,
        parent_id: uuid.UUID,
        child_id: uuid.UUID,
        role: str,
        task_key: str,
        mission: dict,
        capability_ref: str,
        capabilities: frozenset[str],
    ) -> dict:
        mission_text = "MISSION\n" + json.dumps(mission, sort_keys=True, separators=(",", ":"))
        try:
            existing = self._request("GET", f"/api/conversations/{child_id}")
            self._validate_existing_child(existing, parent_id, child_id, role, task_key)
            self._validate_existing_checkout(
                self._checkout_path(child_id), mission.get("checkout"), exact=False
            )
            if not existing.get("last_user_message_id"):
                self._ensure_checkout(child_id, mission.get("checkout"))
                self._request("POST", f"/api/conversations/{child_id}/events", {"role": "user", "content": [{"type": "text", "text": mission_text}], "run": True})
            return {"conversationUrl": f"{self.public_url.rstrip('/')}/conversations/{child_id}", "provider": "openhands", "created": False}
        except ProviderError as exc:
            if exc.status != 404:
                raise
        self._ensure_checkout(child_id, mission.get("checkout"))
        created = True
        try:
            profiles = self._request("GET", "/api/agent-profiles")
            profile_id = profiles.get("active_agent_profile_id")
            if not isinstance(profile_id, str) or not profile_id:
                raise ProviderError("OpenHands has no active Agent Profile")
            settings = self._request("GET", "/api/settings")
            mcp_config = self._mission_mcp_config(settings, capabilities)
            payload = {
                "conversation_id": str(child_id),
                "agent_profile_id": profile_id,
                "workspace": {"working_dir": str(self._checkout_path(child_id))},
                "tags": {"project": "evex-u", "evexrole": "role-child", "evextask": task_key, "evexparent": str(parent_id), "evexchildrole": role},
                "autotitle": False,
                "max_iterations": 300,
                "secrets": {
                    "EVEX_AGENT_ROLE": {"kind": "StaticSecret", "value": role},
                    "EVEX_AGENT_INSTANCE_ID": {
                        "kind": "StaticSecret",
                        "value": str(child_id),
                    },
                    "EVEX_AGENT_SKILLS": {
                        "kind": "StaticSecret",
                        "value": "\n".join(mission.get("skills", [])),
                    },
                },
                "agent_launch_additions": {
                    "system_message_suffix_append": (
                        f"EVEX role scope: {role}. Use only the Mission-authorized checkout, "
                        "skills, GitHub mutations, Messaging MCP, and any explicitly provisioned "
                        "Runtime MCP. Never call OpenHands provider-control APIs or inspect peers."
                    )
                },
                "hook_config": {
                    "session_start": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": self._admission_hook_command(
                                        child_id, mission["checkout"]
                                    ),
                                    "timeout": 10,
                                }
                            ],
                        }
                    ],
                    "stop": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": self._completion_hook_command(capability_ref),
                                    "timeout": 50,
                                    "async": True,
                                }
                            ],
                        }
                    ]
                },
            }
            if mcp_config:
                payload["mcp_config"] = mcp_config
            result = self._request(
                "POST",
                "/api/conversations",
                payload,
            )
        except ProviderError as exc:
            if exc.status != 409:
                raise
            existing = self._request("GET", f"/api/conversations/{child_id}")
            self._validate_existing_child(existing, parent_id, child_id, role, task_key)
            created = False
        if created or not existing.get("last_user_message_id"):
            self._ensure_checkout(child_id, mission.get("checkout"))
            self._admission_marker(child_id).unlink(missing_ok=True)
            self._request("POST", f"/api/conversations/{child_id}/events", {"role": "user", "content": [{"type": "text", "text": mission_text}], "run": True})
            self._wait_for_admission(child_id, mission.get("checkout"))
        return {"conversationUrl": f"{self.public_url.rstrip('/')}/conversations/{child_id}", "provider": "openhands", "created": created}

    def _checkout_path(self, child_id: uuid.UUID) -> Path:
        return Path(self.workspace_root).resolve() / f"child-{child_id}"

    def _admission_marker(self, child_id: uuid.UUID) -> Path:
        return Path(self.workspace_root).resolve().parent / ".evex-admission" / f"{child_id}.ready"

    def _admission_hook_command(self, child_id: uuid.UUID, checkout: dict) -> str:
        path = self._checkout_path(child_id)
        marker = self._admission_marker(child_id)
        temporary = marker.with_suffix(".tmp")
        head = str(checkout["headSha"])
        ref = f"refs/heads/{checkout['branch']}"
        quoted_path = shlex.quote(str(path))
        quoted_head = shlex.quote(head)
        quoted_ref = shlex.quote(ref)
        quoted_marker_parent = shlex.quote(str(marker.parent))
        quoted_marker = shlex.quote(str(marker))
        quoted_temporary = shlex.quote(str(temporary))
        return (
            "set -eu; "
            f"test \"$(git -C {quoted_path} rev-parse --show-toplevel)\" = {quoted_path}; "
            f"test \"$(git -C {quoted_path} symbolic-ref HEAD)\" = {quoted_ref}; "
            f"git -C {quoted_path} cat-file -e {quoted_head}^{{commit}}; "
            f"git -C {quoted_path} diff --quiet {quoted_head} --; "
            f"git -C {quoted_path} diff --cached --quiet {quoted_head} --; "
            f"git -C {quoted_path} update-ref {quoted_ref} {quoted_head}; "
            f"test \"$(git -C {quoted_path} rev-parse HEAD)\" = {quoted_head}; "
            f"test -z \"$(git -C {quoted_path} status --porcelain)\"; "
            f"mkdir -p {quoted_marker_parent}; "
            f"printf '%s\\n' {quoted_head} > {quoted_temporary}; "
            f"mv {quoted_temporary} {quoted_marker}"
        )

    def _wait_for_admission(self, child_id: uuid.UUID, checkout: object) -> None:
        if not isinstance(checkout, dict):
            raise ProviderError("Child checkout authority is missing")
        marker = self._admission_marker(child_id)
        expected = str(checkout.get("headSha", ""))
        for _ in range(150):
            try:
                admitted = marker.read_text().strip() == expected
            except OSError:
                admitted = False
            if admitted:
                self._validate_existing_checkout(self._checkout_path(child_id), checkout, exact=True)
                marker.unlink(missing_ok=True)
                return
            self.sleeper(0.1)
        raise ProviderError("OpenHands Child runtime admission did not preserve the exact checkout")

    def _validate_existing_child(
        self,
        value: dict,
        parent_id: uuid.UUID,
        child_id: uuid.UUID,
        role: str,
        task_key: str,
    ) -> None:
        tags = value.get("tags")
        workspace = value.get("workspace")
        expected_tags = {
            "project": "evex-u",
            "evexrole": "role-child",
            "evextask": task_key,
            "evexparent": str(parent_id),
            "evexchildrole": role,
        }
        working_dir = workspace.get("working_dir") if isinstance(workspace, dict) else None
        try:
            working_dir_matches = (
                isinstance(working_dir, str)
                and Path(working_dir).resolve() == self._checkout_path(child_id)
            )
        except OSError:
            working_dir_matches = False
        if (
            str(value.get("id") or value.get("conversation_id") or "") != str(child_id)
            or not isinstance(tags, dict)
            or any(tags.get(key) != expected for key, expected in expected_tags.items())
            or not working_dir_matches
        ):
            raise ProviderError("Existing Child Conversation does not match Mission authority")

    def _ensure_checkout(self, child_id: uuid.UUID, checkout: object) -> None:
        if not isinstance(checkout, dict):
            raise ProviderError("Child checkout authority is missing")
        path = self._checkout_path(child_id)
        lock = _CHECKOUT_LOCKS[child_id.int % len(_CHECKOUT_LOCKS)]
        with lock:
            if path.is_symlink():
                raise ProviderError("Child checkout is not an isolated directory")
            if not path.exists():
                self._provision_checkout(path, checkout)
            self._validate_existing_checkout(path, checkout, exact=True)

    def _provision_checkout(self, path: Path, checkout: dict) -> None:
        repository = str(checkout.get("repository", ""))
        if repository.count("/") != 1:
            raise ProviderError("Child checkout repository is invalid")
        owner, name = repository.split("/", 1)
        mirror = Path(self.workspace_root).resolve().parent / "mirrors" / f"{owner}--{name}.git"
        if mirror.is_symlink() or not mirror.is_dir():
            raise ProviderError(f"Child checkout mirror is unavailable: {mirror}")
        if self._repository_from_remote(self._git(mirror, "remote", "get-url", "origin")).lower() != repository.lower():
            raise ProviderError("Child checkout mirror origin does not match Mission authority")
        head_sha = str(checkout.get("headSha", ""))
        branch = str(checkout.get("branch", ""))
        self._git(mirror, "check-ref-format", "--branch", branch)
        self._git(mirror, "cat-file", "-e", f"{head_sha}^{{commit}}")
        try:
            subprocess.run(
                [
                    "git",
                    "--no-optional-locks",
                    "--git-dir",
                    str(mirror),
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(path),
                    head_sha,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProviderError("Child checkout provisioning failed") from exc

    def _validate_existing_checkout(
        self, path: Path, checkout: object, *, exact: bool
    ) -> None:
        if not isinstance(checkout, dict):
            raise ProviderError("Child checkout authority is missing")
        try:
            if path.is_symlink() or not path.is_dir() or path.resolve() != path:
                raise ProviderError("Child checkout is not an isolated directory")
            top = self._git(path, "rev-parse", "--show-toplevel")
            remote = self._git(path, "remote", "get-url", "origin")
            branch = self._git(path, "branch", "--show-current")
            head = self._git(path, "rev-parse", "HEAD")
            dirty = self._git(path, "status", "--porcelain")
        except OSError as exc:
            raise ProviderError("Child checkout validation failed") from exc
        if Path(top).resolve() != path.resolve():
            raise ProviderError("Child checkout top-level does not match its isolated directory")
        if self._repository_from_remote(remote).lower() != str(checkout.get("repository", "")).lower():
            raise ProviderError("Child checkout origin does not match Mission authority")
        if branch != checkout.get("branch"):
            raise ProviderError("Child checkout branch does not match Mission authority")
        if exact and head != checkout.get("headSha"):
            raise ProviderError("Child checkout head does not match Mission authority")
        if exact and dirty:
            raise ProviderError("Child checkout must be clean before Conversation creation")

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
            raise ProviderError("Child checkout validation failed") from exc
        return result.stdout.strip()

    @staticmethod
    def _repository_from_remote(remote: str) -> str:
        value = remote.strip()
        if value.startswith("git@github.com:"):
            value = value.removeprefix("git@github.com:")
        elif value.startswith("https://github.com/"):
            value = value.removeprefix("https://github.com/")
        else:
            raise ProviderError("Child checkout origin must be a GitHub repository")
        value = value.removesuffix(".git")
        if value.count("/") != 1:
            raise ProviderError("Child checkout origin is invalid")
        return value

    def _completion_hook_command(self, capability_ref: str) -> str:
        body = json.dumps({"capabilityRef": capability_ref}, separators=(",", ":"))
        return (
            "curl --fail --silent --show-error --retry 2 --retry-delay 2 "
            "--retry-all-errors --max-time 45 --header 'Content-Type: application/json' "
            f"--data {shlex.quote(body)} {shlex.quote(self.completion_hook_url)}"
        )

    @staticmethod
    def _mission_mcp_config(settings: dict, capabilities: frozenset[str]) -> dict:
        agent_settings = settings.get("agent_settings") if isinstance(settings, dict) else None
        value = agent_settings.get("mcp_config") if isinstance(agent_settings, dict) else None
        if not isinstance(value, dict):
            return {}
        requested = {"evex_agent_messaging"}
        if "runtime_environment" in capabilities:
            requested.add("evex_runtime")
        servers = value.get("mcpServers")
        if isinstance(servers, dict):
            selected = {
                name: server
                for name, server in servers.items()
                if name in requested and isinstance(server, dict)
            }
            return {"mcpServers": selected} if selected else {}
        return {
            name: server
            for name, server in value.items()
            if name in requested and isinstance(server, dict)
        }

    def wait_until_terminal(self, target_id: uuid.UUID) -> str:
        path = f"/api/conversations/{target_id}"
        for _ in range(300):
            status = self._request("GET", path).get("execution_status")
            if status in {"finished", "error", "stuck"}:
                return status
            self.sleeper(0.1)
        raise ProviderError("OpenHands Child did not reach terminal state")

    def send_message(self, target_id: uuid.UUID, message_key: str, kind: str, text: str) -> dict:
        path = f"/api/conversations/{target_id}"
        for _ in range(300):
            status = self._request("GET", path).get("execution_status")
            if status in {"idle", "paused", "finished", "error", "stuck"}:
                break
            self.sleeper(0.1)
        else:
            raise ProviderError("OpenHands Main did not become callback-wakeable")
        self._request("POST", f"/api/conversations/{target_id}/events", {"role": "user", "content": [{"type": "text", "text": f"{kind}\n{text}"}], "run": True})
        return {"accepted": True, "messageKey": message_key}

    def cancel_mission(
        self,
        target_id: uuid.UUID,
        message_key: str,
        task_key: str,
        owning_main_id: uuid.UUID,
    ) -> dict:
        path = f"/api/conversations/{target_id}"
        current = self._request("GET", path)
        status = current.get("execution_status")
        if status in {"finished", "error", "stuck"}:
            return {"accepted": True, "messageKey": message_key, "taskKey": task_key, "terminal": True}
        if status == "running":
            self._request("POST", f"{path}/interrupt", {})
        for _ in range(20):
            current = self._request("GET", path)
            status = current.get("execution_status")
            if status in {"finished", "error", "stuck"}:
                return {"accepted": True, "messageKey": message_key, "taskKey": task_key, "terminal": True}
            if status in {"paused", "idle"}:
                envelope = {
                    "childId": str(target_id),
                    "messageKey": message_key,
                    "owningMainId": str(owning_main_id),
                    "targetId": str(target_id),
                    "taskKey": task_key,
                }
                self._request(
                    "POST",
                    f"{path}/events",
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "CANCEL_MISSION\n"
                                + json.dumps(envelope, sort_keys=True, separators=(",", ":")),
                            }
                        ],
                        "run": True,
                    },
                )
                self.wait_until_terminal(target_id)
                return {"accepted": True, "messageKey": message_key, "taskKey": task_key, "terminal": True}
            self.sleeper(0.1)
        raise ProviderError("OpenHands Child did not become cancellation-wakeable")

    def resume_mission(self, target_id: uuid.UUID, message_key: str, task_key: str) -> dict:
        self._request("POST", f"/api/conversations/{target_id}/events", {"role": "user", "content": [{"type": "text", "text": "RESUME_MISSION\n" + message_key}], "run": True})
        return {"accepted": True, "messageKey": message_key, "taskKey": task_key}
