"""Provider adapter. Only this module knows OpenHands REST details."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import http.client
import json
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class ProviderError(RuntimeError):
    """Provider failure translated away from raw credentials/HTTP details."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


_CHECKOUT_LOCKS = tuple(threading.RLock() for _ in range(64))
_NATIVE_CANCELLED = "cancelled"
_RESULT_TERMINAL_STATES = frozenset({"finished", "error", "stuck"})
_CONTROL_HISTORY_PAGE_SIZE = 100
_CONTROL_HISTORY_MAX_PAGES = 8
_CONTROL_HISTORY_MAX_TEXTS = 800
_CONTROL_HISTORY_MAX_TEXT_BYTES = 1_000_000
_CONTROL_HISTORY_MAX_CURSOR_BYTES = 1_024
_CONTROL_HISTORY_MAX_RESPONSE_BYTES = 1_048_576
_CALLBACK_GENERATION = re.compile(r"^evxg1_[0-9a-f]{64}$")
_CONTROL_SIGNATURE = re.compile(r"^evxs1_[0-9a-f]{64}$")
_CONTROL_TEXT_MAX_BYTES = 100_000
_CONTROL_TASK_MAX_BYTES = 200
_CONTROL_KEY_MAX_BYTES = 200

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
_WORKSPACE_ISSUE_URL = re.compile(
    r"https://github\.com/EvexU2/evex-u-workspace/issues/([1-9][0-9]*)"
)
_ISSUE_TASK_KEY = re.compile(r"(?:^|-)issue-([1-9][0-9]*)(?:-|$)")
_ROLE_TITLES = {
    "spec": "Spec",
    "plan-author": "Plan",
    "writer": "Implement",
    "reviewer": "Review",
    "qa": "QA",
    "repair": "Repair",
}


def _compact_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass
class OpenHandsProvider:
    base_url: str
    api_key: str
    public_url: str
    timeout: float = 5.0
    sleeper: object = time.sleep
    workspace_root: str = "/home/openhands/workspace/delivery"
    write_mission_admission_paused: bool = False

    def _request(
        self, method: str, path: str, body: dict | None = None, *, timeout: float | None = None
    ) -> dict:
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=json.dumps(body, separators=(",", ":")).encode() if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json", "X-Session-API-Key": self.api_key},
        )
        created = True
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout if timeout is None else timeout
            ) as response:
                declared_size = response.headers.get("Content-Length")
                expected_size = None
                if isinstance(declared_size, str):
                    if not declared_size.isdecimal():
                        raise ProviderError("OpenHands response is invalid") from None
                    expected_size = int(declared_size)
                    if expected_size > _CONTROL_HISTORY_MAX_RESPONSE_BYTES:
                        raise ProviderError("OpenHands response exceeds bounded byte budget")
                try:
                    raw = response.read(_CONTROL_HISTORY_MAX_RESPONSE_BYTES + 1)
                except http.client.IncompleteRead as exc:
                    raise ProviderError("OpenHands response is truncated") from exc
                if not isinstance(raw, bytes) or len(raw) > _CONTROL_HISTORY_MAX_RESPONSE_BYTES:
                    raise ProviderError("OpenHands response exceeds bounded byte budget")
                if expected_size is not None and len(raw) != expected_size:
                    raise ProviderError("OpenHands response is truncated")
                value = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise ProviderError("OpenHands messaging transport failed", status=exc.code) from exc
        except OSError as exc:
            raise ProviderError("OpenHands messaging transport failed") from exc
        if not isinstance(value, dict):
            raise ProviderError("OpenHands returned an invalid response")
        return value

    def readiness(self) -> bool:
        """Perform the single, bounded read required by the readiness probe."""
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.base_url, self.api_key, self.public_url)
        ):
            return False
        try:
            profiles = self._request("GET", "/api/agent-profiles", timeout=15.0)
        except (ProviderError, TypeError, ValueError):
            return False
        if not isinstance(profiles, dict):
            return False
        profile_id = profiles.get("active_agent_profile_id")
        return isinstance(profile_id, str) and bool(profile_id)

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
        if role in {"spec", "writer"} and self.write_mission_admission_paused:
            raise ProviderError("write_mission_admission_paused")
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
        delivered_mission = {
            **mission,
            "callbackGeneration": self._initial_callback_generation(
                parent_id, child_id, task_key
            ),
        }
        delivered_mission = self._signed_control_envelope("MISSION", delivered_mission)
        mission_text = "MISSION\n" + json.dumps(
            delivered_mission, sort_keys=True, separators=(",", ":")
        )
        try:
            existing = self._request("GET", f"/api/conversations/{child_id}")
            self._validate_existing_child(existing, parent_id, child_id, role, task_key, model, reasoning_effort, capabilities)
            if self._has_user_message(child_id, mission_text):
                self._switch_and_verify_model(child_id, model)
                self._validate_existing_checkout(
                    self._checkout_path(child_id), mission.get("checkout"), exact=False
                )
                return {"conversationUrl": f"{self.public_url.rstrip('/')}/conversations/{child_id}", "provider": "openhands", "created": False}
            # Do not append a second Mission to a recovered Child when its prior
            # continuation evidence cannot prove the new generation contract.
            self._current_callback_generation(child_id, parent_id, task_key)
            self._switch_and_verify_model(child_id, model)
            self._validate_existing_checkout(
                self._checkout_path(child_id), mission.get("checkout"), exact=False
            )
            return {"conversationUrl": f"{self.public_url.rstrip('/')}/conversations/{child_id}", "provider": "openhands", "created": False}
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
                if self._has_user_message(child_id, mission_text):
                    self._switch_and_verify_model(child_id, model)
                    self._validate_existing_checkout(
                        self._checkout_path(child_id), mission.get("checkout"), exact=False
                    )
                    return {"conversationUrl": f"{self.public_url.rstrip('/')}/conversations/{child_id}", "provider": "openhands", "created": False}
                try:
                    self._current_callback_generation(child_id, parent_id, task_key)
                except ProviderError:
                    if self._has_any_mission_event(child_id):
                        raise
                else:
                    self._switch_and_verify_model(child_id, model)
                    self._validate_existing_checkout(
                        self._checkout_path(child_id), mission.get("checkout"), exact=False
                    )
                    return {"conversationUrl": f"{self.public_url.rstrip('/')}/conversations/{child_id}", "provider": "openhands", "created": False}
                created = False
            if created:
                self._request(
                    "PATCH",
                    f"/api/conversations/{child_id}",
                    {"title": self._conversation_title(role, task_key, mission)},
                )
        self._switch_and_verify_model(child_id, model)
        self._validate_existing_checkout(
            self._checkout_path(child_id), mission.get("checkout"), exact=True
        )
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

    @staticmethod
    def _conversation_title(role: str, task_key: str, mission: dict) -> str:
        role_title = _ROLE_TITLES[role]
        links = mission.get("links")
        issue_url = links.get("issue") if isinstance(links, dict) else None
        issue_match = (
            _WORKSPACE_ISSUE_URL.fullmatch(issue_url)
            if isinstance(issue_url, str)
            else None
        )
        task_match = _ISSUE_TASK_KEY.search(task_key)
        issue = issue_match.group(1) if issue_match else (
            task_match.group(1) if task_match else None
        )
        prefix = f"#{issue} · {role_title}" if issue else role_title
        display_title = mission.get("displayTitle")
        if not isinstance(display_title, str):
            return prefix
        subject = " ".join(display_title.split())
        if len(subject) > 60:
            subject = subject[:59].rstrip() + "…"
        return f"{prefix} · {subject}" if subject else prefix

    def _switch_and_verify_model(self, child_id: uuid.UUID, model: str) -> None:
        self._request(
            "POST", f"/api/conversations/{child_id}/switch_acp_model", {"model": model}
        )
        current = self._request("GET", f"/api/conversations/{child_id}")
        if current.get("current_model_id") != model:
            raise ProviderError("OpenHands Child model verification failed")

    def _checkout_path(self, child_id: uuid.UUID) -> Path:
        return Path(self.workspace_root).resolve() / f"child-{child_id}"

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

    def _has_any_mission_event(self, child_id: uuid.UUID) -> bool:
        return any(text.startswith("MISSION\n") for text in self._event_texts(child_id))

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

    def wait_until_terminal(self, target_id: uuid.UUID) -> str:
        path = f"/api/conversations/{target_id}"
        for _ in range(300):
            status = self._request("GET", path).get("execution_status")
            if status in _RESULT_TERMINAL_STATES | {_NATIVE_CANCELLED}:
                return status
            self.sleeper(0.1)
        raise ProviderError("OpenHands Child did not reach terminal state")

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

    def write_mission_inventory(self) -> list[dict]:
        """Return current Spec/Writer facts for the operator cutover procedure."""
        inventory = []
        page_id = None
        seen_page_ids = set()
        while True:
            path = "/api/conversations?limit=100"
            if page_id is not None:
                path += "&page_id=" + urllib.parse.quote(page_id, safe="")
            response = self._request("GET", path)
            if not isinstance(response, dict):
                raise ProviderError("OpenHands write Mission inventory is incomplete")
            values = response.get("items")
            next_page_id = response.get("next_page_id")
            if not isinstance(values, list) or "next_page_id" not in response:
                raise ProviderError("OpenHands write Mission inventory is incomplete")
            if next_page_id is not None and (not isinstance(next_page_id, str) or not next_page_id):
                raise ProviderError("OpenHands write Mission inventory is incomplete")
            for value in values:
                if not isinstance(value, dict):
                    raise ProviderError("OpenHands write Mission inventory is invalid")
                tags = value.get("tags")
                if not isinstance(tags, dict) or tags.get("project") != "evex-u" or tags.get("evexrole") != "role-child":
                    continue
                role = tags.get("evexchildrole")
                if role not in {"spec", "writer"}:
                    continue
                child_id = value.get("id") or value.get("conversation_id")
                owner = tags.get("evexparent")
                task_key = tags.get("evextask")
                status = value.get("execution_status")
                try:
                    child = uuid.UUID(str(child_id))
                    owning_main = uuid.UUID(str(owner))
                except (TypeError, ValueError, AttributeError) as exc:
                    raise ProviderError("OpenHands write Mission inventory is invalid") from exc
                if not isinstance(task_key, str) or not task_key or not isinstance(status, str):
                    raise ProviderError("OpenHands write Mission inventory is invalid")
                inventory.append({"childId": str(child), "owningMainId": str(owning_main), "role": role, "taskKey": task_key, "terminal": status in {"finished", "error", "stuck"}})
            if next_page_id is None:
                break
            if next_page_id in seen_page_ids:
                raise ProviderError("OpenHands write Mission inventory is incomplete")
            seen_page_ids.add(next_page_id)
            page_id = next_page_id
        return sorted(inventory, key=lambda item: item["childId"])

    def request_write_mission_drain(self, mission: dict) -> dict:
        """Route one live write Mission to its owning Main; never control the Child directly."""
        if not isinstance(mission, dict):
            raise ProviderError("write Mission drain target is invalid")
        try:
            child_id = uuid.UUID(str(mission["childId"]))
            owning_main_id = uuid.UUID(str(mission["owningMainId"]))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ProviderError("write Mission drain target is invalid") from exc
        task_key, role = mission.get("taskKey"), mission.get("role")
        if not isinstance(task_key, str) or not task_key or role not in {"spec", "writer"}:
            raise ProviderError("write Mission drain target is invalid")
        current = self._request("GET", f"/api/conversations/{child_id}")
        if self._write_mission_facts(current) != (str(owning_main_id), role, task_key):
            raise ProviderError("write Mission drain target no longer matches live provider facts")
        if current.get("execution_status") in {"finished", "error", "stuck"}:
            return {"accepted": True, "terminal": True, "childId": str(child_id)}
        message_key = f"quiesce:{child_id}"
        envelope = {"childId": str(child_id), "messageKey": message_key, "owningMainId": str(owning_main_id), "role": role, "taskKey": task_key}
        self.send_message(owning_main_id, message_key, "QUIESCE_WRITE_MISSION", json.dumps(envelope, sort_keys=True, separators=(",", ":")))
        return {"accepted": True, "terminal": False, "childId": str(child_id), "messageKey": message_key}

    @staticmethod
    def _write_mission_facts(value: dict) -> tuple[str, str, str] | None:
        tags = value.get("tags") if isinstance(value, dict) else None
        if not isinstance(tags, dict) or tags.get("project") != "evex-u" or tags.get("evexrole") != "role-child":
            return None
        role, owner, task_key = tags.get("evexchildrole"), tags.get("evexparent"), tags.get("evextask")
        if role not in {"spec", "writer"} or not isinstance(owner, str) or not isinstance(task_key, str) or not task_key:
            return None
        return owner, role, task_key

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

    def send_child_message(
        self,
        child_id: uuid.UUID,
        target_id: uuid.UUID,
        message_key: str,
        kind: str,
        text: str,
    ) -> dict:
        """Deliver one Child callback unless native cancellation already won."""
        with _CHECKOUT_LOCKS[child_id.int % len(_CHECKOUT_LOCKS)]:
            status = self._request(
                "GET", f"/api/conversations/{child_id}"
            ).get("execution_status")
            if status == _NATIVE_CANCELLED:
                return {
                    "accepted": False,
                    "messageKey": message_key,
                    "outcome": "CANCELLED",
                }
            try:
                envelope = json.loads(text)
                task_key = envelope["taskKey"]
            except (KeyError, TypeError, ValueError):
                raise ProviderError("OpenHands Child callback envelope is invalid") from None
            if (
                envelope.get("childId") != str(child_id)
                or envelope.get("owningMainId") != str(target_id)
                or envelope.get("messageKey") != message_key
            ):
                raise ProviderError("OpenHands Child callback identity is invalid")
            callback_generation = envelope.get("callbackGeneration")
            if not isinstance(callback_generation, str) or not _CALLBACK_GENERATION.fullmatch(
                callback_generation
            ):
                raise ProviderError("OpenHands Child callback generation is invalid")
            current_generation = self._current_callback_generation(
                child_id, target_id, task_key
            )
            if not hmac.compare_digest(callback_generation, current_generation):
                raise ProviderError("OpenHands Child callback generation is stale")
            result_key = self._terminal_result_key(target_id, child_id, task_key)
            if status in _RESULT_TERMINAL_STATES:
                return self._settled(message_key, task_key, "RESULT")
            if result_key is not None:
                if result_key == message_key:
                    return {"accepted": True, "messageKey": message_key, "outcome": "RESULT"}
                return self._settled(message_key, task_key, "RESULT")
            if kind == "RESULT" and self._has_waiting_input(
                target_id, child_id, task_key
            ):
                return self._settled(message_key, task_key, "NEEDS_INPUT")
            delivered = self._signed_control_envelope(
                kind,
                {
                    "callbackGeneration": callback_generation,
                    "childId": str(child_id),
                    "kind": kind,
                    "messageKey": message_key,
                    "owningMainId": str(target_id),
                    "taskKey": task_key,
                    "text": envelope.get("text"),
                },
            )
            if not self._valid_control_schema(kind, delivered):
                raise ProviderError("OpenHands Child callback envelope is invalid")
            return self.send_message(
                target_id,
                message_key,
                kind,
                _compact_json(delivered),
            )

    def cancel_mission(
        self,
        target_id: uuid.UUID,
        message_key: str,
        task_key: str,
        owning_main_id: uuid.UUID,
    ) -> dict:
        path = f"/api/conversations/{target_id}"
        with _CHECKOUT_LOCKS[target_id.int % len(_CHECKOUT_LOCKS)]:
            current = self._request("GET", path)
            status = current.get("execution_status")
            if status == _NATIVE_CANCELLED:
                return self._cancellation_replay(
                    target_id, task_key, message_key, owning_main_id
                )
            if status in _RESULT_TERMINAL_STATES or self._has_terminal_result(
                owning_main_id, target_id, task_key
            ):
                return self._settled(message_key, task_key, "RESULT")
            if self._has_resume(target_id, task_key, owning_main_id):
                return self._settled(message_key, task_key, "RESUMED")
            if status == "running":
                self._request("POST", f"{path}/interrupt", {})
            for _ in range(20):
                current = self._request("GET", path)
                status = current.get("execution_status")
                if status == _NATIVE_CANCELLED:
                    return self._cancelled(message_key, task_key)
                if status in _RESULT_TERMINAL_STATES:
                    return self._settled(message_key, task_key, "RESULT")
                if status in {"paused", "idle"}:
                    current_generation = self._current_callback_generation(
                        target_id, owning_main_id, task_key
                    )
                    envelope = {
                        "callbackGeneration": current_generation,
                        "childId": str(target_id),
                        "messageKey": message_key,
                        "owningMainId": str(owning_main_id),
                        "targetId": str(target_id),
                        "taskKey": task_key,
                    }
                    envelope = self._signed_control_envelope("CANCEL_MISSION", envelope)
                    self._request(
                        "POST",
                        f"{path}/events",
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "CANCEL_MISSION\n" + _compact_json(envelope)}],
                            "run": True,
                        },
                    )
                    if self.wait_until_terminal(target_id) == _NATIVE_CANCELLED:
                        return self._cancelled(message_key, task_key)
                    return self._settled(message_key, task_key, "RESULT")
                self.sleeper(0.1)
        raise ProviderError("OpenHands Child did not become cancellation-wakeable")

    def resume_mission(
        self,
        target_id: uuid.UUID,
        message_key: str,
        task_key: str,
        context: dict,
        owning_main_id: uuid.UUID,
    ) -> dict:
        with _CHECKOUT_LOCKS[target_id.int % len(_CHECKOUT_LOCKS)]:
            current = self._request("GET", f"/api/conversations/{target_id}")
            if current.get("execution_status") == _NATIVE_CANCELLED:
                return self._settled(message_key, task_key, "CANCELLED")
            if current.get("execution_status") in _RESULT_TERMINAL_STATES:
                return self._settled(message_key, task_key, "RESULT")
            if self.write_mission_admission_paused:
                if self._write_mission_facts(current) is not None:
                    raise ProviderError("write_mission_admission_paused")
            if self._has_terminal_result(owning_main_id, target_id, task_key):
                return self._settled(message_key, task_key, "RESULT")
            current_generation = self._current_callback_generation(
                target_id, owning_main_id, task_key
            )
            if not self._has_waiting_input(owning_main_id, target_id, task_key):
                return self._settled(message_key, task_key, "NEEDS_INPUT")
            envelope = {
                "callbackGeneration": self._resumed_callback_generation(
                    current_generation, message_key
                ),
                "childId": str(target_id),
                "messageKey": message_key,
                "owningMainId": str(owning_main_id),
                "taskKey": task_key,
                "context": context,
            }
            envelope = self._signed_control_envelope("RESUME_MISSION", envelope)
            self._request("POST", f"/api/conversations/{target_id}/events", {"role": "user", "content": [{"type": "text", "text": "RESUME_MISSION\n" + _compact_json(envelope)}], "run": True})
            return {"accepted": True, "messageKey": message_key, "taskKey": task_key, "outcome": "RESUMED"}

    def replacement_cancelled(
        self, target_id: uuid.UUID, task_key: str, message_key: str, owning_main_id: uuid.UUID
    ) -> bool:
        with _CHECKOUT_LOCKS[target_id.int % len(_CHECKOUT_LOCKS)]:
            status = self._request(
                "GET", f"/api/conversations/{target_id}"
            ).get("execution_status")
            return status == _NATIVE_CANCELLED and self._has_cancel(
                target_id, task_key, message_key, owning_main_id
            )

    @staticmethod
    def _cancelled(message_key: str, task_key: str) -> dict:
        return {"accepted": True, "messageKey": message_key, "taskKey": task_key, "outcome": "CANCELLED"}

    @staticmethod
    def _settled(message_key: str, task_key: str, outcome: str) -> dict:
        return {"accepted": False, "messageKey": message_key, "taskKey": task_key, "outcome": outcome}

    def _cancellation_replay(
        self, target_id: uuid.UUID, task_key: str, message_key: str, owning_main_id: uuid.UUID
    ) -> dict:
        if self._has_cancel(target_id, task_key, message_key, owning_main_id):
            return self._cancelled(message_key, task_key)
        return self._settled(message_key, task_key, "CANCELLED")

    def _generation(self, value: dict) -> str:
        payload = _compact_json(value).encode()
        return "evxg1_" + hmac.new(
            self.api_key.encode(), payload, hashlib.sha256
        ).hexdigest()

    def _control_signature(self, kind: str, envelope: dict) -> str:
        unsigned = {key: value for key, value in envelope.items() if key != "controlSignature"}
        payload = _compact_json({"envelope": unsigned, "kind": kind}).encode()
        return "evxs1_" + hmac.new(
            self.api_key.encode(), payload, hashlib.sha256
        ).hexdigest()

    def _signed_control_envelope(self, kind: str, envelope: dict) -> dict:
        signed = dict(envelope)
        signed["controlSignature"] = self._control_signature(kind, signed)
        return signed

    def _valid_control_signature(self, kind: str, envelope: dict) -> bool:
        signature = envelope.get("controlSignature")
        return (
            isinstance(signature, str)
            and _CONTROL_SIGNATURE.fullmatch(signature) is not None
            and hmac.compare_digest(signature, self._control_signature(kind, envelope))
        )

    @staticmethod
    def _bounded_string(value: object, maximum: int) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and len(value.encode()) <= maximum
        )

    def _valid_control_schema(self, kind: str, envelope: dict) -> bool:
        if not isinstance(envelope, dict):
            return False
        common = {
            "callbackGeneration", "childId", "controlSignature", "owningMainId", "taskKey"
        }
        fields = {
            "MISSION": common,
            "RESUME_MISSION": common | {"context", "messageKey"},
            "RESULT": common | {"kind", "messageKey", "text"},
            "NEEDS_INPUT": common | {"kind", "messageKey", "text"},
            "CANCEL_MISSION": common | {"messageKey", "targetId"},
        }.get(kind)
        if fields is None or not fields.issubset(envelope):
            return False
        # A Mission intentionally carries a caller-defined structured payload. Every
        # subsequent control kind is closed to unknown fields.
        if kind != "MISSION" and set(envelope) != fields:
            return False
        try:
            child_id = uuid.UUID(envelope["childId"])
            owning_main_id = uuid.UUID(envelope["owningMainId"])
        except (TypeError, ValueError, AttributeError):
            return False
        if (
            not self._bounded_string(envelope.get("taskKey"), _CONTROL_TASK_MAX_BYTES)
            or not isinstance(envelope.get("callbackGeneration"), str)
            or _CALLBACK_GENERATION.fullmatch(envelope["callbackGeneration"]) is None
            or not isinstance(envelope.get("controlSignature"), str)
            or _CONTROL_SIGNATURE.fullmatch(envelope["controlSignature"]) is None
        ):
            return False
        if kind in {"RESULT", "NEEDS_INPUT"}:
            return (
                envelope.get("kind") == kind
                and self._bounded_string(envelope.get("messageKey"), _CONTROL_KEY_MAX_BYTES)
                and self._bounded_string(envelope.get("text"), _CONTROL_TEXT_MAX_BYTES)
            )
        if kind == "RESUME_MISSION":
            if not self._bounded_string(envelope.get("messageKey"), _CONTROL_KEY_MAX_BYTES):
                return False
            context = envelope.get("context")
            if not isinstance(context, dict) or not context:
                return False
            try:
                return len(_compact_json(context).encode()) <= _CONTROL_TEXT_MAX_BYTES
            except (TypeError, ValueError):
                return False
        if kind == "CANCEL_MISSION":
            try:
                target_id = uuid.UUID(envelope["targetId"])
            except (TypeError, ValueError, AttributeError):
                return False
            return (
                target_id == child_id
                and self._bounded_string(envelope.get("messageKey"), _CONTROL_KEY_MAX_BYTES)
            )
        return True

    @staticmethod
    def _matches_control_identity(
        envelope: dict, child_id: uuid.UUID, owning_main_id: uuid.UUID, task_key: str
    ) -> bool:
        try:
            return (
                uuid.UUID(envelope["childId"]) == child_id
                and uuid.UUID(envelope["owningMainId"]) == owning_main_id
                and envelope.get("taskKey") == task_key
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    def _control_envelopes(self, conversation_id: uuid.UUID, kind: str) -> list[dict]:
        prefix = kind + "\n"
        envelopes = []
        for text in self._event_texts(conversation_id):
            if not text.startswith(prefix):
                continue
            try:
                envelope = json.loads(text.removeprefix(prefix))
            except (TypeError, ValueError):
                continue
            if isinstance(envelope, dict):
                envelopes.append(envelope)
        return envelopes

    def _initial_callback_generation(
        self, owning_main_id: uuid.UUID, child_id: uuid.UUID, task_key: str
    ) -> str:
        return self._generation(
            {
                "childId": str(child_id),
                "kind": "initial",
                "owningMainId": str(owning_main_id),
                "taskKey": task_key,
            }
        )

    def _resumed_callback_generation(self, previous: str, message_key: str) -> str:
        return self._generation(
            {"messageKey": message_key, "previous": previous, "kind": "resume"}
        )

    def _callback_generation_chain(
        self, child_id: uuid.UUID, owning_main_id: uuid.UUID, task_key: str
    ) -> list[str]:
        initial = self._initial_callback_generation(owning_main_id, child_id, task_key)
        missions = []
        resumes = []
        for text in self._event_texts(child_id):
            for kind, values in (("MISSION", missions), ("RESUME_MISSION", resumes)):
                prefix = kind + "\n"
                if not text.startswith(prefix):
                    continue
                try:
                    envelope = json.loads(text.removeprefix(prefix))
                except (TypeError, ValueError):
                    break
                if isinstance(envelope, dict) and self._matches_control_identity(
                    envelope, child_id, owning_main_id, task_key
                ):
                    values.append(envelope)
                break
        if len(missions) != 1:
            raise ProviderError("OpenHands Child callback generation history is ambiguous")
        if (
            not self._valid_control_schema("MISSION", missions[0])
            or not self._valid_control_signature("MISSION", missions[0])
            or not hmac.compare_digest(str(missions[0].get("callbackGeneration")), initial)
        ):
            raise ProviderError("OpenHands Child callback generation history is incomplete")
        generations = [initial]
        for envelope in reversed(resumes):
            message_key = envelope.get("messageKey")
            next_generation = envelope.get("callbackGeneration")
            if (
                not self._valid_control_schema("RESUME_MISSION", envelope)
                or not self._valid_control_signature("RESUME_MISSION", envelope)
                or not isinstance(message_key, str)
                or not isinstance(next_generation, str)
                or not _CALLBACK_GENERATION.fullmatch(next_generation)
                or not hmac.compare_digest(
                    next_generation,
                    self._resumed_callback_generation(generations[-1], message_key),
                )
            ):
                raise ProviderError("OpenHands Child callback generation history is incomplete")
            generations.append(next_generation)
        return generations

    def _current_callback_generation(
        self, child_id: uuid.UUID, owning_main_id: uuid.UUID, task_key: str
    ) -> str:
        return self._callback_generation_chain(child_id, owning_main_id, task_key)[-1]

    def _has_resume(
        self, target_id: uuid.UUID, task_key: str, owning_main_id: uuid.UUID
    ) -> bool:
        resumes = [
            envelope
            for envelope in self._control_envelopes(target_id, "RESUME_MISSION")
            if self._matches_control_identity(
                envelope, target_id, owning_main_id, task_key
            )
        ]
        if resumes:
            self._current_callback_generation(target_id, owning_main_id, task_key)
        return bool(resumes)

    def _has_cancel(
        self, target_id: uuid.UUID, task_key: str, message_key: str, owning_main_id: uuid.UUID
    ) -> bool:
        for envelope in self._control_envelopes(target_id, "CANCEL_MISSION"):
            if (
                self._matches_control_identity(
                    envelope, target_id, owning_main_id, task_key
                )
                and envelope.get("messageKey") == message_key
            ):
                if (
                    not self._valid_control_schema("CANCEL_MISSION", envelope)
                    or not self._valid_control_signature("CANCEL_MISSION", envelope)
                ):
                    raise ProviderError("OpenHands control envelope is invalid")
                generation = envelope.get("callbackGeneration")
                if (
                    not isinstance(generation, str)
                    or not _CALLBACK_GENERATION.fullmatch(generation)
                    or not hmac.compare_digest(
                        generation,
                        self._current_callback_generation(
                            target_id, owning_main_id, task_key
                        ),
                    )
                ):
                    raise ProviderError("OpenHands control history is unauthenticated")
                return True
        return False

    def _has_terminal_result(
        self, owning_main_id: uuid.UUID, child_id: uuid.UUID, task_key: str
    ) -> bool:
        return self._terminal_result_key(owning_main_id, child_id, task_key) is not None

    def _terminal_result_key(
        self, owning_main_id: uuid.UUID, child_id: uuid.UUID, task_key: str
    ) -> str | None:
        for envelope in self._control_envelopes(owning_main_id, "RESULT"):
            if (
                self._matches_control_identity(
                    envelope, child_id, owning_main_id, task_key
                )
                and envelope.get("kind") == "RESULT"
                and isinstance(envelope.get("messageKey"), str)
            ):
                current_generation = self._current_callback_generation(
                    child_id, owning_main_id, task_key
                )
                if (
                    not self._valid_control_schema("RESULT", envelope)
                    or not self._valid_control_signature("RESULT", envelope)
                    or not isinstance(envelope.get("callbackGeneration"), str)
                    or not hmac.compare_digest(
                        envelope["callbackGeneration"], current_generation
                    )
                ):
                    raise ProviderError("OpenHands control history is unauthenticated")
                return envelope["messageKey"]
        return None

    def _has_waiting_input(
        self, owning_main_id: uuid.UUID, child_id: uuid.UUID, task_key: str
    ) -> bool:
        generations = self._callback_generation_chain(
            child_id, owning_main_id, task_key
        )
        current_generation = generations[-1]
        current = []
        for envelope in self._control_envelopes(owning_main_id, "NEEDS_INPUT"):
            if (
                self._matches_control_identity(
                    envelope, child_id, owning_main_id, task_key
                )
                and envelope.get("kind") == "NEEDS_INPUT"
            ):
                if (
                    not self._valid_control_schema("NEEDS_INPUT", envelope)
                    or not self._valid_control_signature("NEEDS_INPUT", envelope)
                    or not isinstance(envelope.get("callbackGeneration"), str)
                ):
                    raise ProviderError("OpenHands control envelope is invalid")
                matches_chain = any(
                    hmac.compare_digest(envelope["callbackGeneration"], generation)
                    for generation in generations
                )
                if not matches_chain:
                    raise ProviderError("OpenHands control history is unauthenticated")
                if hmac.compare_digest(envelope["callbackGeneration"], current_generation):
                    current.append(envelope)
        if len(current) > 1:
            raise ProviderError("OpenHands control history is ambiguous")
        return bool(current)

    def _event_texts(self, conversation_id: uuid.UUID) -> list[str]:
        texts = []
        page_id = None
        seen_page_ids = set()
        text_bytes = 0
        for _ in range(_CONTROL_HISTORY_MAX_PAGES):
            path = (
                f"/api/conversations/{conversation_id}/events/search?limit={_CONTROL_HISTORY_PAGE_SIZE}"
                "&sort_order=TIMESTAMP_DESC&source=user"
            )
            if page_id is not None:
                path += "&page_id=" + urllib.parse.quote(page_id, safe="")
            response = self._request("GET", path)
            values = response.get("items") if isinstance(response, dict) else None
            next_page_id = response.get("next_page_id") if isinstance(response, dict) else None
            if not isinstance(values, list) or len(values) > _CONTROL_HISTORY_PAGE_SIZE or (
                next_page_id is not None
                and (
                    not isinstance(next_page_id, str)
                    or not next_page_id
                    or len(next_page_id.encode()) > _CONTROL_HISTORY_MAX_CURSOR_BYTES
                )
            ):
                raise ProviderError("OpenHands control history is unavailable")
            for event in values:
                message = event.get("llm_message") if isinstance(event, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                for item in content:
                    text = item.get("text") if isinstance(item, dict) else None
                    if not isinstance(text, str):
                        continue
                    text_bytes += len(text.encode())
                    if (
                        len(texts) >= _CONTROL_HISTORY_MAX_TEXTS
                        or text_bytes > _CONTROL_HISTORY_MAX_TEXT_BYTES
                    ):
                        raise ProviderError("OpenHands control history exceeds bounded event budget")
                    texts.append(text)
            if next_page_id is None:
                return texts
            if next_page_id in seen_page_ids:
                raise ProviderError("OpenHands control history is unavailable")
            seen_page_ids.add(next_page_id)
            page_id = next_page_id
        raise ProviderError("OpenHands control history exceeds bounded page budget")
