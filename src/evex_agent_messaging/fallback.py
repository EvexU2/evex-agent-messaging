"""Narrow GitHub adapter for the one trusted callback recovery comment."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request


class CallbackFallbackError(RuntimeError):
    """A bounded, credential-free callback fallback outcome."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


CALLBACK_FALLBACK_RETRYABLE_MCP_ERROR_CODE = -32002
CALLBACK_FALLBACK_RETRYABLE_ERROR = "CALLBACK_FALLBACK_RETRYABLE"


_ISSUE_URL = re.compile(r"https://github\.com/EvexU2/evex-u-workspace/issues/([1-9][0-9]*)")
_PREFIX = "@evexubot callback recovery for "
_MAX_GITHUB_RESPONSE_BYTES = 1_048_576
CALLBACK_FALLBACK_MUTATION = (
    "Post exactly one GitHub Issue comment '@evexubot callback recovery for "
    "<Child Conversation URL> (<taskKey>)' on <owning Issue URL> only after the initial "
    "send_to_parent attempt and two byte-identical retries return retryable transport failures."
)


def materialize_callback_fallback_mutation(mission: dict, conversation_url: str) -> dict:
    """Replace the one canonical Mission template with immutable provider facts."""
    copied = json.loads(json.dumps(mission, separators=(",", ":")))
    mutations = copied.get("allowedMutations")
    links = copied.get("links")
    task_key = copied.get("taskKey")
    issue_url = links.get("issue") if isinstance(links, dict) else None
    if (
        not isinstance(mutations, list)
        or mutations.count(CALLBACK_FALLBACK_MUTATION) != 1
        or _ISSUE_URL.fullmatch(issue_url or "") is None
        or not isinstance(task_key, str)
        or not isinstance(conversation_url, str)
    ):
        return copied
    exact = (
        CALLBACK_FALLBACK_MUTATION
        .replace("<Child Conversation URL>", conversation_url)
        .replace("<taskKey>", task_key)
        .replace("<owning Issue URL>", issue_url)
    )
    copied["allowedMutations"] = [exact if item == CALLBACK_FALLBACK_MUTATION else item for item in mutations]
    return copied


class GitHubCallbackFallbackAdapter:
    """Use one repository-scoped App token for one convergent comment shape."""

    def __init__(self, token: str, bot_login: str, *, timeout: float = 5.0) -> None:
        if not isinstance(token, str) or not token or not isinstance(bot_login, str) or not bot_login:
            raise ValueError("fallback GitHub App configuration is required")
        self._token = token
        self._bot_login = bot_login
        self._timeout = timeout

    def converge_callback(self, issue_url: str, body: str) -> dict[str, bool]:
        match = _ISSUE_URL.fullmatch(issue_url)
        if match is None or not isinstance(body, str) or not body.startswith(_PREFIX) or "\n" in body:
            raise CallbackFallbackError("CALLBACK_FALLBACK_NOT_AUTHORIZED")
        path = f"/repos/EvexU2/evex-u-workspace/issues/{match.group(1)}/comments"
        replayed = self._classify(self._comments(path), body)
        if replayed:
            return {"accepted": True, "replayed": True}
        self._request("POST", path, {"body": body})
        if not self._classify(self._comments(path), body):
            raise CallbackFallbackError("CALLBACK_FALLBACK_CONFLICT")
        return {"accepted": True, "replayed": False}

    def _comments(self, path: str) -> list[dict]:
        value, headers = self._request("GET", path + "?per_page=100")
        if not isinstance(value, list) or len(value) >= 100 or headers.get("Link"):
            raise CallbackFallbackError("CALLBACK_FALLBACK_CONFLICT")
        if not all(isinstance(comment, dict) for comment in value):
            raise CallbackFallbackError("CALLBACK_FALLBACK_CONFLICT")
        return value

    def _classify(self, comments: list[dict], body: str) -> bool:
        exact = []
        for comment in comments:
            comment_body = comment.get("body")
            user = comment.get("user")
            login = user.get("login") if isinstance(user, dict) else None
            if not isinstance(comment_body, str):
                raise CallbackFallbackError("CALLBACK_FALLBACK_CONFLICT")
            if comment_body.startswith(_PREFIX) and comment_body != body:
                raise CallbackFallbackError("CALLBACK_FALLBACK_CONFLICT")
            if comment_body == body:
                if login != self._bot_login:
                    raise CallbackFallbackError("CALLBACK_FALLBACK_CONFLICT")
                exact.append(comment)
        if len(exact) > 1:
            raise CallbackFallbackError("CALLBACK_FALLBACK_CONFLICT")
        return len(exact) == 1

    def _request(self, method: str, path: str, body: dict | None = None):
        request = urllib.request.Request(
            "https://api.github.com" + path,
            data=json.dumps(body, separators=(",", ":")).encode() if body is not None else None,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read(_MAX_GITHUB_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_GITHUB_RESPONSE_BYTES:
                    raise CallbackFallbackError("CALLBACK_FALLBACK_CONFLICT")
                return (json.loads(raw) if raw else {}, dict(response.headers.items()))
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 404}:
                raise CallbackFallbackError("CALLBACK_FALLBACK_NOT_AUTHORIZED") from exc
            if 400 <= exc.code < 500:
                raise CallbackFallbackError("CALLBACK_FALLBACK_CONFLICT") from exc
            raise CallbackFallbackError("CALLBACK_FALLBACK_RETRYABLE") from exc
        except CallbackFallbackError:
            raise
        except (OSError, ValueError, UnicodeError) as exc:
            raise CallbackFallbackError("CALLBACK_FALLBACK_RETRYABLE") from exc
