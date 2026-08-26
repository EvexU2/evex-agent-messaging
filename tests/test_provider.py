from __future__ import annotations

from unittest.mock import ANY, MagicMock, Mock, patch
import http.client
from pathlib import Path
import json
import subprocess
import tempfile
import threading
import urllib.error
import uuid
import unittest

from evex_agent_messaging.provider import OpenHandsProvider, ProviderError, _compact_json
from evex_agent_messaging.capability import deterministic_child_id


class OpenHandsProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._original_model_method = OpenHandsProvider._switch_and_verify_model
        self._model_patch = patch.object(
            OpenHandsProvider, "_switch_and_verify_model", autospec=True
        )
        self._model_patch.start()

    def tearDown(self) -> None:
        self._model_patch.stop()

    def create_provider_child(self, provider, *args, **kwargs):
        kwargs.setdefault("model", "gpt-5.6-sol")
        kwargs.setdefault("reasoning_effort", "medium")
        return provider.create_child(*args, **kwargs)

    @staticmethod
    def _git_run(path: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(path), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _reviewer_git_history(self, workspace: Path, child: uuid.UUID) -> dict[str, str | Path]:
        source = workspace / "source"
        source.mkdir()
        self._git_run(source, "init")
        self._git_run(source, "config", "user.name", "EVEX Test")
        self._git_run(source, "config", "user.email", "test@evex.local")
        self._git_run(source, "remote", "add", "origin", "https://github.com/EvexU2/evex-agent-messaging.git")
        candidate = source / "candidate.txt"
        candidate.write_text("original\n")
        self._git_run(source, "add", "candidate.txt")
        self._git_run(source, "commit", "-m", "original")
        original = self._git_run(source, "rev-parse", "HEAD")
        candidate.write_text("reviewed\n")
        self._git_run(source, "commit", "-am", "reviewed")
        reviewed = self._git_run(source, "rev-parse", "HEAD")
        candidate.write_text("repaired\n")
        self._git_run(source, "commit", "-am", "repaired")
        repaired = self._git_run(source, "rev-parse", "HEAD")
        checkout = workspace / f"child-{child}"
        self._git_run(
            source,
            "worktree",
            "add",
            "-b",
            "review/issue-836",
            str(checkout),
            reviewed,
        )
        return {
            "source": source,
            "checkout": checkout,
            "original": original,
            "reviewed": reviewed,
            "repaired": repaired,
        }

    def _reviewer_resume_provider(
        self, workspace: Path, child: uuid.UUID, main: uuid.UUID, task_key: str
    ) -> tuple[OpenHandsProvider, dict[str, str | Path], list[str], list[str]]:
        history = self._reviewer_git_history(workspace, child)
        provider = OpenHandsProvider(
            "http://openhands",
            "key",
            "http://public",
            workspace_root=str(workspace),
            github_token=" provider-held-token ",
        )
        initial = provider._initial_callback_generation(main, child, task_key)
        mission = provider._signed_control_envelope(
            "MISSION",
            {
                "callbackGeneration": initial,
                "childId": str(child),
                "owningMainId": str(main),
                "taskKey": task_key,
                "role": "reviewer",
                "links": {
                    "issue": "https://github.com/EvexU2/evex-u-workspace/issues/836",
                    "specificationPr": "https://github.com/EvexU2/evex-agent-messaging/pull/42",
                },
                "checkout": {
                    "repository": "EvexU2/evex-agent-messaging",
                    "branch": "review/issue-836",
                    "headSha": history["original"],
                },
                "allowedMutations": [],
                "capabilities": [],
                "callback": {"tool": "send_to_parent"},
            },
        )
        result = provider._signed_control_envelope(
            "RESULT",
            {
                "callbackGeneration": initial,
                "childId": str(child),
                "kind": "RESULT",
                "messageKey": "result:reviewed",
                "owningMainId": str(main),
                "taskKey": task_key,
                "text": "{}",
            },
        )
        child_events = ["MISSION\n" + _compact_json(mission)]
        main_events = ["RESULT\n" + _compact_json(result)]
        provider._event_texts = Mock(
            side_effect=lambda conversation_id: (
                list(child_events) if conversation_id == child else list(main_events)
            )
        )
        return provider, history, child_events, main_events

    def test_child_model_is_switched_and_verified_before_mission(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._request = Mock(side_effect=[{}, {"current_model_id": "gpt-5.6-sol"}])

        self._original_model_method(provider, child, "gpt-5.6-sol")

        self.assertEqual(
            provider._request.call_args_list[0].args,
            (
                "POST",
                f"/api/conversations/{child}/switch_acp_model",
                {"model": "gpt-5.6-sol"},
            ),
        )

    def test_readiness_makes_one_authenticated_read_with_fifteen_second_timeout(self) -> None:
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        response = MagicMock()
        response.read.return_value = b'{"active_agent_profile_id":"profile-1"}'
        context = MagicMock()
        context.__enter__.return_value = response
        with patch("evex_agent_messaging.provider.urllib.request.urlopen", return_value=context) as urlopen:
            self.assertTrue(provider.readiness())

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://openhands/api/agent-profiles")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("X-session-api-key"), "key")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 15.0)
        urlopen.assert_called_once()

    def test_readiness_fails_closed_without_retry_or_mutation(self) -> None:
        failures = {
            "incomplete configuration": (OpenHandsProvider("", "key", "http://public"), None),
            "timeout": (OpenHandsProvider("http://openhands", "key", "http://public"), ProviderError("timeout")),
            "connection failure": (OpenHandsProvider("http://openhands", "key", "http://public"), ProviderError("connection")),
            "authentication failure": (OpenHandsProvider("http://openhands", "key", "http://public"), ProviderError("auth", status=401)),
            "non-success response": (OpenHandsProvider("http://openhands", "key", "http://public"), ProviderError("upstream", status=503)),
            "non-object response": (OpenHandsProvider("http://openhands", "key", "http://public"), []),
            "missing active profile": (OpenHandsProvider("http://openhands", "key", "http://public"), {}),
            "empty active profile": (OpenHandsProvider("http://openhands", "key", "http://public"), {"active_agent_profile_id": ""}),
            "non-string active profile": (OpenHandsProvider("http://openhands", "key", "http://public"), {"active_agent_profile_id": 1}),
        }
        for name, (provider, response) in failures.items():
            with self.subTest(name=name):
                provider._request = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
                self.assertFalse(provider.readiness())
                if response is None:
                    provider._request.assert_not_called()
                else:
                    provider._request.assert_called_once_with(
                        "GET", "/api/agent-profiles", timeout=15.0
                    )

    def test_readiness_fails_closed_for_malformed_raw_json(self) -> None:
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        response = MagicMock()
        response.read.return_value = b'{'
        context = MagicMock()
        context.__enter__.return_value = response

        with patch("evex_agent_messaging.provider.urllib.request.urlopen", return_value=context):
            with self.assertRaises(ValueError):
                provider._request("GET", "/api/agent-profiles", timeout=15.0)
        with patch("evex_agent_messaging.provider.urllib.request.urlopen", return_value=context) as urlopen:
            self.assertFalse(provider.readiness())

        urlopen.assert_called_once()
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 15.0)

    def test_request_rejects_declared_or_streamed_oversized_response_before_decode(self) -> None:
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        limit = 1_048_576
        for name, headers, raw in (
            ("declared", {"Content-Length": str(limit + 1)}, b"{}"),
            ("streamed", {}, b"{}" + b"x" * limit),
        ):
            with self.subTest(name=name):
                response = MagicMock()
                response.headers = headers
                response.read.return_value = raw
                context = MagicMock()
                context.__enter__.return_value = response
                with patch(
                    "evex_agent_messaging.provider.urllib.request.urlopen",
                    return_value=context,
                ):
                    with self.assertRaisesRegex(ProviderError, "response exceeds bounded byte budget"):
                        provider._request("GET", "/api/agent-profiles")
                if name == "declared":
                    response.read.assert_not_called()
                else:
                    response.read.assert_called_once_with(limit + 1)

    def test_request_rejects_declared_short_or_truncated_response_before_decode(self) -> None:
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        for name, headers, raw in (
            ("declared short", {"Content-Length": "3"}, b"{}"),
            ("negative", {"Content-Length": "-1"}, b"{}"),
        ):
            with self.subTest(name=name):
                response = MagicMock()
                response.headers = headers
                response.read.return_value = raw
                context = MagicMock()
                context.__enter__.return_value = response
                with patch(
                    "evex_agent_messaging.provider.urllib.request.urlopen",
                    return_value=context,
                ):
                    with self.assertRaisesRegex(ProviderError, "response (is invalid|is truncated)"):
                        provider._request("GET", "/api/conversations/child/events/search?limit=100")

    def test_control_history_short_read_fails_closed_for_child_and_parent(self) -> None:
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        for conversation in ("child", "parent"):
            with self.subTest(conversation=conversation):
                response = MagicMock()
                response.headers = {}
                response.read.side_effect = http.client.IncompleteRead(b"{", 2)
                context = MagicMock()
                context.__enter__.return_value = response
                with patch(
                    "evex_agent_messaging.provider.urllib.request.urlopen",
                    return_value=context,
                ):
                    with self.assertRaisesRegex(ProviderError, "response is truncated"):
                        provider._request(
                            "GET", f"/api/conversations/{conversation}/events/search?limit=100"
                        )

    def test_callback_waits_for_busy_main_before_delivery(self) -> None:
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        provider = OpenHandsProvider("http://openhands", "key", "http://public", sleeper=lambda _seconds: None)
        provider._request = Mock(side_effect=[
            {"execution_status": "running"},
            {"execution_status": "idle"},
            {},
        ])

        result = provider.send_message(main, "result:child:review", "RESULT", "{}")

        self.assertTrue(result["accepted"])
        event = provider._request.call_args_list[2].args
        self.assertEqual(event[0:2], ("POST", f"/api/conversations/{main}/events"))
        self.assertTrue(event[2]["run"])

    def test_resume_mission_carries_verified_context_as_canonical_json(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        provider._request = Mock(side_effect=[
            {"execution_status": "idle"}, {"items": []}, {"items": []}, {},
        ])
        initial = "evxg1_" + "0" * 64
        provider._current_callback_generation = Mock(return_value=initial)
        provider._has_waiting_input = Mock(return_value=True)

        provider.resume_mission(
            child,
            "resume:plan-reviewed",
            "spec-author-604",
            {"reviewOutcome": "PASS", "planCommit": "a" * 40},
            main,
        )

        body = provider._request.call_args.args[2]
        text = body["content"][0]["text"]
        envelope = json.loads(text.removeprefix("RESUME_MISSION\n"))
        self.assertEqual(envelope["callbackGeneration"], provider._resumed_callback_generation(initial, "resume:plan-reviewed"))
        self.assertEqual(envelope["childId"], str(child))
        self.assertTrue(provider._valid_control_signature("RESUME_MISSION", envelope))

    def test_child_creation_starts_mission_without_bootstrap_wait(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenHandsProvider(
                "http://openhands",
                "key",
                "http://public",
                workspace_root=temporary,
            )
            provider._ensure_checkout = Mock()
            provider._validate_existing_checkout = Mock()
            provider._has_user_message = Mock(return_value=False)
            provider._request = Mock(side_effect=[
                ProviderError("missing", status=404),
                {"active_agent_profile_id": "acp"},
                {"id": str(child)},
                {},
                {},
            ])

            self.create_provider_child(provider,
                parent,
                child,
                "reviewer",
                "review-612",
                {
                    "checkout": {"repository": "EvexU2/evex-u-core", "branch": "fix/612", "headSha": "a" * 40},
                    "displayTitle": "Runtime resolution",
                    "links": {"issue": "https://github.com/EvexU2/evex-u-workspace/issues/677"},
                },
                "evx1_opaque",
                frozenset(),
            )

        create = provider._request.call_args_list[2].args[2]
        self.assertNotIn("hook_config", create)
        self.assertEqual(create["mcp_config"], {})
        self.assertEqual(
            create["secrets"]["EVEX_AGENT_MESSAGING_CAPABILITY"],
            {"kind": "StaticSecret", "value": "evx1_opaque"},
        )
        self.assertEqual(create["secrets"]["EVEX_AGENT_ROLE"]["value"], "reviewer")
        self.assertEqual(create["secrets"]["EVEX_AGENT_INSTANCE_ID"]["value"], str(child))
        self.assertEqual(create["secrets"]["EVEX_REASONING_EFFORT"]["value"], "medium")
        self.assertEqual(create["secrets"]["EVEX_AGENT_CAPABILITIES"]["value"], "")
        OpenHandsProvider._switch_and_verify_model.assert_called_with(
            ANY, child, "gpt-5.6-sol"
        )
        self.assertIn("Never call OpenHands provider-control APIs", create["agent_launch_additions"]["system_message_suffix_append"])
        self.assertEqual(
            provider._request.call_args_list[3].args,
            (
                "PATCH",
                f"/api/conversations/{child}",
                {"title": "#677 · Review · Runtime resolution"},
            ),
        )
        mission_event = provider._request.call_args_list[4].args[2]["content"][0]["text"]
        self.assertTrue(mission_event.startswith("MISSION\n{"))
        self.assertRegex(
            json.loads(mission_event.removeprefix("MISSION\n"))["callbackGeneration"],
            r"^evxg1_[0-9a-f]{64}$",
        )
        self.assertFalse(hasattr(provider.wait_until_terminal, "assert_called"))

    def test_recovered_child_with_incomplete_generation_history_is_not_restarted(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenHandsProvider(
                "http://openhands", "key", "http://public", workspace_root=temporary
            )
            provider._has_user_message = Mock(return_value=False)
            provider._validate_existing_child = Mock()
            provider._current_callback_generation = Mock(
                side_effect=ProviderError("OpenHands Child callback generation history is incomplete")
            )
            provider._request = Mock(return_value={"id": str(child)})

            with self.assertRaisesRegex(ProviderError, "generation history"):
                self.create_provider_child(
                    provider, parent, child, "reviewer", "review-614",
                    {"checkout": {"repository": "EvexU2/evex-u-core", "branch": "fix/614", "headSha": "a" * 40}},
                    "evx1_opaque", frozenset(),
                )

        self.assertFalse(any(
            len(call.args) > 1 and call.args[0] == "POST"
            for call in provider._request.call_args_list
        ))

    def test_specialist_title_maps_roles_and_hides_task_identity(self) -> None:
        mission = {
            "displayTitle": "  Runtime   resolution  ",
            "links": {"issue": "https://github.com/EvexU2/evex-u-workspace/issues/677"},
        }
        expected = {
            "spec": "Spec",
            "plan-author": "Plan",
            "writer": "Implement",
            "reviewer": "Review",
            "qa": "QA",
            "repair": "Repair",
        }

        for role, label in expected.items():
            with self.subTest(role=role):
                title = OpenHandsProvider._conversation_title(
                    role, "issue-677-deadbeef", mission
                )
                self.assertEqual(title, f"#677 · {label} · Runtime resolution")
                self.assertNotIn("deadbeef", title)
                self.assertNotIn("EVEX", title)

    def test_specialist_title_has_issue_role_fallback_for_legacy_mission(self) -> None:
        title = OpenHandsProvider._conversation_title(
            "qa",
            "issue-677-e1-qa-48491",
            {"links": {"issue": "https://github.com/EvexU2/evex-u-workspace/issues/677"}},
        )

        self.assertEqual(title, "#677 · QA")

    def test_integrated_mission_starts_with_empty_profile_mcp_override(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenHandsProvider(
                "http://openhands", "key", "http://public", workspace_root=temporary
            )
            provider._ensure_checkout = Mock()
            provider._validate_existing_checkout = Mock()
            provider._has_user_message = Mock(return_value=False)
            provider.wait_until_terminal = Mock(return_value="finished")
            provider._request = Mock(side_effect=[
                ProviderError("missing", status=404),
                {"active_agent_profile_id": "acp"},
                {"id": str(child)},
                {},
                {},
                {},
            ])

            self.create_provider_child(provider,
                parent,
                child,
                "qa",
                "qa-integrated",
                {"checkout": {"repository": "EvexU2/evex-u-core", "branch": "fix/qa", "headSha": "a" * 40}},
                "evx1_opaque",
                frozenset({"runtime_environment"}),
            )

        create = provider._request.call_args_list[2].args[2]
        self.assertEqual(create["mcp_config"], {})
        self.assertEqual(
            create["secrets"]["EVEX_AGENT_CAPABILITIES"]["value"],
            "runtime_environment",
        )
        self.assertEqual(create["tags"]["evexcaps"], "runtime_environment")

    def test_child_mission_is_not_sent_when_exact_admission_fails(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenHandsProvider(
                "http://openhands", "key", "http://public", workspace_root=temporary
            )
            provider._ensure_checkout = Mock()
            provider._has_user_message = Mock(return_value=False)
            provider._validate_existing_checkout = Mock(
                side_effect=ProviderError("Child checkout validation failed")
            )
            provider._request = Mock(side_effect=[
                ProviderError("missing", status=404),
                {"active_agent_profile_id": "acp"},
                {"id": str(child)},
                {},
                {},
            ])

            with self.assertRaisesRegex(ProviderError, "checkout validation"):
                self.create_provider_child(provider,
                    parent,
                    child,
                    "writer",
                    "writer-612",
                    {
                        "checkout": {
                            "repository": "EvexU2/evex-u-core",
                            "branch": "fix/612",
                            "headSha": "a" * 40,
                        }
                    },
                    "evx1_opaque",
                    frozenset(),
                )

        delivered = [
            call.args[2]["content"][0]["text"]
            for call in provider._request.call_args_list
            if len(call.args) == 3 and call.args[0] == "POST" and call.args[1].endswith("/events")
        ]
        self.assertEqual(delivered, [])

    def test_concurrent_create_reuses_matching_child_and_continues_admission(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        with tempfile.TemporaryDirectory() as temporary:
            checkout_path = str(Path(temporary) / f"child-{child}")
            provider = OpenHandsProvider(
                "http://openhands", "key", "http://public", workspace_root=temporary
            )
            provider._ensure_checkout = Mock()
            provider._validate_existing_checkout = Mock()
            provider._has_user_message = Mock(return_value=False)
            existing = {
                "id": str(child),
                "workspace": {"working_dir": checkout_path},
                "tags": {
                    "project": "evex-u",
                    "evexrole": "role-child",
                    "evextask": "writer-612",
                    "evexparent": str(parent),
                    "evexchildrole": "writer",
                    "evexmodel": "gpt-5.6-sol",
                    "evexreasoning": "medium",
                    "evexcaps": "none",
                },
            }
            provider._request = Mock(side_effect=[
                ProviderError("missing", status=404),
                {"active_agent_profile_id": "acp"},
                ProviderError("conflict", status=409),
                existing,
                {"items": []},
                {"items": []},
                {},
            ])

            result = self.create_provider_child(provider,
                parent,
                child,
                "writer",
                "writer-612",
                {
                    "checkout": {
                        "repository": "EvexU2/evex-u-core",
                        "branch": "fix/612",
                        "headSha": "a" * 40,
                    }
                },
                "evx1_opaque",
                frozenset(),
            )

        self.assertFalse(result["created"])
        provider._validate_existing_checkout.assert_called_once()

    def test_child_admission_validates_exact_checkout_before_conversation_mutation(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            checkout = workspace / f"child-{child}"
            checkout.mkdir()
            subprocess.run(["git", "init", "-b", "fix/612"], cwd=checkout, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "eval@example.invalid"], cwd=checkout, check=True)
            subprocess.run(["git", "config", "user.name", "Eval"], cwd=checkout, check=True)
            subprocess.run(["git", "remote", "add", "origin", "https://github.com/EvexU2/evex-u-core.git"], cwd=checkout, check=True)
            (checkout / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=checkout, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=checkout, check=True, capture_output=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, check=True, capture_output=True, text=True).stdout.strip()
            provider = OpenHandsProvider("http://openhands", "key", "http://public", workspace_root=str(workspace))
            provider._request = Mock(side_effect=[
                ProviderError("missing", status=404),
                ProviderError("stop after admission", status=500),
            ])
            mission = {"checkout": {"repository": "EvexU2/evex-u-core", "branch": "fix/612", "headSha": head}}

            with self.assertRaises(ProviderError):
                self.create_provider_child(provider, parent, child, "reviewer", "review-612", mission, "evx1_opaque", frozenset())

            self.assertEqual(
                provider._request.call_args_list[0].args,
                ("GET", f"/api/conversations/{child}"),
            )
            self.assertEqual(provider._request.call_count, 2)

    def test_child_admission_rejects_missing_or_mismatched_checkout_without_api_call(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenHandsProvider("http://openhands", "key", "http://public", workspace_root=temporary)
            provider._request = Mock(side_effect=ProviderError("missing", status=404))
            mission = {"checkout": {"repository": "EvexU2/evex-u-core", "branch": "fix/612", "headSha": "a" * 40}}

            with self.assertRaisesRegex(ProviderError, "checkout"):
                self.create_provider_child(provider, parent, child, "reviewer", "review-612", mission, "evx1_opaque", frozenset())

            provider._request.assert_called_once_with("GET", f"/api/conversations/{child}")

    def test_child_admission_provisions_deterministic_worktree_from_persistent_mirror(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "eval@example.invalid"], cwd=source, check=True)
            subprocess.run(["git", "config", "user.name", "Eval"], cwd=source, check=True)
            (source / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=source, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=source, check=True, capture_output=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True).stdout.strip()
            mirrors = workspace / "mirrors"
            mirrors.mkdir()
            mirror = mirrors / "EvexU2--evex-u-core.git"
            subprocess.run(["git", "clone", "--mirror", str(source), str(mirror)], check=True, capture_output=True)
            subprocess.run(["git", "remote", "set-url", "origin", "https://github.com/EvexU2/evex-u-core.git"], cwd=mirror, check=True)
            delivery = workspace / "delivery"
            delivery.mkdir()
            provider = OpenHandsProvider("http://openhands", "key", "http://public", workspace_root=str(delivery))
            provider._request = Mock(side_effect=[
                ProviderError("missing", status=404),
                ProviderError("stop after admission", status=500),
            ])
            mission = {"checkout": {"repository": "EvexU2/evex-u-core", "branch": "fix/612", "headSha": head}}

            with self.assertRaises(ProviderError):
                self.create_provider_child(provider, parent, child, "writer", "writer-612", mission, "evx1_opaque", frozenset())

            checkout = delivery / f"child-{child}"
            self.assertTrue(checkout.is_dir())
            self.assertEqual(
                subprocess.run(["git", "branch", "--show-current"], cwd=checkout, check=True, capture_output=True, text=True).stdout.strip(),
                "fix/612",
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "config", "--bool", "remote.origin.mirror"],
                    cwd=checkout,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                "false",
            )
            self.assertEqual(
                provider._request.call_args_list[0].args,
                ("GET", f"/api/conversations/{child}"),
            )

    def test_provisioning_owns_branch_creation_without_remote_guessing_and_reports_git_cause(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            delivery = workspace / "delivery"
            delivery.mkdir()
            mirror = workspace / "mirrors" / "EvexU2--evex-agent-skills.git"
            mirror.mkdir(parents=True)
            provider = OpenHandsProvider(
                "http://openhands", "key", "http://public", workspace_root=str(delivery)
            )
            completed = lambda output="": subprocess.CompletedProcess([], 0, output, "")
            failure = subprocess.CalledProcessError(
                128,
                ["git", "worktree", "add"],
                stderr="fatal: a branch named 'delivery/670-plan-author' already exists\n",
            )
            with patch("evex_agent_messaging.provider.subprocess.run") as run:
                run.side_effect = [
                    completed("https://github.com/EvexU2/evex-agent-skills.git\n"),
                    completed(),
                    completed(),
                    failure,
                ]
                with self.assertRaisesRegex(
                    ProviderError,
                    "provisioning failed: a branch named 'delivery/670-plan-author' already exists",
                ):
                    provider._provision_checkout(
                        delivery / "child-22222222-2222-4222-8222-222222222222",
                        {
                            "repository": "EvexU2/evex-agent-skills",
                            "branch": "delivery/670-plan-author",
                            "headSha": "a" * 40,
                        },
                    )

            command = run.call_args_list[-1].args[0]
            self.assertIn("--no-track", command)
            self.assertIn("--no-guess-remote", command)

    def test_existing_progressed_child_is_reused_without_requiring_initial_head(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            checkout = workspace / f"child-{child}"
            checkout.mkdir()
            subprocess.run(["git", "init", "-b", "fix/612"], cwd=checkout, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "eval@example.invalid"], cwd=checkout, check=True)
            subprocess.run(["git", "config", "user.name", "Eval"], cwd=checkout, check=True)
            subprocess.run(["git", "remote", "add", "origin", "https://github.com/EvexU2/evex-u-core.git"], cwd=checkout, check=True)
            (checkout / "README.md").write_text("initial\n")
            subprocess.run(["git", "add", "README.md"], cwd=checkout, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=checkout, check=True, capture_output=True)
            initial = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, check=True, capture_output=True, text=True).stdout.strip()
            (checkout / "README.md").write_text("progressed\n")
            subprocess.run(["git", "add", "README.md"], cwd=checkout, check=True)
            subprocess.run(["git", "commit", "-m", "progress"], cwd=checkout, check=True, capture_output=True)
            provider = OpenHandsProvider("http://openhands", "key", "http://public", workspace_root=str(workspace))
            provider._has_user_message = Mock(return_value=True)
            provider._request = Mock(return_value={
                "id": str(child),
                "last_user_message_id": "event-1",
                "workspace": {"working_dir": str(checkout)},
                "tags": {
                    "project": "evex-u",
                    "evexrole": "role-child",
                    "evextask": "writer-612",
                    "evexparent": str(parent),
                    "evexchildrole": "writer",
                    "evexmodel": "gpt-5.6-sol",
                    "evexreasoning": "medium",
                    "evexcaps": "none",
                },
            })
            mission = {"checkout": {"repository": "EvexU2/evex-u-core", "branch": "fix/612", "headSha": initial}}

            result = self.create_provider_child(provider,
                parent, child, "writer", "writer-612", mission, "evx1_opaque", frozenset()
            )

            self.assertFalse(result["created"])
            provider._request.assert_called_once_with("GET", f"/api/conversations/{child}")

    def test_wait_until_terminal_is_bounded(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public", sleeper=lambda _seconds: None)
        provider._request = Mock(side_effect=[
            {"execution_status": "running"},
            {"execution_status": "finished"},
        ])

        self.assertEqual(provider.wait_until_terminal(child), "finished")

    def test_usage_reports_tokens_cache_hit_rate_and_official_standard_estimate(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._request = Mock(
            return_value={
                "current_model_id": "gpt-5.6-sol",
                "tags": {
                    "evexmodel": "gpt-5.6-sol",
                    "evexreasoning": "high",
                },
                "stats": {
                    "usage_to_metrics": {
                        "default": {
                            "accumulated_token_usage": {
                                "prompt_tokens": 1_000,
                                "cache_read_tokens": 9_000,
                                "cache_write_tokens": 500,
                                "completion_tokens": 200,
                                "reasoning_tokens": 50,
                            },
                            "token_usages": [
                                {
                                    "prompt_tokens": 1_000,
                                    "cache_read_tokens": 9_000,
                                    "cache_write_tokens": 500,
                                    "completion_tokens": 200,
                                    "reasoning_tokens": 50,
                                }
                            ],
                        }
                    }
                },
            }
        )

        value = provider.usage(child)

        self.assertEqual(value["model"], "gpt-5.6-sol")
        self.assertEqual(value["reasoningEffort"], "high")
        self.assertEqual(
            value["tokens"],
            {
                "uncachedInput": 1_000,
                "cachedInput": 9_000,
                "cacheWrite": 500,
                "output": 200,
                "reasoning": 50,
            },
        )
        self.assertEqual(value["cacheHitRate"], 0.9)
        self.assertEqual(value["officialApiEquivalentUsd"], 0.0141)
        self.assertEqual(value["pricing"]["serviceTier"], "standard")
        self.assertIn("developers.openai.com", value["pricing"]["source"])
        self.assertIn("not a subscription invoice", value["disclaimer"])

    def test_usage_aggregates_every_openhands_metrics_bucket(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._request = Mock(return_value={
            "tags": {"evexmodel": "gpt-5.6-terra", "evexreasoning": "medium"},
            "stats": {
                "usage_to_metrics": {
                    "default": {
                        "accumulated_token_usage": {
                            "prompt_tokens": 1_000,
                            "cache_read_tokens": 9_000,
                            "cache_write_tokens": 0,
                            "completion_tokens": 100,
                            "reasoning_tokens": 20,
                        },
                        "token_usages": [{
                            "prompt_tokens": 1_000,
                            "cache_read_tokens": 9_000,
                            "cache_write_tokens": 0,
                            "completion_tokens": 100,
                            "reasoning_tokens": 20,
                        }],
                    },
                    "runtime": {
                        "accumulated_token_usage": {
                            "prompt_tokens": 500,
                            "cache_read_tokens": 500,
                            "cache_write_tokens": 200,
                            "completion_tokens": 50,
                            "reasoning_tokens": 10,
                        },
                        "token_usages": [{
                            "prompt_tokens": 500,
                            "cache_read_tokens": 500,
                            "cache_write_tokens": 200,
                            "completion_tokens": 50,
                            "reasoning_tokens": 10,
                        }],
                    },
                }
            },
        })

        value = provider.usage(child)

        self.assertEqual(value["tokens"], {
            "uncachedInput": 1_500,
            "cachedInput": 9_500,
            "cacheWrite": 200,
            "output": 150,
            "reasoning": 30,
        })
        self.assertEqual(value["cacheHitRate"], round(9_500 / 11_000, 6))
        self.assertEqual(value["officialApiEquivalentUsd"], 0.0072)

    def test_usage_applies_long_context_standard_rates_per_turn(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._request = Mock(return_value={
            "tags": {"evexmodel": "gpt-5.6-sol", "evexreasoning": "high"},
            "stats": {
                "usage_to_metrics": {
                    "default": {
                        "accumulated_token_usage": {
                            "prompt_tokens": 10_000,
                            "cache_read_tokens": 270_000,
                            "cache_write_tokens": 0,
                            "completion_tokens": 2_000,
                            "reasoning_tokens": 500,
                        },
                        "token_usages": [{
                            "prompt_tokens": 10_000,
                            "cache_read_tokens": 270_000,
                            "cache_write_tokens": 0,
                            "completion_tokens": 2_000,
                            "reasoning_tokens": 500,
                        }],
                    }
                }
            },
        })

        value = provider.usage(child)

        self.assertEqual(value["longContextTurns"], 1)
        self.assertAlmostEqual(value["officialApiEquivalentUsd"], 0.356, places=8)

    def test_usage_fails_closed_without_reasoning_effort(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._request = Mock(return_value={
            "current_model_id": "gpt-5.6-sol",
            "stats": {
                "usage_to_metrics": {
                    "default": {
                        "accumulated_token_usage": {},
                        "token_usages": [],
                    }
                }
            },
        })

        with self.assertRaisesRegex(ProviderError, "reasoning effort"):
            provider.usage(child)

    def test_usage_rejects_reasoning_tokens_above_output_tokens(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._request = Mock(return_value={
            "tags": {"evexmodel": "gpt-5.6-sol", "evexreasoning": "high"},
            "stats": {
                "usage_to_metrics": {
                    "default": {
                        "accumulated_token_usage": {
                            "completion_tokens": 10,
                            "reasoning_tokens": 11,
                        },
                        "token_usages": [{"completion_tokens": 10, "reasoning_tokens": 11}],
                    }
                }
            },
        })

        with self.assertRaisesRegex(ProviderError, "reasoning tokens"):
            provider.usage(child)

    def test_cancel_interrupts_then_delivers_bound_cancel_to_paused_child(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public", sleeper=lambda _seconds: None)
        provider._request = Mock(side_effect=[
            {"execution_status": "running"},
            {"items": []},
            {"items": []},
            {},
            {"execution_status": "paused"},
            {},
            {"execution_status": "cancelled"},
        ])

        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        provider._current_callback_generation = Mock(return_value="evxg1_" + "0" * 64)
        result = provider.cancel_mission(child, "cancel:one", "spec-614", main)

        self.assertEqual(result, {
            "accepted": True,
            "messageKey": "cancel:one",
            "taskKey": "spec-614",
            "outcome": "CANCELLED",
        })
        self.assertEqual(provider._request.call_args_list[0].args, ("GET", f"/api/conversations/{child}"))
        self.assertEqual(provider._request.call_args_list[3].args, ("POST", f"/api/conversations/{child}/interrupt", {}))
        event = provider._request.call_args_list[5].args
        self.assertEqual(event[0:2], ("POST", f"/api/conversations/{child}/events"))
        self.assertTrue(event[2]["run"])
        envelope = json.loads(event[2]["content"][0]["text"].removeprefix("CANCEL_MISSION\n"))
        self.assertTrue(provider._valid_control_signature("CANCEL_MISSION", envelope))

    def test_cancel_wakes_authenticated_input_paused_child_before_terminal_replay(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        task_key = "issue-689-paused-cancel-terminal"
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        generation = provider._initial_callback_generation(main, child, task_key)
        events = {
            child: [
                "MISSION\n" + _compact_json(provider._signed_control_envelope("MISSION", {
                    "callbackGeneration": generation,
                    "childId": str(child),
                    "owningMainId": str(main),
                    "taskKey": task_key,
                }))
            ],
            main: [
                "NEEDS_INPUT\n" + _compact_json(provider._signed_control_envelope("NEEDS_INPUT", {
                    "callbackGeneration": generation,
                    "childId": str(child),
                    "kind": "NEEDS_INPUT",
                    "messageKey": "decision:one",
                    "owningMainId": str(main),
                    "taskKey": task_key,
                    "text": '{"options":["A","B"],"question":"Choose"}',
                }))
            ],
        }
        provider._event_texts = Mock(side_effect=lambda conversation_id: events[conversation_id])
        status = "paused"
        interrupted = False
        requests = []

        def request(method, path, payload=None):
            nonlocal interrupted, status
            requests.append((method, path, payload))
            if method == "GET" and path == f"/api/conversations/{child}":
                return {"execution_status": status}
            if method == "POST" and path == f"/api/conversations/{child}/interrupt":
                interrupted = True
                return {}
            if method == "POST" and path == f"/api/conversations/{child}/events":
                self.assertTrue(interrupted, "paused child must be interrupted before cancellation delivery")
                events[child].append(payload["content"][0]["text"])
                status = "cancelled"
                return {}
            self.fail(f"unexpected provider request: {method} {path}")

        provider._request = Mock(side_effect=request)

        self.assertTrue(provider._has_waiting_input(main, child, task_key))
        self.assertEqual(
            provider.cancel_mission(child, "cancel:one", task_key, main),
            {"accepted": True, "messageKey": "cancel:one", "taskKey": task_key, "outcome": "CANCELLED"},
        )
        self.assertEqual(
            provider.cancel_mission(child, "cancel:one", task_key, main),
            {"accepted": True, "messageKey": "cancel:one", "taskKey": task_key, "outcome": "CANCELLED"},
        )
        self.assertEqual(
            provider.resume_mission(child, "resume:late", task_key, {"answer": "A"}, main),
            {"accepted": False, "messageKey": "resume:late", "taskKey": task_key, "outcome": "CANCELLED"},
        )
        self.assertEqual(
            [path for method, path, _ in requests if method == "POST" and path.endswith("/interrupt")],
            [f"/api/conversations/{child}/interrupt"],
        )
        cancel_events = [
            payload for method, path, payload in requests
            if method == "POST" and path.endswith("/events")
        ]
        self.assertEqual(len(cancel_events), 1)
        self.assertTrue(cancel_events[0]["run"])
        envelope = json.loads(cancel_events[0]["content"][0]["text"].removeprefix("CANCEL_MISSION\n"))
        self.assertTrue(provider._valid_control_signature("CANCEL_MISSION", envelope))

    def test_cancel_does_not_relabel_an_ordinary_finished_child_as_cancelled(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public", sleeper=lambda _seconds: None)
        provider._request = Mock(return_value={"execution_status": "finished"})

        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        result = provider.cancel_mission(child, "cancel:one", "spec-614", main)

        self.assertEqual(result, {
            "accepted": False,
            "messageKey": "cancel:one",
            "taskKey": "spec-614",
            "outcome": "RESULT",
        })
        provider._request.assert_called_once_with("GET", f"/api/conversations/{child}")

    def test_cancel_replays_only_the_identical_native_cancel_event(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        generation = provider._initial_callback_generation(main, child, "spec-614")
        cancel = "CANCEL_MISSION\n" + json.dumps(provider._signed_control_envelope("CANCEL_MISSION", {
            "callbackGeneration": generation, "childId": str(child), "messageKey": "cancel:one", "owningMainId": str(main),
            "targetId": str(child), "taskKey": "spec-614",
        }), sort_keys=True, separators=(",", ":"))
        provider._current_callback_generation = Mock(return_value=generation)
        provider._request = Mock(side_effect=[
            {"execution_status": "cancelled"},
            {"items": [{"llm_message": {"content": [{"text": cancel}]}}]},
        ])

        self.assertEqual(
            provider.cancel_mission(child, "cancel:one", "spec-614", main),
            {"accepted": True, "messageKey": "cancel:one", "taskKey": "spec-614", "outcome": "CANCELLED"},
        )

    def test_result_or_resume_winner_blocks_later_cancellation_without_interrupt(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        result = (
            "RESULT\n"
            '{"childId":"22222222-2222-4222-8222-222222222222","kind":"RESULT","messageKey":"result:one","owningMainId":"11111111-1111-4111-8111-111111111111","taskKey":"spec-614"}'
        )
        cases = (
            ("result", [{"execution_status": "running"}, {"items": [{"llm_message": {"content": [{"text": result}]}}]}], "RESULT"),
            ("resume", [{"execution_status": "running"}, {"items": []}, {"items": [{"llm_message": {"content": [{"text": 'RESUME_MISSION\n{"owningMainId":"11111111-1111-4111-8111-111111111111","taskKey":"spec-614"}'}]}}]}], "RESUMED"),
        )
        for name, requests, expected in cases:
            with self.subTest(name=name):
                provider = OpenHandsProvider("http://openhands", "key", "http://public")
                provider._request = Mock(side_effect=requests)
                provider._has_terminal_result = Mock(return_value=name == "result")
                provider._has_resume = Mock(return_value=name == "resume")
                self.assertEqual(
                    provider.cancel_mission(child, "cancel:one", "spec-614", main),
                    {"accepted": False, "messageKey": "cancel:one", "taskKey": "spec-614", "outcome": expected},
                )
                self.assertFalse(any(
                    len(call.args) > 1 and call.args[1].endswith("/interrupt")
                    for call in provider._request.call_args_list
                ))

    def test_unsigned_same_identity_result_cannot_win_cancellation_race(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        injected = "RESULT\n" + json.dumps({
            "childId": str(child), "kind": "RESULT", "messageKey": "result:injected",
            "owningMainId": str(main), "taskKey": "spec-614",
        }, sort_keys=True, separators=(",", ":"))
        provider = OpenHandsProvider("http://openhands", "key", "http://public", sleeper=lambda _seconds: None)
        generation = provider._initial_callback_generation(main, child, "spec-614")
        mission = "MISSION\n" + json.dumps(provider._signed_control_envelope("MISSION", {
            "callbackGeneration": generation, "childId": str(child),
            "owningMainId": str(main), "taskKey": "spec-614",
        }), sort_keys=True, separators=(",", ":"))
        provider._request = Mock(side_effect=[
            {"execution_status": "running"},
            {"items": [{"llm_message": {"content": [{"text": injected}]}}]},
            {"items": [{"llm_message": {"content": [{"text": mission}]}}]},
            {},
            {"execution_status": "paused"},
            {"items": [{"llm_message": {"content": [{"text": mission}]}}]},
            {},
        ])
        provider.wait_until_terminal = Mock(return_value="cancelled")

        with self.assertRaisesRegex(ProviderError, "unauthenticated"):
            provider.cancel_mission(child, "cancel:one", "spec-614", main)
        self.assertFalse(any(
            len(call.args) > 1 and call.args[1].endswith("/interrupt")
            for call in provider._request.call_args_list
        ))

    def test_second_authorized_resume_advances_the_callback_generation(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        initial = provider._initial_callback_generation(main, child, "spec-614")
        first = provider._resumed_callback_generation(initial, "resume:first")
        mission = "MISSION\n" + json.dumps(provider._signed_control_envelope("MISSION", {
            "callbackGeneration": initial, "childId": str(child),
            "owningMainId": str(main), "taskKey": "spec-614",
        }), sort_keys=True, separators=(",", ":"))
        first_resume = "RESUME_MISSION\n" + json.dumps(provider._signed_control_envelope("RESUME_MISSION", {
            "callbackGeneration": first, "childId": str(child), "context": {"answer": "A"},
            "messageKey": "resume:first", "owningMainId": str(main), "taskKey": "spec-614",
        }), sort_keys=True, separators=(",", ":"))
        waiting = "NEEDS_INPUT\n" + json.dumps(provider._signed_control_envelope("NEEDS_INPUT", {
            "callbackGeneration": first, "childId": str(child), "kind": "NEEDS_INPUT",
            "messageKey": "decision:second", "owningMainId": str(main), "taskKey": "spec-614", "text": "{}",
        }), sort_keys=True, separators=(",", ":"))
        provider._request = Mock(side_effect=[
            {"execution_status": "idle"},
            {"items": []},
            {"items": [{"llm_message": {"content": [{"text": first_resume}, {"text": mission}]}}]},
            {"items": [{"llm_message": {"content": [{"text": first_resume}, {"text": mission}]}}]},
            {"items": [{"llm_message": {"content": [{"text": waiting}]}}]},
            {},
        ])

        result = provider.resume_mission(
            child, "resume:second", "spec-614", {"answer": "B"}, main
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["outcome"], "RESUMED")
        self.assertIn(provider._resumed_callback_generation(first, "resume:second"), provider._request.call_args.args[2]["content"][0]["text"])

    def test_terminal_result_accepts_repeatable_fresh_key_resume_and_exact_replay(self) -> None:
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        task_key = "plan-796"
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        initial = provider._initial_callback_generation(main, child, task_key)

        def control(kind, envelope):
            return kind + "\n" + _compact_json(
                provider._signed_control_envelope(kind, envelope)
            )

        mission = control("MISSION", {
            "callbackGeneration": initial,
            "childId": str(child),
            "owningMainId": str(main),
            "taskKey": task_key,
        })
        first_result = control("RESULT", {
            "callbackGeneration": initial,
            "childId": str(child),
            "kind": "RESULT",
            "messageKey": "result:first",
            "owningMainId": str(main),
            "taskKey": task_key,
            "text": "{}",
        })
        child_events = [mission]
        main_events = [first_result]
        provider._event_texts = Mock(
            side_effect=lambda conversation_id: (
                list(child_events) if conversation_id == child else list(main_events)
            )
        )
        provider._request = Mock(
            side_effect=lambda method, path, *args: (
                {"execution_status": "finished"} if method == "GET" else {}
            )
        )

        first = provider.resume_mission(
            child,
            "review:first",
            task_key,
            {"findings": ["P2-1"]},
            main,
        )
        self.assertEqual(first["outcome"], "RESUMED")
        self.assertTrue(first["accepted"])
        first_resume = provider._request.call_args_list[-1].args[2]["content"][0]["text"]
        child_events.insert(0, first_resume)

        provider._request.reset_mock()
        replay = provider.resume_mission(
            child,
            "review:first",
            task_key,
            {"findings": ["P2-1"]},
            main,
        )
        self.assertEqual(replay["outcome"], "RESUMED")
        self.assertTrue(replay["accepted"])
        self.assertFalse(any(call.args[0] == "POST" for call in provider._request.call_args_list))

        with self.assertRaisesRegex(ProviderError, "changed context"):
            provider.resume_mission(
                child,
                "review:first",
                task_key,
                {"findings": ["P2-2"]},
                main,
            )

        second_generation = provider._resumed_callback_generation(
            initial, "review:first"
        )
        main_events.insert(0, control("RESULT", {
            "callbackGeneration": second_generation,
            "childId": str(child),
            "kind": "RESULT",
            "messageKey": "result:second",
            "owningMainId": str(main),
            "taskKey": task_key,
            "text": "{}",
        }))
        provider._request.reset_mock()
        second = provider.resume_mission(
            child,
            "review:second",
            task_key,
            {"findings": ["P1-2"]},
            main,
        )
        self.assertEqual(second["outcome"], "RESUMED")
        self.assertTrue(second["accepted"])
        second_resume = provider._request.call_args_list[-1].args[2]["content"][0]["text"]
        self.assertIn(
            provider._resumed_callback_generation(second_generation, "review:second"),
            second_resume,
        )

    def test_terminal_resumed_run_without_result_accepts_one_recovery_key(self) -> None:
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        task_key = "plan-796"
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        initial = provider._initial_callback_generation(main, child, task_key)

        def control(kind, envelope):
            return kind + "\n" + _compact_json(
                provider._signed_control_envelope(kind, envelope)
            )

        mission = control("MISSION", {
            "callbackGeneration": initial,
            "childId": str(child),
            "owningMainId": str(main),
            "taskKey": task_key,
        })
        first_result = control("RESULT", {
            "callbackGeneration": initial,
            "childId": str(child),
            "kind": "RESULT",
            "messageKey": "result:first",
            "owningMainId": str(main),
            "taskKey": task_key,
            "text": "{}",
        })
        review_generation = provider._resumed_callback_generation(
            initial, "plan-review:first"
        )
        review_resume = control("RESUME_MISSION", {
            "callbackGeneration": review_generation,
            "childId": str(child),
            "context": {"findings": ["P2-1"]},
            "messageKey": "plan-review:first",
            "owningMainId": str(main),
            "taskKey": task_key,
        })
        child_events = [review_resume, mission]
        main_events = [first_result]
        provider._event_texts = Mock(
            side_effect=lambda conversation_id: (
                list(child_events) if conversation_id == child else list(main_events)
            )
        )
        provider._request = Mock(
            side_effect=lambda method, path, *args: (
                {"execution_status": "finished"} if method == "GET" else {}
            )
        )

        ordinary = provider.resume_mission(
            child,
            "plan-review:second",
            task_key,
            {"findings": ["P1-2"]},
            main,
        )
        self.assertEqual(ordinary["outcome"], "RESULT")
        self.assertFalse(ordinary["accepted"])
        self.assertFalse(any(
            call.args[0] == "POST" for call in provider._request.call_args_list
        ))

        provider._request.reset_mock()
        recovery_context = {
            "instruction": "Reuse the completed bounded result and send the bound callback."
        }
        recovery = provider.resume_mission(
            child,
            "recovery-mode-796-plan-callback",
            task_key,
            recovery_context,
            main,
        )
        self.assertEqual(recovery["outcome"], "RESUMED")
        self.assertTrue(recovery["accepted"])
        recovery_resume = provider._request.call_args_list[-1].args[2]["content"][0]["text"]
        self.assertIn(
            provider._resumed_callback_generation(
                review_generation, "recovery-mode-796-plan-callback"
            ),
            recovery_resume,
        )
        child_events.insert(0, recovery_resume)

        provider._request.reset_mock()
        replay = provider.resume_mission(
            child,
            "recovery-mode-796-plan-callback",
            task_key,
            recovery_context,
            main,
        )
        self.assertEqual(replay["outcome"], "RESUMED")
        self.assertTrue(replay["accepted"])
        self.assertFalse(any(
            call.args[0] == "POST" for call in provider._request.call_args_list
        ))

        provider._request.reset_mock()
        second_recovery = provider.resume_mission(
            child,
            "recovery-mode-796-plan-callback-again",
            task_key,
            recovery_context,
            main,
        )
        self.assertEqual(second_recovery["outcome"], "RESULT")
        self.assertFalse(second_recovery["accepted"])
        self.assertFalse(any(
            call.args[0] == "POST" for call in provider._request.call_args_list
        ))

    def test_terminal_result_resume_rejects_cancelled_child(self) -> None:
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._request = Mock(return_value={"execution_status": "cancelled"})

        self.assertEqual(
            provider.resume_mission(
                child, "review:first", "plan-796", {"findings": ["P2"]}, main
            ),
            {
                "accepted": False,
                "messageKey": "review:first",
                "taskKey": "plan-796",
                "outcome": "CANCELLED",
            },
        )
        provider._event_texts = Mock()
        provider._event_texts.assert_not_called()

    def test_current_result_after_resume_ignores_authenticated_prior_input(self) -> None:
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        task_key = "spec-614"
        child = deterministic_child_id(main, task_key)
        self.assertEqual(child.version, 5)
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        first = provider._initial_callback_generation(main, child, task_key)
        second = provider._resumed_callback_generation(first, "resume:first")
        mission = "MISSION\n" + _compact_json(provider._signed_control_envelope("MISSION", {
            "callbackGeneration": first, "childId": str(child),
            "owningMainId": str(main), "taskKey": task_key,
        }))
        resume = "RESUME_MISSION\n" + _compact_json(provider._signed_control_envelope("RESUME_MISSION", {
            "callbackGeneration": second, "childId": str(child), "context": {"answer": "A"},
            "messageKey": "resume:first", "owningMainId": str(main), "taskKey": task_key,
        }))
        prior_wait = "NEEDS_INPUT\n" + _compact_json(provider._signed_control_envelope("NEEDS_INPUT", {
            "callbackGeneration": first, "childId": str(child), "kind": "NEEDS_INPUT",
            "messageKey": "decision:first", "owningMainId": str(main), "taskKey": task_key, "text": "{}",
        }))
        callback = _compact_json({
            "callbackGeneration": second, "childId": str(child), "kind": "RESULT",
            "messageKey": "result:second", "owningMainId": str(main), "taskKey": task_key,
            "text": "{}",
        })
        provider._request = Mock(side_effect=[
            {"execution_status": "idle"}, {"execution_status": "idle"}, {},
        ])
        provider._event_texts = Mock(side_effect=[
            [resume, mission], [], [resume, mission], [prior_wait],
        ])

        self.assertEqual(
            provider.send_child_message(child, main, "result:second", "RESULT", callback),
            {"accepted": True, "messageKey": "result:second"},
        )
        self.assertEqual(
            provider._request.call_args_list[-1].args[0:2],
            ("POST", f"/api/conversations/{main}/events"),
        )

    def test_two_resume_chain_ignores_prior_inputs_and_rejects_ambiguous_current_input(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        task_key = "spec-614"
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        first = provider._initial_callback_generation(main, child, task_key)
        second = provider._resumed_callback_generation(first, "resume:first")
        third = provider._resumed_callback_generation(second, "resume:second")

        def control(kind, envelope):
            return kind + "\n" + _compact_json(provider._signed_control_envelope(kind, envelope))

        mission = control("MISSION", {
            "callbackGeneration": first, "childId": str(child),
            "owningMainId": str(main), "taskKey": task_key,
        })
        resume_one = control("RESUME_MISSION", {
            "callbackGeneration": second, "childId": str(child), "context": {"answer": "A"},
            "messageKey": "resume:first", "owningMainId": str(main), "taskKey": task_key,
        })
        resume_two = control("RESUME_MISSION", {
            "callbackGeneration": third, "childId": str(child), "context": {"answer": "B"},
            "messageKey": "resume:second", "owningMainId": str(main), "taskKey": task_key,
        })

        def waiting(generation, key):
            return control("NEEDS_INPUT", {
                "callbackGeneration": generation, "childId": str(child), "kind": "NEEDS_INPUT",
                "messageKey": key, "owningMainId": str(main), "taskKey": task_key, "text": "{}",
            })

        chain = [resume_two, resume_one, mission]
        provider._event_texts = Mock(side_effect=[chain, [waiting(first, "decision:one"), waiting(second, "decision:two")]])
        self.assertFalse(provider._has_waiting_input(main, child, task_key))

        provider._event_texts = Mock(side_effect=[chain, [waiting(third, "decision:three")]])
        self.assertTrue(provider._has_waiting_input(main, child, task_key))

        provider._event_texts = Mock(side_effect=[chain, [waiting(third, "decision:three"), waiting(third, "decision:four")]])
        with self.assertRaisesRegex(ProviderError, "history is ambiguous"):
            provider._has_waiting_input(main, child, task_key)

    def test_signed_incomplete_current_input_fails_closed_before_result_delivery(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        task_key = "spec-614"
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        generation = provider._initial_callback_generation(main, child, task_key)
        mission = "MISSION\n" + _compact_json(provider._signed_control_envelope("MISSION", {
            "callbackGeneration": generation, "childId": str(child),
            "owningMainId": str(main), "taskKey": task_key,
        }))
        malformed = "NEEDS_INPUT\n" + _compact_json(provider._signed_control_envelope("NEEDS_INPUT", {
            "callbackGeneration": generation, "childId": str(child), "kind": "NEEDS_INPUT",
            "messageKey": "decision:missing-text", "owningMainId": str(main), "taskKey": task_key,
        }))
        callback = _compact_json({
            "callbackGeneration": generation, "childId": str(child), "kind": "RESULT",
            "messageKey": "result:after-malformed-input", "owningMainId": str(main),
            "taskKey": task_key, "text": "{}",
        })
        provider._request = Mock(return_value={"execution_status": "idle"})
        provider._event_texts = Mock(side_effect=[[mission], [], [mission], [malformed]])

        with self.assertRaisesRegex(ProviderError, "control envelope is invalid"):
            provider.send_child_message(
                child, main, "result:after-malformed-input", "RESULT", callback
            )
        self.assertFalse(any(
            len(call.args) > 1 and call.args[0] == "POST"
            for call in provider._request.call_args_list
        ))

    def test_signed_control_schema_rejects_invalid_fields_for_every_control_kind(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        generation = provider._initial_callback_generation(main, child, "spec-614")
        common = {
            "callbackGeneration": generation, "childId": str(child),
            "owningMainId": str(main), "taskKey": "spec-614",
        }
        valid = {
            "MISSION": dict(common),
            "RESUME_MISSION": {**common, "context": {"answer": "A"}, "messageKey": "resume:one"},
            "RESULT": {**common, "kind": "RESULT", "messageKey": "result:one", "text": "{}"},
            "NEEDS_INPUT": {**common, "kind": "NEEDS_INPUT", "messageKey": "decision:one", "text": "{}"},
            "CANCEL_MISSION": {**common, "messageKey": "cancel:one", "targetId": str(child)},
        }
        for kind, envelope in valid.items():
            with self.subTest(kind=kind, case="canonical"):
                self.assertTrue(provider._valid_control_schema(
                    kind, provider._signed_control_envelope(kind, envelope)
                ))
            cases = [
                {key: value for key, value in envelope.items() if key != "taskKey"},
                {**envelope, "taskKey": ""},
                {**envelope, "taskKey": 1},
            ]
            if kind != "MISSION":
                cases.append({**envelope, "unexpected": True})
            if kind in {"RESULT", "NEEDS_INPUT"}:
                cases.extend([
                    {key: value for key, value in envelope.items() if key != "text"},
                    {**envelope, "text": ""},
                    {**envelope, "text": {}},
                    {**envelope, "text": "x" * 100_001},
                    {**envelope, "kind": "RESULT" if kind == "NEEDS_INPUT" else "NEEDS_INPUT"},
                ])
            if kind == "RESUME_MISSION":
                cases.extend([{**envelope, "context": {}}, {**envelope, "context": []}])
            if kind == "CANCEL_MISSION":
                cases.append({**envelope, "targetId": str(uuid.uuid4())})
            for index, invalid in enumerate(cases):
                with self.subTest(kind=kind, case=index):
                    self.assertFalse(provider._valid_control_schema(
                        kind, provider._signed_control_envelope(kind, invalid)
                    ))

    def test_deterministic_uuid5_child_control_chain_is_canonical_and_foreign_ids_fail(self) -> None:
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = deterministic_child_id(main, "spec-614")
        foreign_child = deterministic_child_id(main, "spec-615")
        foreign_main = uuid.UUID("33333333-3333-4333-8333-333333333333")
        self.assertEqual(child.version, 5)
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        generation = provider._initial_callback_generation(main, child, "spec-614")
        common = {
            "callbackGeneration": generation,
            "childId": str(child),
            "owningMainId": str(main),
            "taskKey": "spec-614",
        }
        controls = {
            "MISSION": common,
            "RESULT": {**common, "kind": "RESULT", "messageKey": "result:one", "text": "{}"},
            "NEEDS_INPUT": {**common, "kind": "NEEDS_INPUT", "messageKey": "decision:one", "text": "{}"},
            "RESUME_MISSION": {**common, "context": {"answer": "A"}, "messageKey": "resume:one"},
            "CANCEL_MISSION": {**common, "messageKey": "cancel:one", "targetId": str(child)},
        }
        for kind, envelope in controls.items():
            with self.subTest(kind=kind):
                self.assertTrue(provider._valid_control_schema(
                    kind, provider._signed_control_envelope(kind, envelope)
                ))
                foreign = {**envelope, "childId": str(foreign_child)}
                if kind == "CANCEL_MISSION":
                    foreign["targetId"] = str(foreign_child)
                self.assertTrue(provider._valid_control_schema(
                    kind,
                    provider._signed_control_envelope(
                        kind, foreign
                    ),
                ))
        self.assertFalse(provider._valid_control_schema(
            "MISSION",
            provider._signed_control_envelope(
                "MISSION", {**common, "childId": "not-a-uuid"}
            ),
        ))
        mission = "MISSION\n" + _compact_json(
            provider._signed_control_envelope("MISSION", common)
        )
        foreign_wait = "NEEDS_INPUT\n" + _compact_json(
            provider._signed_control_envelope("NEEDS_INPUT", {
                **common, "childId": str(foreign_child), "kind": "NEEDS_INPUT",
                "messageKey": "decision:foreign-child", "text": "{}",
            })
        )
        foreign_owner_wait = "NEEDS_INPUT\n" + _compact_json(
            provider._signed_control_envelope("NEEDS_INPUT", {
                **common, "kind": "NEEDS_INPUT", "messageKey": "decision:foreign-main",
                "owningMainId": str(foreign_main), "text": "{}",
            })
        )
        provider._event_texts = Mock(side_effect=[[mission], [foreign_wait, foreign_owner_wait]])
        self.assertFalse(provider._has_waiting_input(main, child, "spec-614"))

    def test_terminal_cancellation_rejects_late_child_callback_and_resume(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._request = Mock(return_value={"execution_status": "cancelled"})

        late_result = provider.send_child_message(child, uuid.uuid4(), "result:late", "RESULT", "{}")
        late_resume = provider.resume_mission(child, "resume:late", "spec-614", {"answer": "A"}, uuid.uuid4())

        self.assertEqual(late_result["outcome"], "CANCELLED")
        self.assertFalse(late_result["accepted"])
        self.assertEqual(late_resume["outcome"], "CANCELLED")
        self.assertFalse(late_resume["accepted"])

    def test_paused_child_waiting_for_bound_input_rejects_late_result(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        waiting = (
            "NEEDS_INPUT\n"
            '{"childId":"22222222-2222-4222-8222-222222222222","kind":"NEEDS_INPUT","messageKey":"decision:one","owningMainId":"11111111-1111-4111-8111-111111111111","taskKey":"spec-614"}'
        )
        callback = json.dumps({
            "childId": str(child), "kind": "RESULT", "messageKey": "result:late",
            "owningMainId": str(main), "taskKey": "spec-614", "text": "{}",
            "callbackGeneration": "evxg1_" + "0" * 64,
        })
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._current_callback_generation = Mock(return_value="evxg1_" + "0" * 64)
        provider._has_waiting_input = Mock(return_value=True)
        provider._request = Mock(side_effect=[
            {"execution_status": "paused"},
            {"items": []},
            {"items": []},
            {"items": [{"llm_message": {"content": [{"text": waiting}]}}]},
        ])

        result = provider.send_child_message(child, main, "result:late", "RESULT", callback)

        self.assertEqual(
            result,
            {"accepted": False, "messageKey": "result:late", "taskKey": "spec-614", "outcome": "NEEDS_INPUT"},
        )

    def test_exact_owner_result_after_authorized_resume_is_delivered_once(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        resume = "RESUME_MISSION\n" + json.dumps(
            {"messageKey": "resume:answer", "owningMainId": str(main), "taskKey": "spec-614"},
            sort_keys=True, separators=(",", ":"),
        )
        callback = json.dumps({
            "childId": str(child), "kind": "RESULT", "messageKey": "result:after-resume",
            "owningMainId": str(main), "taskKey": "spec-614", "text": "{}",
            "callbackGeneration": "evxg1_" + "0" * 64,
        })
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._current_callback_generation = Mock(return_value="evxg1_" + "0" * 64)
        provider._has_waiting_input = Mock(return_value=False)
        provider._request = Mock(side_effect=[
            {"execution_status": "running"},
            {"items": []},
            {"items": [{"llm_message": {"content": [{"text": resume}]}}]},
            {"execution_status": "idle"},
            {},
        ])

        result = provider.send_child_message(
            child, main, "result:after-resume", "RESULT", callback
        )

        self.assertEqual(result, {"accepted": True, "messageKey": "result:after-resume"})
        self.assertEqual(
            provider._request.call_args.args[0:2], ("POST", f"/api/conversations/{main}/events")
        )

    def test_needs_input_pause_failure_never_delivers_to_parent(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        generation = "evxg1_" + "0" * 64
        callback = json.dumps({
            "callbackGeneration": generation,
            "childId": str(child),
            "kind": "NEEDS_INPUT",
            "messageKey": "decision:terminal",
            "owningMainId": str(main),
            "taskKey": "spec-614",
            "text": '{"options":["A","B"],"question":"Choose"}',
        })
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._current_callback_generation = Mock(return_value=generation)
        provider._terminal_result_key = Mock(return_value=None)
        provider._waiting_input_key = Mock(return_value=None)
        provider._request = Mock(side_effect=[
            {"execution_status": "running"},
            {},
            {"execution_status": "finished"},
        ])

        with self.assertRaisesRegex(ProviderError, "became terminal"):
            provider.send_child_message(
                child, main, "decision:terminal", "NEEDS_INPUT", callback
            )
        self.assertFalse(any(
            call.args[:2] == ("POST", f"/api/conversations/{main}/events")
            for call in provider._request.call_args_list
        ))

    def test_needs_input_exact_replay_delivers_once_and_returns_pending_outcome(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        generation = "evxg1_" + "0" * 64
        callback = json.dumps({
            "callbackGeneration": generation,
            "childId": str(child),
            "kind": "NEEDS_INPUT",
            "messageKey": "decision:pause",
            "owningMainId": str(main),
            "taskKey": "spec-614",
            "text": '{"options":["A","B"],"question":"Choose"}',
        })
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._current_callback_generation = Mock(return_value=generation)
        provider._terminal_result_key = Mock(return_value=None)
        provider._waiting_input_key = Mock(side_effect=[None, "decision:pause"])
        provider._request = Mock(side_effect=[
            {"execution_status": "running"},
            {},
            {"execution_status": "paused"},
            {"execution_status": "idle"},
            {},
            {"execution_status": "paused"},
        ])

        self.assertEqual(
            provider.send_child_message(child, main, "decision:pause", "NEEDS_INPUT", callback),
            {"accepted": True, "messageKey": "decision:pause"},
        )
        self.assertEqual(
            provider.send_child_message(child, main, "decision:pause", "NEEDS_INPUT", callback),
            {"accepted": True, "messageKey": "decision:pause", "outcome": "NEEDS_INPUT"},
        )
        parent_posts = [
            call for call in provider._request.call_args_list
            if call.args[:2] == ("POST", f"/api/conversations/{main}/events")
        ]
        self.assertEqual(len(parent_posts), 1)
        pause_index = next(
            index for index, call in enumerate(provider._request.call_args_list)
            if call.args == ("POST", f"/api/conversations/{child}/pause", {})
        )
        parent_index = provider._request.call_args_list.index(parent_posts[0])
        self.assertLess(pause_index, parent_index)

    def test_native_paused_decision_remains_authorized_for_resume(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        task_key = "spec-614"
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        generation = provider._initial_callback_generation(main, child, task_key)
        mission = "MISSION\n" + _compact_json(provider._signed_control_envelope("MISSION", {
            "callbackGeneration": generation,
            "childId": str(child),
            "owningMainId": str(main),
            "taskKey": task_key,
        }))
        events = {child: [mission], main: []}
        provider._event_texts = Mock(side_effect=lambda conversation_id: events[conversation_id])
        provider._request = Mock(side_effect=[
            {"execution_status": "running"},
            {},
            {"execution_status": "paused"},
            {"execution_status": "paused"},
            {},
        ])

        def deliver(target_id, message_key, kind, text):
            events[target_id].append(f"{kind}\n{text}")
            return {"accepted": True, "messageKey": message_key}

        provider.send_message = Mock(side_effect=deliver)
        callback = json.dumps({
            "callbackGeneration": generation,
            "childId": str(child),
            "kind": "NEEDS_INPUT",
            "messageKey": "decision:pause",
            "owningMainId": str(main),
            "taskKey": task_key,
            "text": '{"options":["A","B"],"question":"Choose"}',
        })

        self.assertEqual(
            provider.send_child_message(child, main, "decision:pause", "NEEDS_INPUT", callback),
            {"accepted": True, "messageKey": "decision:pause"},
        )
        self.assertEqual(
            provider.resume_mission(child, "resume:answer", task_key, {"answer": "A"}, main),
            {"accepted": True, "messageKey": "resume:answer", "taskKey": task_key, "outcome": "RESUMED"},
        )

    def test_callback_generation_rejects_stale_and_accepts_only_current_resumed_turn(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        task_key = "spec-614"
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        initial = provider._initial_callback_generation(main, child, task_key)
        resumed = provider._resumed_callback_generation(initial, "resume:answer")
        mission = "MISSION\n" + json.dumps(provider._signed_control_envelope("MISSION", {
            "callbackGeneration": initial, "childId": str(child),
            "owningMainId": str(main), "taskKey": task_key,
        }), sort_keys=True, separators=(",", ":"))
        resume = "RESUME_MISSION\n" + json.dumps(provider._signed_control_envelope("RESUME_MISSION", {
            "callbackGeneration": resumed, "messageKey": "resume:answer",
            "childId": str(child), "context": {"answer": "A"}, "owningMainId": str(main), "taskKey": task_key,
        }), sort_keys=True, separators=(",", ":"))
        stale = json.dumps({
            "childId": str(child), "kind": "RESULT", "messageKey": "result:stale",
            "owningMainId": str(main), "taskKey": task_key, "text": "{}",
            "callbackGeneration": initial,
        })
        provider._request = Mock(side_effect=[{"execution_status": "idle"}])
        provider._event_texts = Mock(return_value=[mission, resume])

        with self.assertRaisesRegex(ProviderError, "callback generation"):
            provider.send_child_message(child, main, "result:stale", "RESULT", stale)

        callback = json.dumps({
            "childId": str(child), "kind": "RESULT", "messageKey": "result:current",
            "owningMainId": str(main), "taskKey": task_key, "text": "{}",
            "callbackGeneration": resumed,
        })
        result_event = "RESULT\n" + json.dumps(
            provider._signed_control_envelope("RESULT", json.loads(callback)),
            sort_keys=True, separators=(",", ":"),
        )
        provider._request = Mock(side_effect=[
            {"execution_status": "idle"}, {"execution_status": "idle"}, {},
            {"execution_status": "idle"},
        ])
        provider._event_texts = Mock(side_effect=[
            [mission, resume], [], [mission, resume], [], [mission, resume], [result_event], [mission, resume],
        ])

        self.assertEqual(
            provider.send_child_message(child, main, "result:current", "RESULT", callback),
            {"accepted": True, "messageKey": "result:current"},
        )
        self.assertEqual(
            provider.send_child_message(child, main, "result:current", "RESULT", callback),
            {"accepted": True, "messageKey": "result:current", "outcome": "RESULT"},
        )

    def test_callback_generation_history_or_identity_failure_prevents_parent_delivery(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        task_key = "spec-614"
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        initial = provider._initial_callback_generation(main, child, task_key)
        valid = {
            "callbackGeneration": initial, "childId": str(child), "kind": "RESULT",
            "messageKey": "result:current", "owningMainId": str(main),
            "taskKey": task_key, "text": "{}",
        }
        mission = "MISSION\n" + json.dumps(
            {"callbackGeneration": initial, "childId": str(child),
             "owningMainId": str(main), "taskKey": task_key},
            sort_keys=True, separators=(",", ":"),
        )
        incomplete = "MISSION\n" + json.dumps(
            {"childId": str(child), "owningMainId": str(main), "taskKey": task_key},
            sort_keys=True, separators=(",", ":"),
        )
        for name, callback, history in (
            ("missing", {key: value for key, value in valid.items() if key != "callbackGeneration"}, None),
            ("malformed", {**valid, "callbackGeneration": "not-a-generation"}, None),
            ("foreign", {**valid, "owningMainId": str(uuid.uuid4())}, None),
            ("incomplete", valid, [incomplete]),
            ("ambiguous", valid, [mission, mission]),
        ):
            with self.subTest(name=name):
                provider._request = Mock(return_value={"execution_status": "idle"})
                provider._event_texts = Mock(return_value=history) if history is not None else Mock()
                with self.assertRaisesRegex(ProviderError, "callback (generation|identity)"):
                    provider.send_child_message(
                        child, main, "result:current", "RESULT", json.dumps(callback)
                    )
                self.assertFalse(any(
                    len(call.args) > 1 and call.args[0] == "POST"
                    for call in provider._request.call_args_list
                ))

    def test_resume_generation_history_failure_prevents_resume_mutation(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._request = Mock(side_effect=[
            {"execution_status": "idle"}, {"items": []}, {"items": []}, {"items": []},
        ])

        with self.assertRaisesRegex(ProviderError, "callback generation history"):
            provider.resume_mission(child, "resume:missing", "spec-614", {"answer": "A"}, main)

        self.assertFalse(any(
            len(call.args) > 1 and call.args[0] == "POST"
            for call in provider._request.call_args_list
        ))

    def test_truncated_callback_generation_history_prevents_parent_delivery(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        generation = provider._initial_callback_generation(main, child, "spec-614")
        callback = json.dumps({
            "callbackGeneration": generation, "childId": str(child), "kind": "RESULT",
            "messageKey": "result:truncated", "owningMainId": str(main),
            "taskKey": "spec-614", "text": "{}",
        })
        provider._request = Mock(return_value={"execution_status": "idle"})
        provider._event_texts = Mock(
            side_effect=ProviderError("OpenHands control history exceeds bounded page budget")
        )

        with self.assertRaisesRegex(ProviderError, "page budget"):
            provider.send_child_message(child, main, "result:truncated", "RESULT", callback)

        self.assertFalse(any(
            len(call.args) > 1 and call.args[0] == "POST"
            for call in provider._request.call_args_list
        ))

    def test_control_history_fails_closed_after_a_finite_page_budget(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._request = Mock(side_effect=[
            {"items": [], "next_page_id": f"page-{index}"}
            for index in range(8)
        ])

        with self.assertRaisesRegex(ProviderError, "page budget"):
            provider._event_texts(child)

        self.assertEqual(provider._request.call_count, 8)

    def test_foreign_result_or_resume_evidence_cannot_block_owned_cancellation(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        foreign = "33333333-3333-4333-8333-333333333333"
        result = (
            "RESULT\n"
            + json.dumps({"childId": str(child), "kind": "RESULT", "messageKey": "result:foreign", "owningMainId": foreign, "taskKey": "spec-614"}, sort_keys=True, separators=(",", ":"))
        )
        resume = "RESUME_MISSION\n" + json.dumps(
            {"messageKey": "resume:foreign", "owningMainId": foreign, "taskKey": "spec-614"},
            sort_keys=True, separators=(",", ":"),
        )
        for name, parent_events, child_events in (
            ("result", [result], []),
            ("resume", [], [resume]),
        ):
            with self.subTest(name=name):
                provider = OpenHandsProvider("http://openhands", "key", "http://public", sleeper=lambda _seconds: None)
                provider._has_terminal_result = Mock(return_value=False)
                provider._has_resume = Mock(return_value=False)
                provider._current_callback_generation = Mock(return_value="evxg1_" + "0" * 64)
                provider._request = Mock(side_effect=[
                    {"execution_status": "running"},
                    {"items": [{"llm_message": {"content": [{"text": text}]}} for text in parent_events]},
                    {"items": [{"llm_message": {"content": [{"text": text}]}} for text in child_events]},
                    {},
                    {"execution_status": "paused"},
                    {},
                    {"execution_status": "cancelled"},
                ])

                outcome = provider.cancel_mission(child, "cancel:one", "spec-614", main)

                self.assertTrue(outcome["accepted"])
                self.assertEqual(outcome["outcome"], "CANCELLED")
                self.assertTrue(any(
                    len(call.args) > 1 and call.args[1].endswith("/interrupt")
                    for call in provider._request.call_args_list
                ))

    def test_resume_evidence_carries_owning_main_identity(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._request = Mock(side_effect=[
            {"execution_status": "idle"}, {"items": []}, {"items": []}, {},
        ])
        initial = "evxg1_" + "0" * 64
        provider._current_callback_generation = Mock(return_value=initial)
        provider._has_waiting_input = Mock(return_value=True)

        provider.resume_mission(
            child, "resume:plan-reviewed", "spec-author-604",
            {"reviewOutcome": "PASS", "planCommit": "a" * 40}, main,
        )

        envelope = json.loads(
            provider._request.call_args.args[2]["content"][0]["text"].removeprefix("RESUME_MISSION\n")
        )
        self.assertEqual(envelope["owningMainId"], str(main))
        self.assertEqual(envelope["childId"], str(child))
        self.assertTrue(provider._valid_control_signature("RESUME_MISSION", envelope))

    def test_cancellation_replay_and_replacement_proof_page_past_one_hundred_events(self) -> None:
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = deterministic_child_id(main, "spec-614")
        self.assertEqual(child.version, 5)
        noise = [{"llm_message": {"content": [{"text": f"noise:{index}"}]}} for index in range(100)]
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        generation = provider._initial_callback_generation(main, child, "spec-614")
        cancel = "CANCEL_MISSION\n" + json.dumps(provider._signed_control_envelope("CANCEL_MISSION", {
            "callbackGeneration": generation, "childId": str(child), "messageKey": "cancel:one", "owningMainId": str(main),
            "targetId": str(child), "taskKey": "spec-614",
        }), sort_keys=True, separators=(",", ":"))
        provider._current_callback_generation = Mock(return_value=generation)
        provider._request = Mock(side_effect=[
            {"execution_status": "cancelled"},
            {"items": noise, "next_page_id": "older"},
            {"items": [{"llm_message": {"content": [{"text": cancel}]}}], "next_page_id": None},
            {"execution_status": "cancelled"},
            {"items": noise, "next_page_id": "older"},
            {"items": [{"llm_message": {"content": [{"text": cancel}]}}], "next_page_id": None},
        ])

        replay = provider.cancel_mission(child, "cancel:one", "spec-614", main)
        replacement_proof = provider.replacement_cancelled(
            child, "spec-614", "cancel:one", main
        )

        self.assertTrue(replay["accepted"])
        self.assertTrue(replacement_proof)
        self.assertEqual(
            provider._request.call_args_list[2].args,
            ("GET", f"/api/conversations/{child}/events/search?limit=100&sort_order=TIMESTAMP_DESC&source=user&page_id=older"),
        )
        self.assertEqual(
            provider._request.call_args_list[5].args,
            ("GET", f"/api/conversations/{child}/events/search?limit=100&sort_order=TIMESTAMP_DESC&source=user&page_id=older"),
        )

    def test_control_history_filters_provider_noise_before_bounded_pagination(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._request = Mock(return_value={"items": [], "next_page_id": None})

        self.assertEqual(provider._event_texts(child), [])

        self.assertEqual(
            provider._request.call_args.args,
            (
                "GET",
                f"/api/conversations/{child}/events/search?limit=100"
                "&sort_order=TIMESTAMP_DESC&source=user",
            ),
        )

    def test_paused_provider_rejects_write_create_and_resume_before_durable_mutation(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider(
            "http://openhands", "key", "http://public", write_mission_admission_paused=True
        )
        provider._request = Mock()

        with self.assertRaisesRegex(ProviderError, "^write_mission_admission_paused$"):
            self.create_provider_child(
                provider, parent, child, "writer", "writer-768",
                {"checkout": {"repository": "EvexU2/evex-u-core", "branch": "fix/768", "headSha": "a" * 40}},
                "evx1_opaque", frozenset(),
            )
        provider._request.assert_not_called()

        provider._request = Mock(return_value={
            "tags": {"project": "evex-u", "evexrole": "role-child", "evexchildrole": "spec", "evexparent": str(parent), "evextask": "spec-768"},
        })
        with self.assertRaisesRegex(ProviderError, "^write_mission_admission_paused$"):
            provider.resume_mission(child, "resume:768", "spec-768", {"verified": True}, parent)
        self.assertEqual(provider._request.call_args_list[0].args, ("GET", f"/api/conversations/{child}"))
        self.assertEqual(len(provider._request.call_args_list), 1)

        resumed = OpenHandsProvider("http://openhands", "key", "http://public")
        resumed._request = Mock(side_effect=[
            {"execution_status": "idle"}, {"items": []}, {"items": []}, {},
        ])
        resumed._current_callback_generation = Mock(return_value="evxg1_" + "0" * 64)
        resumed._has_waiting_input = Mock(return_value=True)
        self.assertTrue(
            resumed.resume_mission(child, "resume:after-proof", "spec-768", {"forwardProof": "PASS"}, parent)["accepted"]
        )
        self.assertEqual(resumed._request.call_args.args[0:2], ("POST", f"/api/conversations/{child}/events"))

    def test_live_inventory_and_main_routed_drain_are_stateless_and_terminal_safe(self) -> None:
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        tags = {"project": "evex-u", "evexrole": "role-child", "evexchildrole": "writer", "evexparent": str(main), "evextask": "writer-768"}
        active = {"id": str(child), "tags": tags, "execution_status": "running"}
        terminal = {"id": str(child), "tags": tags, "execution_status": "finished"}
        provider = OpenHandsProvider("http://openhands", "key", "http://public", sleeper=lambda _seconds: None)
        provider._request = Mock(side_effect=[
            {"items": [active], "next_page_id": None}, {"items": [terminal], "next_page_id": None}, active,
            {"execution_status": "idle"}, {},
        ])

        first = provider.write_mission_inventory()
        second = provider.write_mission_inventory()
        self.assertFalse(first[0]["terminal"])
        self.assertTrue(second[0]["terminal"])
        result = provider.request_write_mission_drain(first[0])

        self.assertEqual(result, {"accepted": True, "terminal": False, "childId": str(child), "messageKey": f"quiesce:{child}"})
        event = provider._request.call_args_list[-1].args
        self.assertEqual(event[0:2], ("POST", f"/api/conversations/{main}/events"))
        self.assertIn("QUIESCE_WRITE_MISSION", event[2]["content"][0]["text"])
        self.assertFalse(any(call.args[1] == f"/api/conversations/{child}/events" for call in provider._request.call_args_list if len(call.args) > 1))

    def test_inventory_exhausts_pages_and_routes_later_page_writer_for_drain(self) -> None:
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        tags = {"project": "evex-u", "evexrole": "role-child", "evexchildrole": "writer", "evexparent": str(main), "evextask": "writer-768"}
        writer = {"id": str(child), "tags": tags, "execution_status": "running"}
        provider = OpenHandsProvider("http://openhands", "key", "http://public", sleeper=lambda _seconds: None)
        provider._request = Mock(side_effect=[
            {"items": [], "next_page_id": "cursor/2"},
            {"items": [writer], "next_page_id": None},
            writer, {"execution_status": "idle"}, {},
        ])

        inventory = provider.write_mission_inventory()
        self.assertEqual(inventory, [{"childId": str(child), "owningMainId": str(main), "role": "writer", "taskKey": "writer-768", "terminal": False}])
        provider.request_write_mission_drain(inventory[0])

        self.assertEqual(provider._request.call_args_list[0].args, ("GET", "/api/conversations?limit=100"))
        self.assertEqual(provider._request.call_args_list[1].args, ("GET", "/api/conversations?limit=100&page_id=cursor%2F2"))
        self.assertEqual(provider._request.call_args_list[-1].args[0:2], ("POST", f"/api/conversations/{main}/events"))

    def test_inventory_fails_closed_for_incomplete_or_repeated_pagination(self) -> None:
        malformed = [
            [],
            {"items": []},
            {"items": [], "next_page_id": ""},
            {"items": [], "next_page_id": 2},
        ]
        for response in malformed:
            with self.subTest(response=response):
                provider = OpenHandsProvider("http://openhands", "key", "http://public")
                provider._request = Mock(return_value=response)
                with self.assertRaisesRegex(ProviderError, "inventory is incomplete"):
                    provider.write_mission_inventory()

        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._request = Mock(side_effect=[
            {"items": [], "next_page_id": "repeat"},
            {"items": [], "next_page_id": "repeat"},
        ])
        with self.assertRaisesRegex(ProviderError, "inventory is incomplete"):
            provider.write_mission_inventory()

    def test_authenticated_pr_boundary_returns_only_exact_admission_facts(self) -> None:
        provider = OpenHandsProvider(
            "http://openhands",
            "key",
            "http://public",
            github_token="provider-held-token",
        )
        payload = {
            "html_url": "https://github.com/EvexU2/evex-agent-messaging/pull/42",
            "number": 42,
            "state": "open",
            "draft": True,
            "base": {"repo": {"full_name": "EvexU2/evex-agent-messaging"}},
            "head": {"sha": "b" * 40},
        }
        response = MagicMock()
        response.headers = {"Content-Length": str(len(json.dumps(payload).encode()))}
        response.read.return_value = json.dumps(payload).encode()
        context = MagicMock()
        context.__enter__.return_value = response

        with patch(
            "evex_agent_messaging.provider.urllib.request.urlopen",
            return_value=context,
        ) as opened:
            facts = provider._read_specification_pr(
                "https://github.com/EvexU2/evex-agent-messaging/pull/42",
                "EvexU2/evex-agent-messaging",
            )

        request = opened.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/EvexU2/evex-agent-messaging/pulls/42",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer provider-held-token")
        self.assertEqual(
            facts,
            {
                "headSha": "b" * 40,
                "number": 42,
                "repository": "EvexU2/evex-agent-messaging",
                "url": "https://github.com/EvexU2/evex-agent-messaging/pull/42",
            },
        )
        self.assertNotIn("provider-held-token", repr(facts))

        response.read.side_effect = http.client.IncompleteRead(b"{", 2)
        with patch(
            "evex_agent_messaging.provider.urllib.request.urlopen",
            return_value=context,
        ):
            with self.assertRaisesRegex(ProviderError, "PR read failed"):
                provider._read_specification_pr(
                    "https://github.com/EvexU2/evex-agent-messaging/pull/42",
                    "EvexU2/evex-agent-messaging",
                )

    def test_new_reviewer_admission_authenticates_bound_pr_before_checkout_mutation(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider(
            "http://openhands",
            "key",
            "http://public",
            github_token="provider-held-token",
        )
        order = []
        provider._request = Mock(
            side_effect=[
                ProviderError("missing", status=404),
                {"active_agent_profile_id": "acp"},
                {},
                {},
                {},
            ]
        )
        provider._read_specification_pr = Mock(
            side_effect=lambda *_args: order.append("pr")
            or {
                "headSha": "a" * 40,
                "number": 42,
                "repository": "EvexU2/evex-agent-messaging",
                "url": "https://github.com/EvexU2/evex-agent-messaging/pull/42",
            }
        )
        provider._ensure_checkout = Mock(
            side_effect=lambda *_args: order.append("checkout")
        )
        provider._validate_existing_checkout = Mock()
        mission = {
            "links": {
                "issue": "https://github.com/EvexU2/evex-u-workspace/issues/836",
                "specificationPr": "https://github.com/EvexU2/evex-agent-messaging/pull/42",
            },
            "checkout": {
                "repository": "EvexU2/evex-agent-messaging",
                "branch": "review/issue-836",
                "headSha": "a" * 40,
            },
            "skills": ["evex-delivery-reviewer"],
        }

        self.create_provider_child(
            provider,
            parent,
            child,
            "reviewer",
            "issue-836-reviewer",
            mission,
            "evx1_opaque",
            frozenset(),
        )

        self.assertEqual(order, ["pr", "checkout"])

    def test_authenticated_pr_boundary_rejects_ineligible_or_foreign_responses(self) -> None:
        canonical = "https://github.com/EvexU2/evex-agent-messaging/pull/42"
        repository = "EvexU2/evex-agent-messaging"
        valid = {
            "html_url": canonical,
            "number": 42,
            "state": "open",
            "draft": True,
            "base": {"repo": {"full_name": repository}},
            "head": {"sha": "b" * 40},
        }
        invalid = {
            "closed": {**valid, "state": "closed"},
            "non-draft": {**valid, "draft": False},
            "foreign-repository": {
                **valid,
                "base": {"repo": {"full_name": "EvexU2/foreign"}},
            },
            "foreign-identity": {**valid, "html_url": canonical.replace("42", "43")},
            "unreachable-shape": {**valid, "head": {"sha": "not-a-commit"}},
        }
        provider = OpenHandsProvider(
            "http://openhands",
            "key",
            "http://public",
            github_token="provider-held-token",
        )
        for name, payload in invalid.items():
            with self.subTest(name=name):
                raw = json.dumps(payload).encode()
                response = MagicMock()
                response.headers = {"Content-Length": str(len(raw))}
                response.read.return_value = raw
                context = MagicMock()
                context.__enter__.return_value = response
                with patch(
                    "evex_agent_messaging.provider.urllib.request.urlopen",
                    return_value=context,
                ):
                    with self.assertRaises(ProviderError):
                        provider._read_specification_pr(canonical, repository)

        missing = urllib.error.HTTPError(canonical, 404, "missing", {}, None)
        with patch(
            "evex_agent_messaging.provider.urllib.request.urlopen",
            side_effect=missing,
        ):
            with self.assertRaisesRegex(ProviderError, "PR read failed"):
                provider._read_specification_pr(canonical, repository)

        without_credential = OpenHandsProvider(
            "http://openhands", "key", "http://public", github_token=""
        )
        with self.assertRaisesRegex(ProviderError, "credential is unavailable"):
            without_credential._read_specification_pr(canonical, repository)

    def test_reviewer_resume_fast_forwards_to_authenticated_head_and_replays_once(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        task_key = "issue-836-reviewer"
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            provider, history, child_events, _ = self._reviewer_resume_provider(
                workspace, child, main, task_key
            )
            pr = {
                "headSha": history["repaired"],
                "number": 42,
                "repository": "EvexU2/evex-agent-messaging",
                "url": "https://github.com/EvexU2/evex-agent-messaging/pull/42",
            }
            provider._read_specification_pr = Mock(side_effect=[pr, pr])

            def request(method, path, body=None, **_kwargs):
                if method == "GET":
                    return {"execution_status": "finished"}
                child_events.insert(0, body["content"][0]["text"])
                return {}

            provider._request = Mock(side_effect=request)
            context = {
                "currentRevision": history["repaired"],
                "findings": ["P2-1"],
            }

            first = provider.resume_mission(
                child, "resume:repaired", task_key, context, main
            )

            self.assertEqual(first["outcome"], "RESUMED")
            self.assertEqual(
                self._git_run(history["checkout"], "rev-parse", "HEAD"),
                history["repaired"],
            )
            self.assertEqual(
                (history["checkout"] / "candidate.txt").read_text(), "repaired\n"
            )
            posts = [call for call in provider._request.call_args_list if call.args[0] == "POST"]
            self.assertEqual(len(posts), 1)
            envelope = json.loads(
                posts[0].args[2]["content"][0]["text"].removeprefix("RESUME_MISSION\n")
            )
            self.assertTrue(provider._valid_control_signature("RESUME_MISSION", envelope))

            provider._request.reset_mock()
            provider._read_specification_pr.reset_mock()
            replay = provider.resume_mission(
                child, "resume:repaired", task_key, context, main
            )

            self.assertEqual(replay, first)
            self.assertFalse(
                any(call.args[0] == "POST" for call in provider._request.call_args_list)
            )
            provider._read_specification_pr.assert_not_called()

    def test_reviewer_resume_interruption_and_uncertain_delivery_converge_statelessly(self) -> None:
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain), tempfile.TemporaryDirectory() as temporary:
                child = uuid.UUID("22222222-2222-4222-8222-222222222222")
                main = uuid.UUID("11111111-1111-4111-8111-111111111111")
                task_key = "issue-836-reviewer"
                provider, history, child_events, _ = self._reviewer_resume_provider(
                    Path(temporary), child, main, task_key
                )
                pr = {
                    "headSha": history["repaired"],
                    "number": 42,
                    "repository": "EvexU2/evex-agent-messaging",
                    "url": "https://github.com/EvexU2/evex-agent-messaging/pull/42",
                }
                provider._read_specification_pr = Mock(return_value=pr)
                first_post = True

                def request(method, path, body=None, **_kwargs):
                    nonlocal first_post
                    if method == "GET":
                        return {"execution_status": "finished"}
                    if first_post:
                        first_post = False
                        if uncertain:
                            child_events.insert(0, body["content"][0]["text"])
                        raise ProviderError("interrupted event delivery")
                    child_events.insert(0, body["content"][0]["text"])
                    return {}

                provider._request = Mock(side_effect=request)
                context = {"currentRevision": history["repaired"]}
                with self.assertRaisesRegex(ProviderError, "interrupted"):
                    provider.resume_mission(
                        child, "resume:repaired", task_key, context, main
                    )
                self.assertEqual(
                    self._git_run(history["checkout"], "rev-parse", "HEAD"),
                    history["repaired"],
                )

                result = provider.resume_mission(
                    child, "resume:repaired", task_key, context, main
                )

                self.assertTrue(result["accepted"])
                posts = [
                    call for call in provider._request.call_args_list
                    if call.args[0] == "POST"
                ]
                self.assertEqual(len(posts), 1 if uncertain else 2)

    def test_reviewer_resume_final_pr_flip_fails_before_turn_delivery(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        task_key = "issue-836-reviewer"
        with tempfile.TemporaryDirectory() as temporary:
            provider, history, _, _ = self._reviewer_resume_provider(
                Path(temporary), child, main, task_key
            )
            pr = {
                "headSha": history["repaired"],
                "number": 42,
                "repository": "EvexU2/evex-agent-messaging",
                "url": "https://github.com/EvexU2/evex-agent-messaging/pull/42",
            }
            provider._read_specification_pr = Mock(
                side_effect=[pr, ProviderError("GitHub specification PR must be open and Draft")]
            )
            provider._request = Mock(return_value={"execution_status": "finished"})

            with self.assertRaisesRegex(ProviderError, "open and Draft"):
                provider.resume_mission(
                    child,
                    "resume:eligibility-flip",
                    task_key,
                    {"currentRevision": history["repaired"]},
                    main,
                )

            self.assertFalse(
                any(call.args[0] == "POST" for call in provider._request.call_args_list)
            )

    def test_concurrent_reviewer_resumes_emit_one_event_and_conflicts_fail_closed(self) -> None:
        for conflicting in (False, True):
            with self.subTest(conflicting=conflicting), tempfile.TemporaryDirectory() as temporary:
                child = uuid.UUID("22222222-2222-4222-8222-222222222222")
                main = uuid.UUID("11111111-1111-4111-8111-111111111111")
                task_key = "issue-836-reviewer"
                provider, history, child_events, _ = self._reviewer_resume_provider(
                    Path(temporary), child, main, task_key
                )
                pr = {
                    "headSha": history["repaired"],
                    "number": 42,
                    "repository": "EvexU2/evex-agent-messaging",
                    "url": "https://github.com/EvexU2/evex-agent-messaging/pull/42",
                }
                provider._read_specification_pr = Mock(return_value=pr)
                posted = []

                def request(method, path, body=None, **_kwargs):
                    if method == "GET":
                        return {"execution_status": "finished"}
                    posted.append(body["content"][0]["text"])
                    child_events.insert(0, body["content"][0]["text"])
                    return {}

                provider._request = Mock(side_effect=request)
                barrier = threading.Barrier(2)
                outcomes = []

                def resume(revision):
                    barrier.wait()
                    try:
                        outcomes.append(
                            provider.resume_mission(
                                child,
                                "resume:concurrent",
                                task_key,
                                {"currentRevision": revision},
                                main,
                            )
                        )
                    except ProviderError as exc:
                        outcomes.append(exc)

                revisions = [history["repaired"], history["repaired"]]
                if conflicting:
                    revisions[1] = history["reviewed"]
                threads = [
                    threading.Thread(target=resume, args=(revision,))
                    for revision in revisions
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                self.assertEqual(len(posted), 1)
                accepted = [
                    value for value in outcomes
                    if isinstance(value, dict) and value.get("accepted")
                ]
                self.assertEqual(len(accepted), 2 if not conflicting else 1)
                if conflicting:
                    self.assertEqual(
                        len([value for value in outcomes if isinstance(value, ProviderError)]),
                        1,
                    )

    def test_reviewer_resume_fail_closed_matrix_delivers_no_turn(self) -> None:
        cases = (
            "missing-pr",
            "unreachable-head",
            "stale-final-head",
            "closed-pr",
            "non-draft-pr",
            "foreign-pr",
            "non-descendant",
            "dirty-checkout",
            "mismatched-branch",
            "mismatched-origin",
            "revision-mismatch",
            "unauthorized-context",
            "ambiguous-event",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                child = uuid.UUID("22222222-2222-4222-8222-222222222222")
                main = uuid.UUID("11111111-1111-4111-8111-111111111111")
                task_key = "issue-836-reviewer"
                provider, history, child_events, _ = self._reviewer_resume_provider(
                    Path(temporary), child, main, task_key
                )
                good = {
                    "headSha": history["repaired"],
                    "number": 42,
                    "repository": "EvexU2/evex-agent-messaging",
                    "url": "https://github.com/EvexU2/evex-agent-messaging/pull/42",
                }
                context = {"currentRevision": history["repaired"]}
                provider._read_specification_pr = Mock(return_value=good)
                if case == "missing-pr":
                    mission = json.loads(child_events[0].removeprefix("MISSION\n"))
                    mission.pop("controlSignature")
                    mission["links"].pop("specificationPr")
                    child_events[0] = "MISSION\n" + _compact_json(
                        provider._signed_control_envelope("MISSION", mission)
                    )
                elif case == "unreachable-head":
                    provider._read_specification_pr.return_value = {
                        **good, "headSha": "f" * 40
                    }
                    context["currentRevision"] = "f" * 40
                elif case == "stale-final-head":
                    provider._read_specification_pr = Mock(
                        side_effect=[good, {**good, "headSha": history["reviewed"]}]
                    )
                elif case in {"closed-pr", "non-draft-pr", "foreign-pr"}:
                    provider._read_specification_pr.side_effect = ProviderError(
                        f"GitHub specification PR rejected: {case}"
                    )
                elif case == "non-descendant":
                    source = history["source"]
                    self._git_run(source, "switch", "-c", "divergent", history["original"])
                    (source / "candidate.txt").write_text("divergent\n")
                    self._git_run(source, "commit", "-am", "divergent")
                    divergent = self._git_run(source, "rev-parse", "HEAD")
                    provider._read_specification_pr.return_value = {
                        **good, "headSha": divergent
                    }
                    context["currentRevision"] = divergent
                elif case == "dirty-checkout":
                    (history["checkout"] / "candidate.txt").write_text("dirty\n")
                elif case == "mismatched-branch":
                    mission = json.loads(child_events[0].removeprefix("MISSION\n"))
                    mission.pop("controlSignature")
                    mission["checkout"]["branch"] = "review/other"
                    child_events[0] = "MISSION\n" + _compact_json(
                        provider._signed_control_envelope("MISSION", mission)
                    )
                elif case == "mismatched-origin":
                    self._git_run(
                        history["checkout"],
                        "remote",
                        "set-url",
                        "origin",
                        "https://github.com/EvexU2/foreign.git",
                    )
                elif case == "revision-mismatch":
                    context["currentRevision"] = history["reviewed"]
                elif case == "unauthorized-context":
                    context["url"] = (
                        "https://github.com/EvexU2/evex-agent-messaging/pull/43"
                    )
                elif case == "ambiguous-event":
                    initial = provider._initial_callback_generation(main, child, task_key)
                    resume = provider._signed_control_envelope(
                        "RESUME_MISSION",
                        {
                            "callbackGeneration": provider._resumed_callback_generation(
                                initial, "resume:repaired"
                            ),
                            "childId": str(child),
                            "context": context,
                            "messageKey": "resume:repaired",
                            "owningMainId": str(main),
                            "taskKey": task_key,
                        },
                    )
                    event = "RESUME_MISSION\n" + _compact_json(resume)
                    child_events[0:0] = [event, event]
                provider._request = Mock(return_value={"execution_status": "finished"})

                with self.assertRaises(ProviderError):
                    provider.resume_mission(
                        child, "resume:repaired", task_key, context, main
                    )

                self.assertFalse(
                    any(call.args[0] == "POST" for call in provider._request.call_args_list)
                )


if __name__ == "__main__":
    unittest.main()
