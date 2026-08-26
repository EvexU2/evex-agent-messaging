"""Narrow GitHub adapter for the one trusted callback recovery comment."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from github import Auth, GithubException, GithubIntegration


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
    """Use one on-demand repository-scoped App token per convergence attempt."""

    def __init__(self, token_provider, bot_login: str, *, timeout: float = 5.0) -> None:
        if not callable(token_provider) or not isinstance(bot_login, str) or not bot_login:
            raise ValueError("fallback GitHub App configuration is required")
        self._token_provider = token_provider
        self._bot_login = bot_login
        self._timeout = timeout

    def converge_callback(self, issue_url: str, body: str) -> dict[str, bool]:
        match = _ISSUE_URL.fullmatch(issue_url)
        if match is None or not isinstance(body, str) or not body.startswith(_PREFIX) or "\n" in body:
            raise CallbackFallbackError("CALLBACK_FALLBACK_NOT_AUTHORIZED")
        try:
            token = self._token_provider()
        except CallbackFallbackError:
            raise
        except Exception as exc:
            raise CallbackFallbackError(CALLBACK_FALLBACK_RETRYABLE_ERROR) from exc
        if not isinstance(token, str) or not token:
            raise CallbackFallbackError(CALLBACK_FALLBACK_RETRYABLE_ERROR)
        path = f"/repos/EvexU2/evex-u-workspace/issues/{match.group(1)}/comments"
        replayed = self._classify(self._comments(path, token), body)
        if replayed:
            return {"accepted": True, "replayed": True}
        self._request("POST", path, {"body": body}, token=token)
        if not self._classify(self._comments(path, token), body):
            raise CallbackFallbackError("CALLBACK_FALLBACK_CONFLICT")
        return {"accepted": True, "replayed": False}

    def _comments(self, path: str, token: str) -> list[dict]:
        value, headers = self._request("GET", path + "?per_page=100", token=token)
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

    def _request(
        self, method: str, path: str, body: dict | None = None, *, token: str
    ):
        request = urllib.request.Request(
            "https://api.github.com" + path,
            data=json.dumps(body, separators=(",", ":")).encode() if body is not None else None,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
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


class GitHubAppInstallationTokenProvider:
    """Mint a short-lived installation token for each bounded fallback attempt."""

    def __init__(
        self,
        app_id: int,
        installation_id: int,
        private_key: str,
        *,
        timeout: int = 5,
    ) -> None:
        if (
            not isinstance(app_id, int)
            or isinstance(app_id, bool)
            or app_id < 1
            or not isinstance(installation_id, int)
            or isinstance(installation_id, bool)
            or installation_id < 1
            or not isinstance(private_key, str)
            or not private_key.strip()
        ):
            raise ValueError("fallback GitHub App configuration is required")
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key = private_key
        self._timeout = timeout

    def _integration(self):
        return GithubIntegration(
            auth=Auth.AppAuth(self._app_id, self._private_key),
            timeout=self._timeout,
            retry=0,
            per_page=100,
            seconds_between_requests=0,
            seconds_between_writes=0,
        )

    @staticmethod
    def _error(exc: Exception) -> CallbackFallbackError:
        code = (
            "CALLBACK_FALLBACK_NOT_AUTHORIZED"
            if isinstance(exc, GithubException) and exc.status in {401, 403, 404}
            else CALLBACK_FALLBACK_RETRYABLE_ERROR
        )
        return CallbackFallbackError(code)

    def preflight(self, expected_login: str) -> None:
        """Prove App identity, installation, repository selection, and Issues-write scope."""
        integration = None
        github = None
        try:
            integration = self._integration()
            app = integration.get_app()
            if f"{getattr(app, 'slug', '')}[bot]" != expected_login:
                raise CallbackFallbackError("CALLBACK_FALLBACK_NOT_AUTHORIZED")
            github = integration.get_github_for_installation(
                self._installation_id, permissions={"issues": "write"}
            )
            repository = github.get_repo("EvexU2/evex-u-workspace")
            if getattr(repository, "full_name", None) != "EvexU2/evex-u-workspace":
                raise CallbackFallbackError("CALLBACK_FALLBACK_NOT_AUTHORIZED")
        except CallbackFallbackError:
            raise
        except Exception as exc:
            raise self._error(exc) from exc
        finally:
            if github is not None:
                github.close()
            if integration is not None:
                integration.close()

    def __call__(self) -> str:
        integration = None
        try:
            integration = self._integration()
            authorization = integration.get_access_token(
                self._installation_id, permissions={"issues": "write"}
            )
            token = authorization.token
            if not isinstance(token, str) or not token:
                raise CallbackFallbackError(CALLBACK_FALLBACK_RETRYABLE_ERROR)
            return token
        except CallbackFallbackError:
            raise
        except Exception as exc:
            raise self._error(exc) from exc
        finally:
            if integration is not None:
                integration.close()
