from __future__ import annotations

import json
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
        "tags": {"project": "evex-u", "evexdeliveryrole": role, **tags},
    }


class OpenHandsProviderTest(unittest.TestCase):
    def setUp(self):
        self.parent, self.child, self.spec = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def provider(self, responses):
        transport = FakeTransport(responses)
        return OpenHandsProvider(
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
            evexsourcebasehead="a" * 40,
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
            evexbasehead="a" * 40,
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
                return_value=parent_checkout,
            ),
            patch.object(provider, "_ensure_checkout") as ensure,
        ):
            result = provider.create_spec_chat(
                self.parent,
                self.spec,
                checkout,
                "evx1_spec",
            )

        self.assertTrue(result["created"])
        self.assertEqual(result["conversationUrl"], f"http://openhands.local/canvas/conversations/{self.spec}")
        ensure.assert_called_once_with(self.spec, checkout, parent_checkout)
        create = next(call for call in transport.calls if call[:2] == ("POST", "/api/conversations"))
        self.assertEqual(create[2]["tags"]["evexdeliveryrole"], "spec")
        self.assertEqual(create[2]["secrets"]["EVEX_AGENT_ROLE"]["value"], "spec")
        self.assertEqual(create[2]["current_model_id"] if "current_model_id" in create[2] else "gpt-5.6-sol", "gpt-5.6-sol")
        self.assertFalse(any(path == "/api/conversations/search" for _, path, _ in transport.calls))

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
            provider = OpenHandsProvider(
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
                "evexsourcebasehead": head,
            }
            self.assertEqual(
                provider._validated_parent_checkout(
                    parent, parent_tags, "40", checkout
                ),
                source.resolve(),
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

    def test_parent_checkout_identity_mismatch_fails_before_spec_mutation(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
            evexsourcebasehead="b" * 40,
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        provider, transport = self.provider([parent])
        checkout = {
            "repository": "EvexU2/evex-u-workspace",
            "branch": "spec/issue-40",
            "headSha": "a" * 40,
        }

        with self.assertRaisesRegex(ProviderError, "Parent Main checkout authority"):
            provider.create_spec_chat(self.parent, self.spec, checkout, "evx1_spec")

        self.assertEqual(len(transport.calls), 1)

    def test_spec_checkout_provisioning_has_no_shared_mirror_or_worktree_path(self):
        source = (ROOT / "src/evex_agent_messaging/provider.py").read_text()

        self.assertNotIn(' / "mirrors" / ', source)
        self.assertNotIn('"worktree"', source)

    def test_child_and_spec_can_target_only_their_bound_parent(self):
        for role in ("deputy", "spec"):
            provider, transport = self.provider([discussion(self.parent, "parent-main")])
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

    def test_send_message_posts_one_bounded_event_without_polling(self):
        provider, transport = self.provider([{}])
        result = provider.send_message(self.parent, self.child, "key-1", "review passed")
        self.assertEqual(result, {"accepted": True, "messageKey": "key-1"})
        self.assertEqual(len(transport.calls), 1)
        method, path, body = transport.calls[0]
        self.assertEqual((method, path), ("POST", f"/api/conversations/{self.child}/events"))
        envelope = json.loads(body["content"][0]["text"])
        self.assertEqual(envelope, {"messageKey": "key-1", "senderId": str(self.parent), "text": "review passed"})

    def test_invalid_identity_and_readiness_fail_closed(self):
        provider, _ = self.provider([{"id": "bad", "tags": {}}])
        with self.assertRaises(ProviderError):
            provider.target_allowed(self.parent, self.child, "main", self.parent)
        provider, _ = self.provider([{"active_agent_profile_id": "acp"}])
        self.assertTrue(provider.readiness())


if __name__ == "__main__":
    unittest.main()
