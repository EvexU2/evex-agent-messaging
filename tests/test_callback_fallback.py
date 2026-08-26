import sys
import unittest
import uuid
import threading
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evex_agent_messaging.capability import main_capability_token
from evex_agent_messaging.service import (
    CALLBACK_FALLBACK_MUTATION,
    CallbackFallbackError,
    MessagingService,
)
from evex_agent_messaging.fallback import (
    GitHubAppInstallationTokenProvider,
    GitHubCallbackFallbackAdapter,
)
from evex_agent_messaging.fallback import materialize_callback_fallback_mutation
from unittest.mock import MagicMock, Mock, patch


class Provider:
    def __init__(self):
        self.mission = None
        self.context = None

    def create_child(self, _parent, child, _role, _task, mission, _token, _caps, _model, _effort):
        self.mission = materialize_callback_fallback_mutation(mission, f"http://canvas/conversations/{child}")
        self.child = child
        return {"created": True, "conversationUrl": f"http://canvas/conversations/{child}"}

    def callback_fallback_context(self, _child, _owning_main, _task_key):
        return self.context

    def send_message(self, *_args): return {"accepted": True}
    def cancel_mission(self, *_args): return {"accepted": True}
    def resume_mission(self, *_args): return {"accepted": True}
    def wait_until_terminal(self, *_args): return "finished"
    def usage(self, *_args): return {}
    def readiness(self): return True


class Adapter:
    def __init__(self): self.calls = []
    def converge_callback(self, issue_url, body):
        self.calls.append((issue_url, body))
        return {"accepted": True, "replayed": False}


class CallbackFallbackTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        self.secret = b"test-secret"
        self.main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        self.provider = Provider()
        self.adapter = Adapter()
        self.service = MessagingService(self.provider, self.secret, clock=lambda: self.now, callback_fallback_adapter=self.adapter)

    def main_token(self):
        return main_capability_token(self.secret, self.main, issued_at=self.now - timedelta(minutes=1), expires_at=self.now + timedelta(hours=1))

    def mission(self):
        return {
            "immediateTask": "Your task now: make the bounded change.",
            "links": {"issue": "https://github.com/EvexU2/evex-u-workspace/issues/732"},
            "checkout": {"repository": "EvexU2/evex-agent-messaging", "branch": "delivery/732", "headSha": "a" * 40},
            "allowedMutations": [CALLBACK_FALLBACK_MUTATION],
            "prohibitions": ["Do not merge"], "skills": ["evex-delivery-writer"], "evidence": ["focused tests"],
        }

    def create(self):
        return self.service.create_child(self.main_token(), "issue-732-writer", "writer", self.mission(), model="gpt-5.6-terra", reasoning_effort="medium")

    def trusted_context(self, child):
        generation = "evxg1_" + "a" * 64
        body = f"@evexubot callback recovery for http://canvas/conversations/{child} (issue-732-writer)"
        return {
            "mission": self.provider.mission,
            "conversationUrl": f"http://canvas/conversations/{child}",
            "currentCallbackGeneration": generation,
            "attempts": [
                {
                    "payload": '{"callbackGeneration":"' + generation + '","outcome":"PARTIAL"}',
                    "callbackGeneration": generation,
                    "outcome": "retryable",
                }
            ] * 3,
            "body": body,
        }

    def test_exact_mission_and_three_identical_retryable_failures_converge(self):
        child = self.create()
        self.provider.context = self.trusted_context(child["childId"])

        result = self.service.send_callback_fallback(child["capabilityRef"])

        self.assertTrue(result["accepted"])
        self.assertEqual(self.adapter.calls, [("https://github.com/EvexU2/evex-u-workspace/issues/732", self.provider.context["body"])])
        self.assertEqual(self.provider.mission["allowedMutations"], [
            "Post exactly one GitHub Issue comment '@evexubot callback recovery for "
            f"http://canvas/conversations/{child['childId']} (issue-732-writer)' on "
            "https://github.com/EvexU2/evex-u-workspace/issues/732 only after the initial "
            "send_to_parent attempt and two byte-identical retries return retryable transport failures."
        ])

    def test_untrusted_or_unexhausted_context_never_calls_github(self):
        child = self.create()
        cases = []
        context = self.trusted_context(child["childId"])
        context["attempts"] = context["attempts"][:2]
        cases.append(("CALLBACK_FALLBACK_NOT_EXHAUSTED", context))
        context = self.trusted_context(child["childId"])
        context["attempts"] = [
            *context["attempts"][:2],
            {
                "payload": '{"callbackGeneration":"' + context["currentCallbackGeneration"] + '","outcome":"DIFFERENT"}',
                "callbackGeneration": context["currentCallbackGeneration"],
                "outcome": "retryable",
            },
        ]
        cases.append(("CALLBACK_FALLBACK_NOT_EXHAUSTED", context))
        context = self.trusted_context(child["childId"])
        context["mission"] = {**context["mission"], "allowedMutations": []}
        cases.append(("CALLBACK_FALLBACK_NOT_AUTHORIZED", context))
        for code, context in cases:
            with self.subTest(code=code):
                self.provider.context = context
                with self.assertRaisesRegex(CallbackFallbackError, code):
                    self.service.send_callback_fallback(child["capabilityRef"])
        self.assertEqual(self.adapter.calls, [])

    def test_read_only_or_no_fallback_capability_cannot_invoke_operation(self):
        mission = self.mission()
        mission["allowedMutations"] = ["commit only"]
        child = self.service.create_child(self.main_token(), "issue-732-readonly", "writer", mission, model="gpt-5.6-terra", reasoning_effort="medium")
        with self.assertRaisesRegex(CallbackFallbackError, "CALLBACK_FALLBACK_NOT_AUTHORIZED"):
            self.service.send_callback_fallback(child["capabilityRef"])
        self.assertEqual(self.adapter.calls, [])

    def test_adapter_rejects_conflicts_before_create_and_replays_one_exact_app_comment(self):
        adapter = GitHubCallbackFallbackAdapter(lambda: "test-token", "messaging-fallback[bot]")
        adapter._request = Mock(return_value=([{"body": "@evexubot callback recovery for near", "user": {"login": "messaging-fallback[bot]"}}], {}))
        with self.assertRaisesRegex(CallbackFallbackError, "CALLBACK_FALLBACK_CONFLICT"):
            adapter.converge_callback("https://github.com/EvexU2/evex-u-workspace/issues/732", "@evexubot callback recovery for http://canvas/conversations/x (task)")
        self.assertEqual(adapter._request.call_count, 1)
        adapter._request = Mock(return_value=([{"body": "@evexubot callback recovery for http://canvas/conversations/x (task)", "user": {"login": "messaging-fallback[bot]"}}], {}))
        self.assertEqual(adapter.converge_callback("https://github.com/EvexU2/evex-u-workspace/issues/732", "@evexubot callback recovery for http://canvas/conversations/x (task)"), {"accepted": True, "replayed": True})
        self.assertEqual(adapter._request.call_count, 1)

    def test_adapter_creates_then_rereads_one_exact_app_comment(self):
        adapter = GitHubCallbackFallbackAdapter(
            lambda: "test-token", "messaging-fallback[bot]"
        )
        body = "@evexubot callback recovery for http://canvas/conversations/x (task)"
        exact = {"body": body, "user": {"login": "messaging-fallback[bot]"}}
        adapter._request = Mock(
            side_effect=[([], {}), ({"id": 1}, {}), ([exact], {})]
        )

        result = adapter.converge_callback(
            "https://github.com/EvexU2/evex-u-workspace/issues/732", body
        )

        self.assertEqual(result, {"accepted": True, "replayed": False})
        self.assertEqual(
            [call.args[0] for call in adapter._request.call_args_list],
            ["GET", "POST", "GET"],
        )

    def test_adapter_recovers_post_response_loss_as_exact_replay(self):
        adapter = GitHubCallbackFallbackAdapter(
            lambda: "test-token", "messaging-fallback[bot]"
        )
        body = "@evexubot callback recovery for http://canvas/conversations/x (task)"
        adapter._request = Mock(
            side_effect=[
                ([], {}),
                CallbackFallbackError("CALLBACK_FALLBACK_RETRYABLE"),
            ]
        )
        with self.assertRaisesRegex(
            CallbackFallbackError, "CALLBACK_FALLBACK_RETRYABLE"
        ):
            adapter.converge_callback(
                "https://github.com/EvexU2/evex-u-workspace/issues/732", body
            )
        adapter._request = Mock(
            return_value=(
                [{"body": body, "user": {"login": "messaging-fallback[bot]"}}],
                {},
            )
        )

        self.assertEqual(
            adapter.converge_callback(
                "https://github.com/EvexU2/evex-u-workspace/issues/732", body
            ),
            {"accepted": True, "replayed": True},
        )

    def test_adapter_rejects_pagination_wrong_author_and_duplicate_exact_comments(self):
        adapter = GitHubCallbackFallbackAdapter(
            lambda: "test-token", "messaging-fallback[bot]"
        )
        body = "@evexubot callback recovery for http://canvas/conversations/x (task)"
        cases = [
            ([{"body": "ordinary", "user": {"login": "someone"}}], {"Link": "next"}),
            ([{"body": body, "user": {"login": "someone"}}], {}),
            (
                [
                    {"body": body, "user": {"login": "messaging-fallback[bot]"}},
                    {"body": body, "user": {"login": "messaging-fallback[bot]"}},
                ],
                {},
            ),
        ]
        for comments, headers in cases:
            with self.subTest(comments=comments, headers=headers):
                adapter._request = Mock(return_value=(comments, headers))
                with self.assertRaisesRegex(
                    CallbackFallbackError, "CALLBACK_FALLBACK_CONFLICT"
                ):
                    adapter.converge_callback(
                        "https://github.com/EvexU2/evex-u-workspace/issues/732",
                        body,
                    )

    def test_adapter_classifies_authentication_failure_as_not_authorized(self):
        adapter = GitHubCallbackFallbackAdapter(
            lambda: "test-token", "messaging-fallback[bot]"
        )
        error = urllib.error.HTTPError(
            "https://api.github.com", 403, "forbidden", {}, None
        )
        with patch(
            "evex_agent_messaging.fallback.urllib.request.urlopen",
            side_effect=error,
        ):
            with self.assertRaisesRegex(
                CallbackFallbackError, "CALLBACK_FALLBACK_NOT_AUTHORIZED"
            ):
                adapter._request(
                    "GET",
                    "/repos/EvexU2/evex-u-workspace/issues/1/comments",
                    token="test-token",
                )

    @patch("evex_agent_messaging.fallback.GithubIntegration")
    @patch("evex_agent_messaging.fallback.Auth.AppAuth")
    def test_app_provider_mints_one_scoped_token_per_attempt(
        self, app_auth, github_integration
    ):
        integration = github_integration.return_value
        authorization = MagicMock(token="installation-token")
        integration.get_access_token.return_value = authorization
        provider = GitHubAppInstallationTokenProvider(123, 456, "private-key")

        self.assertEqual(provider(), "installation-token")

        app_auth.assert_called_once_with(123, "private-key")
        integration.get_access_token.assert_called_once_with(
            456, permissions={"issues": "write"}
        )
        integration.close.assert_called_once_with()

    @patch("evex_agent_messaging.fallback.GithubIntegration")
    @patch("evex_agent_messaging.fallback.Auth.AppAuth")
    def test_app_preflight_proves_identity_repository_and_write_scope(
        self, _app_auth, github_integration
    ):
        integration = github_integration.return_value
        integration.get_app.return_value.slug = "messaging-fallback"
        github = integration.get_github_for_installation.return_value
        github.get_repo.return_value.full_name = "EvexU2/evex-u-workspace"
        provider = GitHubAppInstallationTokenProvider(123, 456, "private-key")

        provider.preflight("messaging-fallback[bot]")

        integration.get_github_for_installation.assert_called_once_with(
            456, permissions={"issues": "write"}
        )
        github.get_repo.assert_called_once_with("EvexU2/evex-u-workspace")
        github.close.assert_called_once_with()
        integration.close.assert_called_once_with()

    def test_fallback_writes_are_serialized_per_child(self):
        child = self.create()
        self.provider.context = self.trusted_context(child["childId"])
        entered = threading.Event()
        release = threading.Event()

        class BlockingAdapter(Adapter):
            def converge_callback(self, issue_url, body):
                self.calls.append((issue_url, body))
                entered.set()
                release.wait(1)
                return {"accepted": True}

        adapter = BlockingAdapter()
        service = MessagingService(self.provider, self.secret, clock=lambda: self.now, callback_fallback_adapter=adapter)
        first = threading.Thread(target=service.send_callback_fallback, args=(child["capabilityRef"],))
        second = threading.Thread(target=service.send_callback_fallback, args=(child["capabilityRef"],))
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        self.assertEqual(len(adapter.calls), 1)
        release.set()
        first.join(1)
        second.join(1)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(adapter.calls), 2)
