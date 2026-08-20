from __future__ import annotations

from unittest.mock import Mock
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
        provider = OpenHandsProvider(
            "http://openhands", "key", "http://public", completion_hook_url="http://messaging/completion-hook"
        )
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
        ])

        provider.create_child(
            parent,
            child,
            "reviewer",
            "review-612",
            "Review",
            "evx1_opaque",
            frozenset(),
        )

        create = provider._request.call_args_list[3].args[2]
        hook = create["hook_config"]["stop"][0]["hooks"][0]
        self.assertTrue(hook["async"])
        self.assertIn("http://messaging/completion-hook", hook["command"])
        self.assertIn("evx1_opaque", hook["command"])
        self.assertIn("--retry 2", hook["command"])
        self.assertEqual(
            create["mcp_config"],
            {"mcpServers": {"evex_agent_messaging": {"url": "http://messaging/mcp"}}},
        )

    def test_integrated_mission_receives_runtime_mcp_explicitly(self) -> None:
        parent = uuid.UUID("11111111-1111-4111-8111-111111111111")
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public")
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
        ])

        provider.create_child(
            parent,
            child,
            "qa",
            "qa-integrated",
            "QA",
            "evx1_opaque",
            frozenset({"runtime_environment"}),
        )

        create = provider._request.call_args_list[3].args[2]
        self.assertEqual(create["mcp_config"], config)

    def test_wait_until_terminal_is_bounded(self) -> None:
        child = uuid.UUID("22222222-2222-4222-8222-222222222222")
        provider = OpenHandsProvider("http://openhands", "key", "http://public", sleeper=lambda _seconds: None)
        provider._request = Mock(side_effect=[
            {"execution_status": "running"},
            {"execution_status": "finished"},
        ])

        self.assertEqual(provider.wait_until_terminal(child), "finished")

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
