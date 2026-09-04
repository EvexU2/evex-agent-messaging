from __future__ import annotations

import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from test_provider import discussion, FakeTransport, configured_provider, spec_discussion
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


class StandaloneConfigurationTest(unittest.TestCase):
    def config(self):
        return {
            "EVEX_ENVIRONMENT_ID": "dev:lars", "EVEX_INTAKE_LABEL": "agent:dev:ready:lars",
            "EVEX_MESSAGING_SECRET": "secret", "OPENHANDS_URL": "http://openhands:8000",
            "OPENHANDS_API_KEY": "key", "OPENHANDS_PUBLIC_URL": "https://example.test/canvas",
            "EVEX_DELIVERY_ADMISSION_KEY": "a" * 32,
            "EVEX_GATEWAY_DELIVERY_SECRET": "g" * 32,
        }

    def test_invalid_configuration_never_serves_or_exposes_value(self):
        cases = [
            ("EVEX_MESSAGING_TRANSPORT", value) for value in ("", "HTTP", "htpt", " http")
        ] + [
            ("EVEX_MESSAGING_PORT", value) for value in ("", "0", "65536", "-1", " 3101", "3.1", "private-value", "３１０１")
        ] + [
            ("EVEX_MESSAGING_HOST", value) for value in ("", " ", "localhost/path", "localhost?secret", "localhost#secret", "http://localhost")
        ] + [
            (name, value) for name in ("OPENHANDS_URL", "OPENHANDS_PUBLIC_URL")
            for value in (" https://example.test/canvas", "https://example.test:private-value/canvas", "https://example.test:/canvas", "https://example.test:65536/canvas", "https://example.test:0/canvas", "https://user:private-value@example.test/canvas", "https://example.test/canvas?private-value", "https://exam\nple.test/canvas", "https://[broken/canvas", "https://%6cocalhost/canvas", "https://localhost\\evil.org/canvas")
        ] + [("OPENHANDS_URL", "https://example.test/api"), ("OPENHANDS_PUBLIC_URL", "https://example.test")]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                config = self.config()
                config[name] = value
                with patch.dict(os.environ, config, clear=True), patch("evex_agent_messaging.mcp_server.serve") as stdio, patch("evex_agent_messaging.mcp_server.serve_http") as http:
                    with self.assertRaises(SystemExit) as error:
                        main()
                    self.assertNotIn("private-value", str(error.exception))
                    stdio.assert_not_called()
                    http.assert_not_called()

    def test_uppercase_url_schemes_fail_before_provider_construction(self):
        for name, path in (("OPENHANDS_URL", ""), ("OPENHANDS_PUBLIC_URL", "/canvas")):
            for scheme in ("HTTP", "HTTPS"):
                with self.subTest(name=name, scheme=scheme):
                    config = self.config()
                    config[name] = f"{scheme}://agents.example.org{path}"
                    with patch.dict(os.environ, config, clear=True), patch("evex_agent_messaging.mcp_server.OpenHandsProvider") as provider, patch("evex_agent_messaging.mcp_server.serve") as stdio, patch("evex_agent_messaging.mcp_server.serve_http") as http:
                        with self.assertRaises(SystemExit):
                            main()
                        provider.assert_not_called()
                        stdio.assert_not_called()
                        http.assert_not_called()

    def test_empty_query_and_fragment_markers_fail_before_serving(self):
        for name, origin in (("OPENHANDS_URL", "http://service"), ("OPENHANDS_PUBLIC_URL", "https://agents.example.org/canvas")):
            for suffix in ("?", "#", "?query", "#fragment"):
                with self.subTest(name=name, suffix=suffix):
                    config = self.config()
                    config[name] = origin + suffix
                    with patch.dict(os.environ, config, clear=True), patch("evex_agent_messaging.mcp_server.OpenHandsProvider") as provider, patch("evex_agent_messaging.mcp_server.serve") as stdio, patch("evex_agent_messaging.mcp_server.serve_http") as http:
                        with self.assertRaises(SystemExit):
                            main()
                        provider.assert_not_called()
                        stdio.assert_not_called()
                        http.assert_not_called()

    def test_production_rejects_local_origins_and_insecure_public_url(self):
        cases = [("OPENHANDS_PUBLIC_URL", "http://agents.example.org/canvas")]
        for host in ("localhost", "LOCALHOST.", "openhands.localhost", "127.0.0.1", "127.0.9.3", "[::1]", "0.0.0.0", "[::]", "[::ffff:127.0.0.1]", "[::ffff:0.0.0.0]", "127.1", "2130706433", "0", "0x7f000001", "0177.0.0.1", "ｌｏｃａｌｈｏｓｔ", "１２７.１"):
            cases.extend((
                ("OPENHANDS_URL", f"http://{host}:8000"),
                ("OPENHANDS_PUBLIC_URL", f"https://{host}/canvas"),
            ))
        for host in ("10.0.0.1", "169.254.169.254", "[fc00::1]"):
            cases.append(("OPENHANDS_PUBLIC_URL", f"https://{host}/canvas"))
        for name, value in cases:
            with self.subTest(name=name, value=value):
                config = self.config()
                config.update({"EVEX_ENVIRONMENT_ID": "production", "EVEX_INTAKE_LABEL": "agent:ready", name: value})
                with patch.dict(os.environ, config, clear=True), patch("evex_agent_messaging.mcp_server.serve") as stdio, patch("evex_agent_messaging.mcp_server.serve_http") as http:
                    with self.assertRaises(SystemExit):
                        main()
                    stdio.assert_not_called()
                    http.assert_not_called()

    def test_production_accepts_https_public_and_http_service_origin(self):
        config = self.config()
        config.update({
            "EVEX_ENVIRONMENT_ID": "production", "EVEX_INTAKE_LABEL": "agent:ready",
            "OPENHANDS_URL": "http://openhands.evex-agents.svc.cluster.local:8000",
            "OPENHANDS_PUBLIC_URL": "https://agents.example.org/canvas",
        })
        for public_url in ("https://agents.example.org/canvas", "https://agents.example.org/canvas/"):
            config["OPENHANDS_PUBLIC_URL"] = public_url
            with self.subTest(public_url=public_url), patch.dict(os.environ, config, clear=True), patch("evex_agent_messaging.mcp_server.serve") as stdio:
                self.assertEqual(main(), 0)
                stdio.assert_called_once()

    def test_development_accepts_http_local_urls(self):
        config = self.config()
        config.update({"OPENHANDS_URL": "http://localhost:8000", "OPENHANDS_PUBLIC_URL": "http://openhands.evex.localhost/canvas"})
        with patch.dict(os.environ, config, clear=True), patch("evex_agent_messaging.mcp_server.serve") as stdio:
            self.assertEqual(main(), 0)
            stdio.assert_called_once()

    def test_stdio_default_and_explicit_http_pass_validated_configuration(self):
        for transport in (None, "stdio", "http"):
            with self.subTest(transport=transport):
                config = self.config()
                if transport:
                    config["EVEX_MESSAGING_TRANSPORT"] = transport
                config.update({"EVEX_MESSAGING_HOST": "127.0.0.1", "EVEX_MESSAGING_PORT": "65535"})
                with patch.dict(os.environ, config, clear=True), patch("evex_agent_messaging.mcp_server.serve") as stdio, patch("evex_agent_messaging.mcp_server.serve_http") as http:
                    self.assertEqual(main(), 0)
                    if transport == "http":
                        self.assertEqual(http.call_args.args[1:], ("127.0.0.1", 65535))
                        stdio.assert_not_called()
                    else:
                        stdio.assert_called_once()
                        http.assert_not_called()


class DiscussionEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.parent, self.spec, self.child = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.parent_value = discussion(
            self.parent, "issue", evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace", evexsourcebranch="main",
        )
        self.existing = spec_discussion(
            self.spec, self.parent, capability_ref="evx2_capability",
        )

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
                invalid = self.with_context(self.existing, context)
                transport = FakeTransport([self.parent_value, invalid, invalid])
                provider = configured_provider("http://openhands", "key", transport=transport, workspace_root="/tmp")
                with patch.object(provider, "_validated_parent_checkout", return_value=(Path("/tmp/source"), "a" * 40)), patch.object(provider, "_ensure_checkout") as checkout:
                    with self.assertRaises(ProviderError):
                        provider.create_spec_chat(self.parent, self.spec, "evx2_capability")
                    checkout.assert_not_called()
                self.assertEqual(
                    [method for method, _, _ in transport.calls], ["GET", "GET", "GET"]
                )

    def test_existing_spec_checkout_failure_precedes_model_and_secret_mutations(self):
        transport = FakeTransport([self.parent_value, self.existing, self.existing])
        provider = configured_provider(
            "http://openhands", "key", transport=transport, workspace_root="/tmp",
            admission_key=b"admission-key" * 4,
        )
        with patch.object(provider, "_validated_parent_checkout", return_value=(Path("/tmp/source"), "a" * 40)), patch.object(provider, "_validate_existing_checkout", side_effect=ProviderError("wrong checkout")):
            with self.assertRaisesRegex(ProviderError, "wrong checkout"):
                provider.create_spec_chat(self.parent, self.spec, "evx2_capability")
        self.assertTrue(all(method == "GET" for method, _, _ in transport.calls))

    def test_existing_foreign_spec_after_create_conflict_gets_no_further_mutation(self):
        foreign = self.with_context(self.existing, {
            "evexenvironment": "dev:else", "evexintakelabel": "agent:dev:ready:else",
        })
        transport = FakeTransport([
            self.parent_value, ProviderError("missing", status=404),
            {"active_agent_profile_id": "44444444-4444-4444-8444-444444444444", "profiles": [
                {"id": "44444444-4444-4444-8444-444444444444", "agent_kind": "acp"},
            ]}, ProviderError("conflict", status=409), foreign, foreign,
        ])
        provider = configured_provider(
            "http://openhands", "key", transport=transport, workspace_root="/tmp",
            admission_key=b"admission-key" * 4,
        )
        with patch.object(provider, "_validated_parent_checkout", return_value=(Path("/tmp/source"), "a" * 40)), patch.object(provider, "_ensure_checkout", return_value="a" * 40):
            with self.assertRaises(ProviderError):
                provider.create_spec_chat(self.parent, self.spec, "evx2_capability")
        self.assertEqual([(method, path) for method, path, _ in transport.calls if method != "GET"], [("POST", "/api/conversations")])

    def test_each_message_path_validates_current_environment_before_event_post(self):
        child = discussion(self.child, "subissue", evexparentissue="EvexU2/evex-u-workspace#40")
        message = {"humanSummary": "Review passed", "aiEvidence": {
            "outcome": "PASS", "evidence": [], "findings": [], "nextBoundary": "review",
        }}
        cases = (
            ("issue", self.parent_value, child, ("sender", "target")),
            ("subissue", child, self.parent_value, ("target",)),
            ("spec", self.existing, self.parent_value, ("target",)),
        )
        for role, sender, target, bad_sides in cases:
            for bad_side in bad_sides:
                for context in self.bad_contexts():
                    with self.subTest(role=role, bad_side=bad_side, context=context):
                        sender_value = self.with_context(sender, context) if bad_side == "sender" else sender
                        target_value = self.with_context(target, context) if bad_side == "target" else target
                        responses = [target_value, sender_value] if role == "issue" else [target_value]
                        transport = FakeTransport(responses)
                        provider = configured_provider("http://openhands", "key", transport=transport)
                        token = capability_token(
                            b"secret", owning_issue_id=self.parent,
                            sender_id=uuid.UUID(sender["id"]), task_key="issue-40", role=role,
                        )
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
