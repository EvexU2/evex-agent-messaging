from __future__ import annotations

import json
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evex_agent_messaging.provider import OpenHandsProvider, ProviderError  # noqa: E402
from evex_agent_messaging.capability import capability_token  # noqa: E402
from evex_agent_messaging.service import MessagingService  # noqa: E402
from evex_agent_messaging.mcp_server import McpServer  # noqa: E402


def configured_provider(*args, **kwargs):
    return OpenHandsProvider(
        *args, environment_id="dev:lars", intake_label="agent:dev:ready:lars", **kwargs,
    )


class FakeTransport:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def discussion(conversation_id, role, **tags):
    return {
        "id": str(conversation_id),
        "tags": {
            "project": "evex-u", "evexdeliveryrole": role,
            "evexenvironment": "dev:lars", "evexintakelabel": "agent:dev:ready:lars",
            **tags,
        },
    }


class OpenHandsProviderTest(unittest.TestCase):
    def setUp(self):
        self.parent, self.child, self.spec = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def provider(self, responses):
        transport = FakeTransport(responses)
        return configured_provider(
            "http://openhands",
            "key",
            transport=transport,
            public_url="http://openhands.local/canvas",
        ), transport

    def test_create_spec_chat_reuses_exact_parent_issue_and_fixed_role(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        created = discussion(
            self.spec,
            "spec",
            evexrole="role-child",
            evextask="issue-40-spec",
            evexissue="EvexU2/evex-u-workspace#40",
            evexparent=str(self.parent),
            evexrepository="EvexU2/evex-u-workspace",
            evexbranch="spec/issue-40",
            evexmodel="gpt-5.6-sol",
            evexreasoning="high",
        )
        created["workspace"] = {"working_dir": f"/tmp/spec-{self.spec}"}
        created["current_model_id"] = "gpt-5.6-sol"
        provider, transport = self.provider([
            parent,
            ProviderError("missing", status=404),
            {"active_agent_profile_id": "acp"},
            {},
            created,
            {},
            {},
            created,
            {},
        ])
        provider.workspace_root = "/tmp"
        checkout = {
            "repository": "EvexU2/evex-u-workspace",
            "branch": "spec/issue-40",
            "headSha": "a" * 40,
        }
        parent_checkout = Path("/tmp/issue-40-source/evex-u-workspace")
        with (
            patch.object(
                provider,
                "_validated_parent_checkout",
                return_value=(parent_checkout, "a" * 40),
            ),
            patch.object(
                provider, "_ensure_checkout", return_value="a" * 40
            ) as ensure,
            patch.object(
                provider, "_validate_existing_checkout", return_value="a" * 40
            ),
        ):
            result = provider.create_spec_chat(
                self.parent,
                self.spec,
                "evx2_spec",
            )

        self.assertTrue(result["created"])
        self.assertEqual(result["checkout"], checkout)
        self.assertEqual(result["conversationUrl"], f"http://openhands.local/canvas/conversations/{self.spec}")
        ensure.assert_called_once_with(self.spec, checkout, parent_checkout)
        create = next(call for call in transport.calls if call[:2] == ("POST", "/api/conversations"))
        self.assertEqual(create[2]["tags"]["evexdeliveryrole"], "spec")
        self.assertEqual(create[2]["tags"]["evexenvironment"], "dev:lars")
        self.assertEqual(create[2]["tags"]["evexintakelabel"], "agent:dev:ready:lars")
        self.assertEqual(create[2]["secrets"]["EVEX_ENVIRONMENT_ID"]["value"], "dev:lars")
        self.assertEqual(create[2]["secrets"]["EVEX_INTAKE_LABEL"]["value"], "agent:dev:ready:lars")
        self.assertNotIn("evexlocale", create[2]["tags"])
        self.assertNotIn("evexbasehead", create[2]["tags"])
        self.assertNotIn("language", create[2])
        self.assertEqual(
            create[2]["agent_launch_additions"]["system_message_suffix_append"],
            "EVEX role scope: interactive Spec Chat. Use the admitted checkout, "
            "EVEX Spec skills, native read-only review subagents, and send_message "
            "only to the bound Parent Main.",
        )
        self.assertEqual(create[2]["secrets"]["EVEX_AGENT_ROLE"]["value"], "spec")
        self.assertEqual(create[2]["current_model_id"] if "current_model_id" in create[2] else "gpt-5.6-sol", "gpt-5.6-sol")
        self.assertFalse(any(path == "/api/conversations/search" for _, path, _ in transport.calls))

    def test_reused_spec_chat_receives_the_current_durable_capability(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        existing = discussion(
            self.spec,
            "spec",
            evexrole="role-child",
            evextask="issue-40-spec",
            evexissue="EvexU2/evex-u-workspace#40",
            evexparent=str(self.parent),
            evexrepository="EvexU2/evex-u-workspace",
            evexbranch="spec/issue-40",
            evexmodel="gpt-5.6-sol",
            evexreasoning="high",
        )
        existing["workspace"] = {"working_dir": f"/tmp/spec-{self.spec}"}
        existing["current_model_id"] = "gpt-5.6-sol"
        existing["language"] = "fr-FR"
        provider, transport = self.provider([parent, existing, {}, existing, {}])
        provider.workspace_root = "/tmp"
        with (
            patch.object(
                provider,
                "_validated_parent_checkout",
                return_value=(
                    Path("/tmp/issue-40-source/evex-u-workspace"),
                    "a" * 40,
                ),
            ),
            patch.object(
                provider, "_validate_existing_checkout", return_value="b" * 40
            ),
            patch.object(provider, "_has_initial_prompt", return_value=True),
        ):
            result = provider.create_spec_chat(
                self.parent,
                self.spec,
                "evx2_current",
            )

        self.assertFalse(result["created"])
        self.assertEqual(existing["language"], "fr-FR")
        for method, path, body in transport.calls:
            if method in {"POST", "PATCH"} and isinstance(body, dict):
                with self.subTest(method=method, path=path):
                    self.assertNotIn("language", body)
                    self.assertNotIn("agent_launch_additions", body)
        self.assertEqual(result["checkout"], {
            "repository": "EvexU2/evex-u-workspace",
            "branch": "spec/issue-40",
            "headSha": "b" * 40,
        })
        self.assertIn((
            "POST",
            f"/api/conversations/{self.spec}/secrets",
            {"secrets": {"EVEX_AGENT_MESSAGING_CAPABILITY": {
                "kind": "StaticSecret", "value": "evx2_current",
            }}},
        ), transport.calls)

    def test_create_spec_chat_reconciles_an_ambiguous_create_response(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        created = discussion(
            self.spec,
            "spec",
            evexrole="role-child",
            evextask="issue-40-spec",
            evexissue="EvexU2/evex-u-workspace#40",
            evexparent=str(self.parent),
            evexrepository="EvexU2/evex-u-workspace",
            evexbranch="spec/issue-40",
            evexmodel="gpt-5.6-sol",
            evexreasoning="high",
        )
        created["workspace"] = {"working_dir": f"/tmp/spec-{self.spec}"}
        created["current_model_id"] = "gpt-5.6-sol"
        provider, transport = self.provider([
            parent,
            ProviderError("missing", status=404),
            {"active_agent_profile_id": "acp"},
            ProviderError("connection closed"),
            created,
            {},
            {},
            created,
            {},
        ])
        provider.workspace_root = "/tmp"
        with (
            patch.object(
                provider,
                "_validated_parent_checkout",
                return_value=(
                    Path("/tmp/issue-40-source/evex-u-workspace"),
                    "a" * 40,
                ),
            ),
            patch.object(
                provider, "_ensure_checkout", return_value="a" * 40
            ),
            patch.object(
                provider, "_validate_existing_checkout", return_value="a" * 40
            ),
        ):
            result = provider.create_spec_chat(
                self.parent,
                self.spec,
                "evx2_spec",
            )

        self.assertTrue(result["created"])
        self.assertIn(("GET", f"/api/conversations/{self.spec}", None), transport.calls)

    def test_create_spec_chat_reconciles_an_ambiguous_initial_prompt(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        existing = discussion(
            self.spec,
            "spec",
            evexrole="role-child",
            evextask="issue-40-spec",
            evexissue="EvexU2/evex-u-workspace#40",
            evexparent=str(self.parent),
            evexrepository="EvexU2/evex-u-workspace",
            evexbranch="spec/issue-40",
            evexmodel="gpt-5.6-sol",
            evexreasoning="high",
        )
        existing["workspace"] = {"working_dir": f"/tmp/spec-{self.spec}"}
        existing["current_model_id"] = "gpt-5.6-sol"
        expected_prompt = (
            "EVEX_SPEC_CHAT\n"
            "Issue: https://github.com/EvexU2/evex-u-workspace/issues/40\n"
            f"Parent Main: {self.parent}\n"
            "Your task now: run the interactive Spec Chat for this Issue using the admitted "
            "EVEX Spec skills. Start by reading the current Issue and living Specification."
        )
        prompt_event = {
            "kind": "MessageEvent",
            "source": "user",
            "llm_message": {"content": [{"type": "text", "text": expected_prompt}]},
        }
        provider, transport = self.provider([
            parent,
            existing,
            {},
            existing,
            {},
            {"items": []},
            ProviderError("connection closed"),
            {"items": [prompt_event]},
        ])
        provider.workspace_root = "/tmp"
        with (
            patch.object(
                provider,
                "_validated_parent_checkout",
                return_value=(
                    Path("/tmp/issue-40-source/evex-u-workspace"),
                    "a" * 40,
                ),
            ),
            patch.object(
                provider, "_validate_existing_checkout", return_value="a" * 40
            ),
        ):
            result = provider.create_spec_chat(
                self.parent,
                self.spec,
                "evx2_spec",
            )

        self.assertFalse(result["created"])
        self.assertIn((
            "GET",
            f"/api/conversations/{self.spec}/events/search"
            "?limit=1&source=user&sort_order=TIMESTAMP",
            None,
        ), transport.calls)

    def test_spec_chat_checkout_is_derived_from_exact_parent_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "delivery" / "issue-40-source" / "evex-u-workspace"
            source.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(source), "remote", "add", "origin", "https://github.com/EvexU2/evex-u-workspace.git"], check=True)
            (source / "spec.md").write_text("spec\n")
            subprocess.run(["git", "-C", str(source), "add", "spec.md"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "base"], check=True)
            head = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            provider = configured_provider(
                "http://openhands",
                "key",
                public_url="http://openhands.local/canvas",
                workspace_root=str(root / "delivery"),
            )
            checkout = {
                "repository": "EvexU2/evex-u-workspace",
                "branch": "spec/issue-40",
                "headSha": head,
            }

            parent = {
                "workspace": {"working_dir": str(source.resolve())},
            }
            parent_tags = {
                "evexsourcerepository": "EvexU2/evex-u-workspace",
                "evexsourcebranch": "main",
            }
            self.assertEqual(
                provider._validated_parent_checkout(
                    parent, parent_tags, "40"
                ),
                (source.resolve(), head),
            )

            provider._ensure_checkout(self.spec, checkout, source)

            path = provider._checkout_path(self.spec)
            self.assertEqual((path / "spec.md").read_text(), "spec\n")
            self.assertEqual(provider._git(path, "branch", "--show-current"), "spec/issue-40")
            self.assertEqual(provider._git(path, "rev-parse", "HEAD"), head)
            self.assertEqual(
                provider._git(path, "remote", "get-url", "origin"),
                "https://github.com/EvexU2/evex-u-workspace.git",
            )
            self.assertFalse((root / "mirrors").exists())

            (source / "uncommitted.md").write_text("dirty\n")
            with self.assertRaisesRegex(ProviderError, "must be clean"):
                provider._validated_parent_checkout(parent, parent_tags, "40")

    def test_deleted_spec_conversation_reuses_its_clean_advanced_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "delivery" / "issue-40-source" / "evex-u-workspace"
            source.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(source), "remote", "add", "origin", "https://github.com/EvexU2/evex-u-workspace.git"], check=True)
            (source / "spec.md").write_text("base\n")
            subprocess.run(["git", "-C", str(source), "add", "spec.md"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "base"], check=True)
            parent_head = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            provider = configured_provider(
                "http://openhands",
                "key",
                public_url="http://openhands.local/canvas",
                workspace_root=str(root / "delivery"),
            )
            checkout = {
                "repository": "EvexU2/evex-u-workspace",
                "branch": "spec/issue-40",
                "headSha": parent_head,
            }
            provider._ensure_checkout(self.spec, checkout, source)
            spec_path = provider._checkout_path(self.spec)
            subprocess.run(["git", "-C", str(spec_path), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(spec_path), "config", "user.name", "Test"], check=True)
            (spec_path / "spec.md").write_text("reviewed candidate\n")
            subprocess.run(["git", "-C", str(spec_path), "add", "spec.md"], check=True)
            subprocess.run(["git", "-C", str(spec_path), "commit", "-q", "-m", "spec candidate"], check=True)
            spec_head = subprocess.run(
                ["git", "-C", str(spec_path), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            self.assertNotEqual(spec_head, parent_head)
            self.assertEqual(
                provider._ensure_checkout(self.spec, checkout, source),
                spec_head,
            )

    def test_parent_checkout_identity_mismatch_fails_before_spec_mutation(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="develop",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        provider, transport = self.provider([parent])
        with self.assertRaisesRegex(ProviderError, "Parent Main checkout authority"):
            provider.create_spec_chat(self.parent, self.spec, "evx1_spec")

        self.assertEqual(len(transport.calls), 1)

    def test_spec_checkout_provisioning_has_no_shared_mirror_or_worktree_path(self):
        source = (ROOT / "src/evex_agent_messaging/provider.py").read_text()

        self.assertNotIn(' / "mirrors" / ', source)
        self.assertNotIn('"worktree"', source)

    def test_child_and_spec_can_target_only_their_bound_parent(self):
        for role in ("deputy", "spec"):
            provider, transport = self.provider([
                discussion(self.parent, "parent-main", evexissue="EvexU2/evex-u-workspace#40"),
                discussion(self.child, "child-main" if role == "deputy" else "spec",
                           evexparentissue="EvexU2/evex-u-workspace#40"),
            ])
            self.assertTrue(provider.target_allowed(self.child, self.parent, role, self.parent))
            self.assertEqual(transport.calls[0][1], f"/api/conversations/{self.parent}")
        provider, _ = self.provider([discussion(self.child, "child-main")])
        self.assertFalse(provider.target_allowed(self.child, self.child, "deputy", self.parent))

    def test_parent_can_target_only_direct_child_or_linked_spec(self):
        parent = discussion(self.parent, "parent-main", evexissue="EvexU2/evex-u-workspace#40")
        child = discussion(self.child, "child-main", evexparentissue="EvexU2/evex-u-workspace#40")
        provider, transport = self.provider([child, parent])
        self.assertTrue(provider.target_allowed(self.parent, self.child, "main", self.parent))
        self.assertEqual(len(transport.calls), 2)

        spec = discussion(self.spec, "spec", evexparent=str(self.parent))
        provider, _ = self.provider([spec, parent])
        self.assertTrue(provider.target_allowed(self.parent, self.spec, "main", self.parent))

    def test_foreign_or_unrelated_target_is_rejected_without_search(self):
        parent = discussion(self.parent, "parent-main", evexissue="EvexU2/evex-u-workspace#40")
        child = discussion(self.child, "child-main", evexparentissue="EvexU2/evex-u-workspace#99")
        provider, transport = self.provider([child, parent])
        self.assertFalse(provider.target_allowed(self.parent, self.child, "main", self.parent))
        self.assertFalse(any("search" in path for _, path, _ in transport.calls))

    def test_send_message_projects_visible_summary_and_hidden_canonical_evidence(self):
        provider, transport = self.provider([{}])
        message = {
            "humanSummary": "Review passed; no action is needed.",
            "aiEvidence": {"outcome": "passed", "evidence": ["tests: PASS"], "findings": [], "nextBoundary": "merge"},
        }
        result = provider.send_message(self.parent, self.child, "key-1", message)
        self.assertEqual(result, {"accepted": True, "messageKey": "key-1"})
        self.assertEqual(len(transport.calls), 1)
        method, path, body = transport.calls[0]
        self.assertEqual((method, path), ("POST", f"/api/conversations/{self.child}/events"))
        projection = body["content"][0]["text"]
        self.assertTrue(projection.startswith(message["humanSummary"] + "\n<!-- evex-agent-message:v1 "))
        self.assertTrue(projection.endswith(" -->"))
        envelope = json.loads(projection.removeprefix(message["humanSummary"] + "\n<!-- evex-agent-message:v1 ").removesuffix(" -->"))
        self.assertEqual(envelope, {"aiEvidence": message["aiEvidence"], "humanSummary": message["humanSummary"], "messageKey": "key-1", "senderId": str(self.parent)})

    def test_configured_credential_is_rejected_before_provider_mutation(self):
        transport = FakeTransport([])
        provider = configured_provider(
            "http://openhands", "configured-secret", transport=transport,
        )

        with self.assertRaisesRegex(ProviderError, "configured credential"):
            provider.send_message(
                self.parent,
                self.child,
                "key",
                {"humanSummary": "Delivery passed", "aiEvidence": {"outcome": "configured-secret", "evidence": [], "findings": [], "nextBoundary": "review"}},
            )

        self.assertEqual(transport.calls, [])

    def test_invalid_identity_and_readiness_fail_closed(self):
        provider, _ = self.provider([{"id": "bad", "tags": {}}])
        with self.assertRaises(ProviderError):
            provider.target_allowed(self.parent, self.child, "main", self.parent)
        provider, _ = self.provider([{"active_agent_profile_id": "acp"}])
        self.assertTrue(provider.readiness())


class ConversationResponseBudgetTest(unittest.TestCase):
    LIMIT = 1024 * 1024

    def setUp(self):
        self.parent, self.child = uuid.uuid4(), uuid.uuid4()
        self.capability = capability_token(
            b"test-secret", owning_main_id=self.parent, sender_id=self.child,
            task_key="issue-927", role="deputy",
        )
        self.provider = configured_provider("http://openhands", "test-api-key")
        self.server = McpServer(MessagingService(self.provider, b"test-secret"))
        self.request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "send_message", "arguments": {
                "targetId": str(self.parent), "messageKey": "final-review",
                "message": {"humanSummary": "Review passed", "aiEvidence": {
                    "outcome": "PASS", "evidence": [], "findings": [], "nextBoundary": "spec review",
                }},
            }},
        }

    def parent_bytes(self, size, **overrides):
        value = discussion(self.parent, "parent-main", evexissue="EvexU2/evex-u-workspace#40")
        value["stats"] = {"per_turn": "private-statistics" + "x" * 66000}
        value.update(overrides)
        raw = json.dumps(value).encode()
        self.assertLessEqual(len(raw), size)
        return raw + b" " * (size - len(raw))

    def send(self, raw):
        class Response(io.BytesIO):
            def read(inner, size=-1):
                inner.read_limit = size
                return super().read(size)

        response = Response(raw)
        sender = json.dumps(discussion(
            self.child, "child-main", evexparentissue="EvexU2/evex-u-workspace#40",
        )).encode()
        with patch("urllib.request.urlopen", side_effect=[response, io.BytesIO(sender), io.BytesIO(b"{}")]) as http:
            result = self.server.handle(self.request, capability_ref=self.capability)
        return result, http.call_args_list, response.read_limit

    def test_long_running_parent_and_exact_limit_allow_one_authorized_event(self):
        for size in (69143, self.LIMIT):
            with self.subTest(size=size):
                result, calls, read_limit = self.send(self.parent_bytes(size))
                self.assertEqual(result["result"]["structuredContent"], {
                    "accepted": True, "messageKey": "final-review",
                })
                self.assertEqual([call.args[0].method for call in calls], ["GET", "GET", "POST"])
                self.assertEqual(calls[0].args[0].full_url, f"http://openhands/api/conversations/{self.parent}")
                self.assertEqual(calls[2].args[0].full_url, f"http://openhands/api/conversations/{self.parent}/events")
                self.assertTrue(json.loads(calls[2].args[0].data)["run"])
                self.assertEqual(read_limit, self.LIMIT + 1)
                self.assertEqual([call.kwargs["timeout"] for call in calls], [5.0, 5.0, 5.0])

    def test_over_limit_fails_before_parsing_or_event_post(self):
        with patch("evex_agent_messaging.provider.json.loads", wraps=json.loads) as parse:
            result, calls, read_limit = self.send(b"x" * (self.LIMIT + 1))
        self.assertFalse(any(isinstance(call.args[0], bytes) for call in parse.call_args_list))
        self.assertEqual(result["error"]["code"], -32000)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0].method, "GET")
        self.assertEqual(read_limit, self.LIMIT + 1)
        self.assertNotIn("private-statistics", json.dumps(result))

    def test_large_invalid_id_or_target_role_cannot_authorize_a_post(self):
        for overrides in (
            {"id": str(uuid.uuid4())},
            {"id": "invalid"},
            {"tags": {"project": "foreign", "evexdeliveryrole": "parent-main"}},
            {"tags": {"project": "evex-u", "evexdeliveryrole": "spec"}},
        ):
            with self.subTest(overrides=overrides):
                result, calls, _ = self.send(self.parent_bytes(69143, **overrides))
                self.assertIn("error", result)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].args[0].method, "GET")
                self.assertNotIn("private-statistics", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
