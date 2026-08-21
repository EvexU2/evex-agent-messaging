from __future__ import annotations

from unittest.mock import Mock
from pathlib import Path
import subprocess
import tempfile
import uuid
import unittest

from evex_agent_messaging.provider import OpenHandsProvider, ProviderError


class OpenHandsProviderTest(unittest.TestCase):
    def test_callback_waits_for_busy_main_before_delivery(self) -> None:
        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        provider = OpenHandsProvider("http://openhands", "key", "http://public", sleeper=lambda _seconds: None)
        provider._request = Mock(side_effect=[
            {"execution_status": "running"},
            {"execution_status": "idle"},
            {},
        ])

        result = provider.send_message(main, "terminal:child:review", "RECOVERY_WAKE", "{}")

        self.assertTrue(result["accepted"])
        event = provider._request.call_args_list[2].args
        self.assertEqual(event[0:2], ("POST", f"/api/conversations/{main}/events"))
        self.assertTrue(event[2]["run"])

    def test_child_creation_installs_async_terminal_recovery_hook(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenHandsProvider(
                "http://openhands",
                "key",
                "http://public",
                completion_hook_url="http://messaging/completion-hook",
                workspace_root=temporary,
            )
            provider._ensure_checkout = Mock()
            provider._has_user_message = Mock(return_value=False)
            provider.wait_until_terminal = Mock(return_value="finished")
            provider._restore_checkout_after_bootstrap = Mock()
            provider._request = Mock(side_effect=[
                ProviderError("missing", status=404),
                {"active_agent_profile_id": "acp"},
                {
                    "agent_settings": {
                        "mcp_config": {
                            "mcpServers": {
                                "evex_agent_messaging": {"url": "http://messaging/mcp"},
                                "evex_runtime": {"url": "http://runtime/mcp"},
                            }
                        }
                    }
                },
                {"id": str(child)},
                {},
                {},
            ])

            provider.create_child(
                parent,
                child,
                "reviewer",
                "review-612",
                {"checkout": {"repository": "EvexU2/evex-u-core", "branch": "fix/612", "headSha": "a" * 40}},
                "evx1_opaque",
                frozenset(),
            )

        create = provider._request.call_args_list[3].args[2]
        hook = create["hook_config"]["stop"][0]["hooks"][0]
        self.assertTrue(hook["async"])
        self.assertIn("http://messaging/completion-hook", hook["command"])
        self.assertIn("evx1_opaque", hook["command"])
        self.assertIn("--retry 2", hook["command"])
        admission = create["hook_config"]["pre_tool_use"][0]["hooks"][0]
        self.assertIn(".evex-admission", admission["command"])
        self.assertIn(str(child), admission["command"])
        self.assertIn("if test -f", hook["command"])
        self.assertEqual(
            create["mcp_config"],
            {"mcpServers": {"evex_agent_messaging": {"url": "http://messaging/mcp", "auth": {"strategy": "bearer", "value": "evx1_opaque"}}}},
        )
        self.assertEqual(create["secrets"]["EVEX_AGENT_ROLE"]["value"], "reviewer")
        self.assertEqual(create["secrets"]["EVEX_AGENT_INSTANCE_ID"]["value"], str(child))
        self.assertIn("Never call OpenHands provider-control APIs", create["agent_launch_additions"]["system_message_suffix_append"])
        bootstrap_event = provider._request.call_args_list[4].args[2]["content"][0]["text"]
        self.assertTrue(bootstrap_event.startswith("PROVIDER_ADMISSION\n"))
        mission_event = provider._request.call_args_list[5].args[2]["content"][0]["text"]
        self.assertTrue(mission_event.startswith("MISSION\n{"))
        provider.wait_until_terminal.assert_called_once_with(child)
        provider._restore_checkout_after_bootstrap.assert_called_once()

    def test_integrated_mission_receives_runtime_mcp_explicitly(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenHandsProvider(
                "http://openhands", "key", "http://public", workspace_root=temporary
            )
            provider._ensure_checkout = Mock()
            provider._has_user_message = Mock(return_value=False)
            provider.wait_until_terminal = Mock(return_value="finished")
            provider._restore_checkout_after_bootstrap = Mock()
            config = {
                "mcpServers": {
                    "evex_agent_messaging": {"url": "http://messaging/mcp"},
                    "evex_runtime": {"url": "http://runtime/mcp"},
                }
            }
            provider._request = Mock(side_effect=[
                ProviderError("missing", status=404),
                {"active_agent_profile_id": "acp"},
                {"agent_settings": {"mcp_config": config}},
                {"id": str(child)},
                {},
                {},
            ])

            provider.create_child(
                parent,
                child,
                "qa",
                "qa-integrated",
                {"checkout": {"repository": "EvexU2/evex-u-core", "branch": "fix/qa", "headSha": "a" * 40}},
                "evx1_opaque",
                frozenset({"runtime_environment"}),
            )

        create = provider._request.call_args_list[3].args[2]
        self.assertEqual(
            create["mcp_config"],
            {
                "mcpServers": {
                    "evex_agent_messaging": {
                        "url": "http://messaging/mcp",
                        "auth": {"strategy": "bearer", "value": "evx1_opaque"},
                    },
                    "evex_runtime": {"url": "http://runtime/mcp"},
                }
            },
        )

    def test_child_mission_is_not_sent_when_post_bootstrap_admission_fails(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenHandsProvider(
                "http://openhands", "key", "http://public", workspace_root=temporary
            )
            provider._ensure_checkout = Mock()
            provider._has_user_message = Mock(return_value=False)
            provider.wait_until_terminal = Mock(return_value="finished")
            provider._restore_checkout_after_bootstrap = Mock(
                side_effect=ProviderError("Child checkout validation failed")
            )
            provider._request = Mock(side_effect=[
                ProviderError("missing", status=404),
                {"active_agent_profile_id": "acp"},
                {"agent_settings": {"mcp_config": {}}},
                {"id": str(child)},
                {},
            ])

            with self.assertRaisesRegex(ProviderError, "checkout validation"):
                provider.create_child(
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
        self.assertEqual(len(delivered), 1)
        self.assertTrue(delivered[0].startswith("PROVIDER_ADMISSION\n"))

    def test_concurrent_create_reuses_matching_child_and_continues_admission(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        with tempfile.TemporaryDirectory() as temporary:
            checkout_path = str(Path(temporary) / f"child-{child}")
            provider = OpenHandsProvider(
                "http://openhands", "key", "http://public", workspace_root=temporary
            )
            provider._ensure_checkout = Mock()
            provider._has_user_message = Mock(return_value=False)
            provider.wait_until_terminal = Mock(return_value="finished")
            provider._restore_checkout_after_bootstrap = Mock()
            existing = {
                "id": str(child),
                "workspace": {"working_dir": checkout_path},
                "tags": {
                    "project": "evex-u",
                    "evexrole": "role-child",
                    "evextask": "writer-612",
                    "evexparent": str(parent),
                    "evexchildrole": "writer",
                },
            }
            provider._request = Mock(side_effect=[
                ProviderError("missing", status=404),
                {"active_agent_profile_id": "acp"},
                {"agent_settings": {"mcp_config": {}}},
                ProviderError("conflict", status=409),
                existing,
                {},
                {},
            ])

            result = provider.create_child(
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
        provider._restore_checkout_after_bootstrap.assert_called_once()

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
                provider.create_child(parent, child, "reviewer", "review-612", mission, "evx1_opaque", frozenset())

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
                provider.create_child(parent, child, "reviewer", "review-612", mission, "evx1_opaque", frozenset())

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
                provider.create_child(parent, child, "writer", "writer-612", mission, "evx1_opaque", frozenset())

            checkout = delivery / f"child-{child}"
            self.assertTrue(checkout.is_dir())
            self.assertEqual(
                subprocess.run(["git", "branch", "--show-current"], cwd=checkout, check=True, capture_output=True, text=True).stdout.strip(),
                "fix/612",
            )
            self.assertEqual(
                provider._request.call_args_list[0].args,
                ("GET", f"/api/conversations/{child}"),
            )

    def test_post_bootstrap_admission_restores_runtime_clobbered_worktree_ref(self) -> None:
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
            checkout = {"repository": "EvexU2/evex-u-core", "branch": "fix/atomic", "headSha": head}
            provider._ensure_checkout(child, checkout)

            checkout_path = delivery / f"child-{child}"
            subprocess.run(["git", "update-ref", "-d", "refs/heads/fix/atomic"], cwd=checkout_path, check=True)
            self.assertNotEqual(
                subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=checkout_path, capture_output=True).returncode,
                0,
            )

            provider._restore_checkout_after_bootstrap(child, checkout)

            self.assertEqual(
                subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout_path, check=True, capture_output=True, text=True).stdout.strip(),
                head,
            )
            self.assertEqual(
                subprocess.run(["git", "status", "--porcelain"], cwd=checkout_path, check=True, capture_output=True, text=True).stdout,
                "",
            )

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
                },
            })
            mission = {"checkout": {"repository": "EvexU2/evex-u-core", "branch": "fix/612", "headSha": initial}}

            result = provider.create_child(
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

    def test_terminal_response_reads_latest_assistant_message(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._request = Mock(return_value={
            "items": [
                {
                    "kind": "ActionEvent",
                    "source": "agent",
                    "action": {
                        "kind": "FinishAction",
                        "message": "German PM questions",
                    },
                },
                {
                    "kind": "MessageEvent",
                    "source": "agent",
                    "llm_message": {
                        "content": [{"type": "text", "text": "older assistant text"}]
                    },
                },
                {
                    "kind": "MessageEvent",
                    "source": "user",
                    "llm_message": {"content": [{"type": "text", "text": "MISSION"}]},
                },
            ]
        })

        self.assertEqual(provider.terminal_response(child), "German PM questions")

    def test_terminal_response_fails_closed_without_assistant_text(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
        provider._request = Mock(return_value={"items": []})

        with self.assertRaisesRegex(ProviderError, "terminal response"):
            provider.terminal_response(child)

    def test_cancel_interrupts_then_delivers_bound_cancel_to_paused_child(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public", sleeper=lambda _seconds: None)
        provider._request = Mock(side_effect=[
            {"execution_status": "running"},
            {},
            {"execution_status": "paused"},
            {},
            {"execution_status": "finished"},
        ])

        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        result = provider.cancel_mission(child, "cancel:one", "spec-614", main)

        self.assertEqual(result, {
            "accepted": True,
            "messageKey": "cancel:one",
            "taskKey": "spec-614",
            "terminal": True,
        })
        self.assertEqual(provider._request.call_args_list[0].args, ("GET", f"/api/conversations/{child}"))
        self.assertEqual(provider._request.call_args_list[1].args, ("POST", f"/api/conversations/{child}/interrupt", {}))
        event = provider._request.call_args_list[3].args
        self.assertEqual(event[0:2], ("POST", f"/api/conversations/{child}/events"))
        self.assertTrue(event[2]["run"])
        self.assertEqual(
            event[2]["content"][0]["text"],
            'CANCEL_MISSION\n{"childId":"22222222-2222-4222-8222-222222222222","messageKey":"cancel:one","owningMainId":"11111111-1111-4111-8111-111111111111","targetId":"22222222-2222-4222-8222-222222222222","taskKey":"spec-614"}',
        )

    def test_repeated_cancel_is_noop_after_terminal(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public", sleeper=lambda _seconds: None)
        provider._request = Mock(return_value={"execution_status": "finished"})

        main = uuid.UUID("11111111-1111-4111-8111-111111111111")
        result = provider.cancel_mission(child, "cancel:one", "spec-614", main)

        self.assertTrue(result["terminal"])
        provider._request.assert_called_once_with("GET", f"/api/conversations/{child}")


if __name__ == "__main__":
    unittest.main()
