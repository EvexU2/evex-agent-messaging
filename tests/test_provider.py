from __future__ import annotations

import json
from pathlib import Path
import sys
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
        return OpenHandsProvider("http://openhands", "key", transport=transport), transport

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
