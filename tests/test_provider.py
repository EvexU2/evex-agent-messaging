from __future__ import annotations

from unittest.mock import ANY, Mock, patch
from pathlib import Path
import subprocess
import tempfile
import uuid
import unittest

from evex_agent_messaging.provider import OpenHandsProvider, ProviderError


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
        provider._request = Mock(return_value={})

        provider.resume_mission(
            child,
            "resume:plan-reviewed",
            "spec-author-604",
            {"reviewOutcome": "PASS", "planCommit": "a" * 40},
        )

        body = provider._request.call_args.args[2]
        text = body["content"][0]["text"]
        self.assertEqual(
            text,
            'RESUME_MISSION\n{"context":{"planCommit":"'
            + "a" * 40
            + '","reviewOutcome":"PASS"},"messageKey":"resume:plan-reviewed","taskKey":"spec-author-604"}',
        )

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
                {"checkout": {"repository": "EvexU2/evex-u-core", "branch": "fix/612", "headSha": "a" * 40}},
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
                {"title": "EVEX | Reviewer | review-612"},
            ),
        )
        mission_event = provider._request.call_args_list[4].args[2]["content"][0]["text"]
        self.assertTrue(mission_event.startswith("MISSION\n{"))
        self.assertFalse(hasattr(provider.wait_until_terminal, "assert_called"))

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
                {},
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
