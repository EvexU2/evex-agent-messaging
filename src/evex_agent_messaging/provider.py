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


_CHECKOUT_LOCKS = tuple(threading.RLock() for _ in range(64))

_STANDARD_PRICES_PER_MILLION = {
    "gpt-5.6-sol": {
        "uncached_input": 4.0,
        "cached_input": 0.4,
        "cache_write": 5.0,
        "output": 20.0,
    },
    "gpt-5.6-terra": {
        "uncached_input": 2.0,
        "cached_input": 0.2,
        "cache_write": 2.5,
        "output": 12.0,
    },
    "gpt-5.6-luna": {
        "uncached_input": 0.2,
        "cached_input": 0.02,
        "cache_write": 0.25,
        "output": 1.2,
    },
}
_LONG_CONTEXT_INPUT_THRESHOLD = 272_000


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
        model: str,
        reasoning_effort: str,
    ) -> dict:
        lock = _CHECKOUT_LOCKS[child_id.int % len(_CHECKOUT_LOCKS)]
        with lock:
            return self._create_child_locked(
                parent_id,
                child_id,
                role,
                task_key,
                mission,
                capability_ref,
                capabilities,
                model,
                reasoning_effort,
            )

    def _create_child_locked(
        self,
        parent_id: uuid.UUID,
        child_id: uuid.UUID,
        role: str,
        task_key: str,
        mission: dict,
        capability_ref: str,
        capabilities: frozenset[str],
        model: str,
        reasoning_effort: str,
    ) -> dict:
        mission_text = "MISSION\n" + json.dumps(mission, sort_keys=True, separators=(",", ":"))
        try:
            existing = self._request("GET", f"/api/conversations/{child_id}")
            self._validate_existing_child(existing, parent_id, child_id, role, task_key, model, reasoning_effort, capabilities)
            if self._has_user_message(child_id, mission_text):
                self._switch_and_verify_model(child_id, model)
                self._validate_existing_checkout(
                    self._checkout_path(child_id), mission.get("checkout"), exact=False
                )
                return {"conversationUrl": f"{self.public_url.rstrip('/')}/conversations/{child_id}", "provider": "openhands", "created": False}
            created = False
        except ProviderError as exc:
            if exc.status != 404:
                raise
            self._ensure_checkout(child_id, mission.get("checkout"))
            created = True
            profiles = self._request("GET", "/api/agent-profiles")
            profile_id = profiles.get("active_agent_profile_id")
            if not isinstance(profile_id, str) or not profile_id:
                raise ProviderError("OpenHands has no active Agent Profile")
            payload = {
                "conversation_id": str(child_id),
                "agent_profile_id": profile_id,
                "workspace": {"working_dir": str(self._checkout_path(child_id))},
                "tags": {"project": "evex-u", "evexrole": "role-child", "evextask": task_key, "evexparent": str(parent_id), "evexchildrole": role, "evexmodel": model, "evexreasoning": reasoning_effort, "evexcaps": ",".join(sorted(capabilities)) or "none"},
                "autotitle": False,
                "max_iterations": 300,
                # Role MCPs are materialized by the isolated Codex wrapper. An empty
                # override prevents broad profile-level MCPs leaking into this Child.
                "mcp_config": {},
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
                    "EVEX_AGENT_MESSAGING_CAPABILITY": {
                        "kind": "StaticSecret",
                        "value": capability_ref,
                    },
                    "EVEX_REASONING_EFFORT": {
                        "kind": "StaticSecret",
                        "value": reasoning_effort,
                    },
                    "EVEX_AGENT_CAPABILITIES": {
                        "kind": "StaticSecret",
                        "value": ",".join(sorted(capabilities)),
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
                    "pre_tool_use": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"test -f {shlex.quote(str(self._admission_marker(child_id)))}",
                                    "timeout": 2,
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
                                    "command": (
                                        f"if test -f {shlex.quote(str(self._admission_marker(child_id)))}; "
                                        f"then {self._completion_hook_command()}; fi"
                                    ),
                                    "timeout": 50,
                                    "async": False,
                                }
                            ],
                        }
                    ]
                },
            }
            try:
                self._request("POST", "/api/conversations", payload)
            except ProviderError as exc:
                if exc.status != 409:
                    raise
                existing = self._request("GET", f"/api/conversations/{child_id}")
                self._validate_existing_child(
                    existing, parent_id, child_id, role, task_key, model, reasoning_effort, capabilities
                )
                created = False
            if created:
                role_title = role.replace("-", " ").replace("_", " ").title()
                self._request(
                    "PATCH",
                    f"/api/conversations/{child_id}",
                    {"title": f"EVEX | {role_title} | {task_key}"},
                )
        self._switch_and_verify_model(child_id, model)
        self._validate_existing_checkout(
            self._checkout_path(child_id), mission.get("checkout"), exact=True
        )
        marker = self._admission_marker(child_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(str(mission["checkout"]["headSha"]) + "\n")
        temporary.replace(marker)
        self._request(
            "POST",
            f"/api/conversations/{child_id}/events",
            {
                "role": "user",
                "content": [{"type": "text", "text": mission_text}],
                "run": True,
            },
        )
        return {
            "conversationUrl": f"{self.public_url.rstrip('/')}/conversations/{child_id}",
            "provider": "openhands",
            "created": created,
        }

    def _switch_and_verify_model(self, child_id: uuid.UUID, model: str) -> None:
        self._request(
            "POST", f"/api/conversations/{child_id}/switch_acp_model", {"model": model}
        )
        current = self._request("GET", f"/api/conversations/{child_id}")
        if current.get("current_model_id") != model:
            raise ProviderError("OpenHands Child model verification failed")

    def _checkout_path(self, child_id: uuid.UUID) -> Path:
        return Path(self.workspace_root).resolve() / f"child-{child_id}"

    def _admission_marker(self, child_id: uuid.UUID) -> Path:
        return Path(self.workspace_root).resolve().parent / ".evex-admission" / f"{child_id}.ready"

    def _has_user_message(self, child_id: uuid.UUID, expected_text: str) -> bool:
        events = self._request(
            "GET",
            f"/api/conversations/{child_id}/events/search?limit=100&sort_order=TIMESTAMP_DESC",
        )
        for event in events.get("items", []):
            if not isinstance(event, dict) or event.get("kind") != "MessageEvent" or event.get("source") != "user":
                continue
            message = event.get("llm_message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list) and any(
                isinstance(item, dict) and item.get("type") == "text" and item.get("text") == expected_text
                for item in content
            ):
                return True
        return False

    def _validate_existing_child(
        self,
        value: dict,
        parent_id: uuid.UUID,
        child_id: uuid.UUID,
        role: str,
        task_key: str,
        model: str,
        reasoning_effort: str,
        capabilities: frozenset[str],
    ) -> None:
        tags = value.get("tags")
        workspace = value.get("workspace")
        expected_tags = {
            "project": "evex-u",
            "evexrole": "role-child",
            "evextask": task_key,
            "evexparent": str(parent_id),
            "evexchildrole": role,
            "evexmodel": model,
            "evexreasoning": reasoning_effort,
            "evexcaps": ",".join(sorted(capabilities)) or "none",
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
                    "--no-track",
                    "--no-guess-remote",
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
            detail = getattr(exc, "stderr", "")
            if isinstance(detail, bytes):
                detail = detail.decode(errors="replace")
            detail = " ".join(str(detail or "").split()).removeprefix("fatal: ")[:240]
            message = "Child checkout provisioning failed"
            if detail:
                message += f": {detail}"
            raise ProviderError(message) from exc
        # `git clone --mirror` sets this on the shared remote. Once the mirror
        # backs writable worktrees, keeping it true breaks ordinary explicit
        # branch pushes and makes bounded fetches include mirror refspecs.
        self._git(path, "config", "remote.origin.mirror", "false")

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

    def _completion_hook_command(self) -> str:
        body = '{"capabilityRef":"$EVEX_AGENT_MESSAGING_CAPABILITY"}'
        return (
            "curl --fail --silent --show-error --retry 2 --retry-delay 2 "
            "--retry-all-errors --max-time 45 --header 'Content-Type: application/json' "
            f"--data \"{body.replace(chr(34), chr(92) + chr(34))}\" "
            f"{shlex.quote(self.completion_hook_url)}"
        )

    def wait_until_terminal(self, target_id: uuid.UUID) -> str:
        path = f"/api/conversations/{target_id}"
        for _ in range(300):
            status = self._request("GET", path).get("execution_status")
            if status in {"finished", "error", "stuck"}:
                return status
            self.sleeper(0.1)
        raise ProviderError("OpenHands Child did not reach terminal state")

    def terminal_response(self, target_id: uuid.UUID) -> str:
        events = self._request(
            "GET",
            f"/api/conversations/{target_id}/events/search?limit=100&sort_order=TIMESTAMP_DESC",
        )
        for event in events.get("items", []):
            if not isinstance(event, dict):
                continue
            action = event.get("action")
            if (
                event.get("kind") == "ActionEvent"
                and isinstance(action, dict)
                and action.get("kind") == "FinishAction"
                and isinstance(action.get("message"), str)
                and action["message"].strip()
            ):
                text = action["message"].strip()
                if len(text) > 20000:
                    raise ProviderError("OpenHands Child terminal response is too large")
                return text
        raise ProviderError("OpenHands Child terminal response is unavailable")

    def terminal_recovery(self, target_id: uuid.UUID) -> dict:
        """Return only terminal evidence suitable for one fallback callback."""
        status = self._request(
            "GET", f"/api/conversations/{target_id}"
        ).get("execution_status")
        if status == "finished":
            return {"status": status, "terminalResponse": self.terminal_response(target_id)}
        if status not in {"error", "stuck"}:
            raise ProviderError("OpenHands Child is not terminal")
        events = self._request(
            "GET",
            f"/api/conversations/{target_id}/events/search?limit=100&sort_order=TIMESTAMP_DESC",
        )
        terminal_error = {"kind": "terminal-status", "status": status}
        for event in events.get("items", []):
            if not isinstance(event, dict) or event.get("kind") != "ConversationErrorEvent":
                continue
            fields = {}
            for source, target, limit in (("code", "code", 200), ("detail", "message", 2000)):
                value = event.get(source)
                if isinstance(value, str) and value.strip():
                    fields[target] = value.strip()[:limit]
            if fields:
                terminal_error = {"kind": "conversation-error", "status": status, **fields}
                break
        return {"status": status, "terminalError": terminal_error}

    def parent_callback_succeeded(self, target_id: uuid.UUID) -> bool:
        """Use provider event evidence to suppress a redundant Stop-hook recovery wake."""
        for attempt in range(3):
            events = self._request(
                "GET",
                f"/api/conversations/{target_id}/events/search?limit=100&sort_order=TIMESTAMP_DESC",
            )
            for event in events.get("items", []):
                if not isinstance(event, dict):
                    continue
                if event.get("kind") == "MessageEvent" and event.get("source") == "user":
                    break
                if (
                    event.get("kind") != "ACPToolCallEvent"
                    or event.get("title") != "mcp.evex_agent_messaging.send_to_parent"
                    or event.get("status") != "completed"
                ):
                    continue
                output = event.get("raw_output")
                if not isinstance(output, dict) or output.get("error") is not None:
                    continue
                result = output.get("result")
                structured = result.get("structuredContent") if isinstance(result, dict) else None
                if isinstance(structured, dict) and structured.get("accepted") is True:
                    return True
            if attempt < 2:
                self.sleeper(0.2)
        return False

    def usage(self, target_id: uuid.UUID) -> dict:
        """Return stateless token and official Standard API-equivalent cost evidence."""
        conversation = self._request("GET", f"/api/conversations/{target_id}")
        tags = conversation.get("tags")
        model = tags.get("evexmodel") if isinstance(tags, dict) else None
        model = model or conversation.get("current_model_id")
        reasoning_effort = tags.get("evexreasoning") if isinstance(tags, dict) else None
        if model not in _STANDARD_PRICES_PER_MILLION:
            raise ProviderError("OpenHands usage model is unsupported")
        if not isinstance(reasoning_effort, str) or not reasoning_effort:
            agent = conversation.get("agent")
            llm = agent.get("llm") if isinstance(agent, dict) else None
            reasoning_effort = llm.get("reasoning_effort") if isinstance(llm, dict) else None
        if not isinstance(reasoning_effort, str) or not reasoning_effort:
            raise ProviderError("OpenHands usage reasoning effort is unavailable")
        stats = conversation.get("stats")
        usage_to_metrics = stats.get("usage_to_metrics") if isinstance(stats, dict) else None
        if not isinstance(usage_to_metrics, dict) or not usage_to_metrics:
            raise ProviderError("OpenHands usage statistics are unavailable")

        def token(value: dict, key: str) -> int:
            raw = value.get(key, 0)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ProviderError("OpenHands usage statistics are invalid")
            return raw

        tokens = {
            "uncachedInput": 0,
            "cachedInput": 0,
            "cacheWrite": 0,
            "output": 0,
            "reasoning": 0,
        }
        prices = _STANDARD_PRICES_PER_MILLION[model]
        estimate = 0.0
        long_context_turns = 0
        for metrics in usage_to_metrics.values():
            accumulated = metrics.get("accumulated_token_usage") if isinstance(metrics, dict) else None
            turns = metrics.get("token_usages") if isinstance(metrics, dict) else None
            if not isinstance(accumulated, dict) or not isinstance(turns, list):
                raise ProviderError("OpenHands usage statistics are invalid")
            tokens["uncachedInput"] += token(accumulated, "prompt_tokens")
            tokens["cachedInput"] += token(accumulated, "cache_read_tokens")
            tokens["cacheWrite"] += token(accumulated, "cache_write_tokens")
            tokens["output"] += token(accumulated, "completion_tokens")
            tokens["reasoning"] += token(accumulated, "reasoning_tokens")
            for turn in turns:
                if not isinstance(turn, dict):
                    raise ProviderError("OpenHands usage statistics are invalid")
                uncached = token(turn, "prompt_tokens")
                cached = token(turn, "cache_read_tokens")
                cache_write = token(turn, "cache_write_tokens")
                output = token(turn, "completion_tokens")
                is_long = uncached + cached + cache_write > _LONG_CONTEXT_INPUT_THRESHOLD
                if is_long:
                    long_context_turns += 1
                input_multiplier = 2.0 if is_long else 1.0
                output_multiplier = 1.5 if is_long else 1.0
                estimate += (
                    uncached * prices["uncached_input"] * input_multiplier
                    + cached * prices["cached_input"] * input_multiplier
                    + cache_write * prices["cache_write"] * input_multiplier
                    + output * prices["output"] * output_multiplier
                ) / 1_000_000
        denominator = tokens["uncachedInput"] + tokens["cachedInput"]
        if tokens["reasoning"] > tokens["output"]:
            raise ProviderError("OpenHands usage reasoning tokens exceed output tokens")
        cache_hit_rate = tokens["cachedInput"] / denominator if denominator else 0.0
        return {
            "conversationId": str(target_id),
            "model": model,
            "reasoningEffort": reasoning_effort,
            "tokens": tokens,
            "cacheHitRate": round(cache_hit_rate, 6),
            "officialApiEquivalentUsd": round(estimate, 8),
            "longContextTurns": long_context_turns,
            "pricing": {
                "serviceTier": "standard",
                "asOf": "2026-08-23",
                "source": "https://developers.openai.com/api/docs/pricing",
                "perMillionTokensUsd": prices,
            },
            "disclaimer": "Official Standard API-equivalent estimate; not a subscription invoice.",
        }

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

    def resume_mission(self, target_id: uuid.UUID, message_key: str, task_key: str, context: dict) -> dict:
        envelope = {"messageKey": message_key, "taskKey": task_key, "context": context}
        self._request("POST", f"/api/conversations/{target_id}/events", {"role": "user", "content": [{"type": "text", "text": "RESUME_MISSION\n" + json.dumps(envelope, sort_keys=True, separators=(",", ":"))}], "run": True})
        return {"accepted": True, "messageKey": message_key, "taskKey": task_key}
