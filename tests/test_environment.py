from __future__ import annotations

import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from test_provider import discussion, FakeTransport, configured_provider
from evex_agent_messaging.mcp_server import main
from evex_agent_messaging.provider import OpenHandsProvider, ProviderError
from evex_agent_messaging.capability import capability_token
from evex_agent_messaging.service import MessagingService


class EnvironmentConfigurationTest(unittest.TestCase):
    def test_exact_production_and_dev_pair_including_maximum_suffix(self):
        for env, label in (
            ("production", "agent:ready"),
            ("dev:lars", "agent:dev:ready:lars"),
            ("dev:" + "a" * 34, "agent:dev:ready:" + "a" * 34),
        ):
            with self.subTest(environment=env):
                provider = OpenHandsProvider("http://openhands", "key", env, label)
                self.assertEqual((provider.environment_id, provider.intake_label), (env, label))

    def test_missing_malformed_and_mismatched_values_fail_before_transport(self):
        for env, label in (
            ("", ""), (None, None), ("dev:lars", ""), ("", "agent:ready"),
            ("dev:lars", "agent:ready"), ("production", "agent:dev:ready:lars"),
            ("dev:lars", "agent:dev:ready:else"), ("dev:", "agent:dev:ready:"),
            ("dev:Lars", "agent:dev:ready:Lars"), ("dev:l_ars", "agent:dev:ready:l_ars"),
            ("dev:lars ", "agent:dev:ready:lars"), ("production", "agent:ready "),
            ("dev:" + "a" * 35, "agent:dev:ready:" + "a" * 35),
        ):
            with self.subTest(environment=env, label=label):
                transport = FakeTransport([])
                with self.assertRaises(ValueError):
                    OpenHandsProvider("http://openhands", "key", env, label, transport=transport)
                self.assertEqual(transport.calls, [])

    def test_startup_requires_pair_before_serving(self):
        env = {
            "EVEX_MESSAGING_SECRET": "secret", "OPENHANDS_URL": "http://openhands",
            "OPENHANDS_API_KEY": "key", "OPENHANDS_PUBLIC_URL": "http://openhands.local",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "evex_agent_messaging.mcp_server.serve"
        ) as serve:
            with self.assertRaises(SystemExit):
                main()
            serve.assert_not_called()


class DiscussionEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.parent, self.spec, self.child = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.parent_value = discussion(
            self.parent, "parent-main", evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace", evexsourcebranch="main",
        )
        self.existing = discussion(
            self.spec, "spec", evexrole="role-child", evextask="issue-40-spec",
            evexissue="EvexU2/evex-u-workspace#40", evexparent=str(self.parent),
            evexrepository="EvexU2/evex-u-workspace", evexbranch="spec/issue-40",
            evexmodel="gpt-5.6-sol", evexreasoning="high",
        )
        self.existing.update({
            "workspace": {"working_dir": f"/tmp/spec-{self.spec}"},
            "current_model_id": "gpt-5.6-sol",
        })

    @staticmethod
    def bad_contexts():
        return (
            {}, {"evexenvironment": "dev:lars"},
            {"evexintakelabel": "agent:dev:ready:lars"},
            {"evexenvironment": "production", "evexintakelabel": "agent:ready"},
            {"evexenvironment": "dev:lars", "evexintakelabel": "agent:ready"},
            {"evexenvironment": ["dev:lars", "production"], "evexintakelabel": "agent:dev:ready:lars"},
        )

    @staticmethod
    def with_context(value, context):
        clone = json.loads(json.dumps(value))
        clone["tags"].pop("evexenvironment", None)
        clone["tags"].pop("evexintakelabel", None)
        clone["tags"].update(context)
        return clone

    def test_parent_context_rejection_has_no_checkout_or_provider_mutations(self):
        for context in self.bad_contexts():
            with self.subTest(context=context):
                transport = FakeTransport([self.with_context(self.parent_value, context)])
                provider = configured_provider("http://openhands", "key", transport=transport)
                with patch.object(provider, "_validated_parent_checkout") as checkout:
                    with self.assertRaises(ProviderError):
                        provider.create_spec_chat(self.parent, self.spec, "evx2_capability")
                    checkout.assert_not_called()
                self.assertEqual([method for method, _, _ in transport.calls], ["GET"])

    def test_existing_spec_context_rejection_precedes_model_secrets_checkout_and_events(self):
        for context in self.bad_contexts():
            with self.subTest(context=context):
                transport = FakeTransport([self.parent_value, self.with_context(self.existing, context)])
                provider = configured_provider("http://openhands", "key", transport=transport, workspace_root="/tmp")
                with patch.object(provider, "_validated_parent_checkout", return_value=(Path("/tmp/source"), "a" * 40)), patch.object(provider, "_ensure_checkout") as checkout:
                    with self.assertRaises(ProviderError):
                        provider.create_spec_chat(self.parent, self.spec, "evx2_capability")
                    checkout.assert_not_called()
                self.assertEqual([method for method, _, _ in transport.calls], ["GET", "GET"])

    def test_existing_spec_checkout_failure_precedes_model_and_secret_mutations(self):
        transport = FakeTransport([self.parent_value, self.existing])
        provider = configured_provider("http://openhands", "key", transport=transport, workspace_root="/tmp")
        with patch.object(provider, "_validated_parent_checkout", return_value=(Path("/tmp/source"), "a" * 40)), patch.object(provider, "_validate_existing_checkout", side_effect=ProviderError("wrong checkout")):
            with self.assertRaisesRegex(ProviderError, "wrong checkout"):
                provider.create_spec_chat(self.parent, self.spec, "evx2_capability")
        self.assertTrue(all(method == "GET" for method, _, _ in transport.calls))

    def test_sender_role_and_parent_relationship_are_current_authority(self):
        for role, actual_role, parent_issue in (
            ("deputy", "spec", "EvexU2/evex-u-workspace#40"),
            ("deputy", "child-main", "EvexU2/evex-u-workspace#99"),
            ("spec", "child-main", "EvexU2/evex-u-workspace#40"),
            ("spec", "spec", "EvexU2/evex-u-workspace#99"),
        ):
            with self.subTest(role=role, actual_role=actual_role, parent_issue=parent_issue):
                sender = discussion(self.child, actual_role, evexparentissue=parent_issue)
                transport = FakeTransport([self.parent_value, sender])
                provider = configured_provider("http://openhands", "key", transport=transport)
                self.assertFalse(provider.target_allowed(self.child, self.parent, role, self.parent))
                self.assertEqual([method for method, _, _ in transport.calls], ["GET", "GET"])

    def test_existing_foreign_spec_after_create_conflict_gets_no_further_mutation(self):
        foreign = self.with_context(self.existing, {
            "evexenvironment": "dev:else", "evexintakelabel": "agent:dev:ready:else",
        })
        transport = FakeTransport([
            self.parent_value, ProviderError("missing", status=404),
            {"active_agent_profile_id": "acp"}, ProviderError("conflict", status=409), foreign,
        ])
        provider = configured_provider("http://openhands", "key", transport=transport, workspace_root="/tmp")
        with patch.object(provider, "_validated_parent_checkout", return_value=(Path("/tmp/source"), "a" * 40)), patch.object(provider, "_ensure_checkout", return_value="a" * 40):
            with self.assertRaises(ProviderError):
                provider.create_spec_chat(self.parent, self.spec, "evx2_capability")
        self.assertEqual([(method, path) for method, path, _ in transport.calls if method != "GET"], [("POST", "/api/conversations")])

    def test_each_message_role_validates_sender_and_target_before_event_post(self):
        child = discussion(self.child, "child-main", evexparentissue="EvexU2/evex-u-workspace#40")
        message = {"humanSummary": "Review passed", "aiEvidence": {
            "outcome": "PASS", "evidence": [], "findings": [], "nextBoundary": "review",
        }}
        for role, sender, target in (
            ("main", self.parent_value, child),
            ("deputy", child, self.parent_value),
            ("spec", self.existing, self.parent_value),
        ):
            for bad_side in ("sender", "target"):
                for context in self.bad_contexts():
                    with self.subTest(role=role, bad_side=bad_side, context=context):
                        sender_value = self.with_context(sender, context) if bad_side == "sender" else sender
                        target_value = self.with_context(target, context) if bad_side == "target" else target
                        transport = FakeTransport([target_value, sender_value])
                        provider = configured_provider("http://openhands", "key", transport=transport)
                        token = capability_token(b"secret", owning_main_id=self.parent, sender_id=uuid.UUID(sender["id"]), task_key="issue-40", role=role)
                        service = MessagingService(provider, b"secret")
                        with self.assertRaises(ProviderError):
                            service.send_message(token, uuid.UUID(target["id"]), "result", message)
                        self.assertTrue(all(method == "GET" for method, _, _ in transport.calls))

    def test_duplicate_raw_environment_tags_fail_before_mutation(self):
        for key, value in (("evexenvironment", "dev:lars"), ("evexintakelabel", "agent:dev:ready:lars")):
            raw = json.dumps(self.parent_value).replace(f'"{key}": "{value}"', f'"{key}": "{value}", "{key}": "{value}"').encode()
            provider = configured_provider("http://openhands", "key")
            with patch("urllib.request.urlopen", return_value=io.BytesIO(raw)) as transport:
                with self.assertRaisesRegex(ProviderError, "duplicate"):
                    provider.create_spec_chat(self.parent, self.spec, "evx2_capability")
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(transport.call_args.args[0].method, "GET")
