from __future__ import annotations

from pathlib import Path
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evex_agent_messaging.capability import (  # noqa: E402
    CapabilityError,
    capability_token,
    inspect_capability,
    main_capability_token,
    deterministic_spec_chat_id,
)
from evex_agent_messaging.service import MessagingService  # noqa: E402


class FakeProvider:
    def __init__(self, allowed=True, ready=True):
        self.allowed, self.ready, self.calls = allowed, ready, []

    def target_allowed(self, *args):
        self.calls.append(("allowed", args))
        return self.allowed

    def send_message(self, *args):
        self.calls.append(("send", args))
        return {"accepted": True, "messageKey": args[2]}

    def create_spec_chat(self, *args):
        self.calls.append(("create-spec", args))
        return {"created": True, "conversationUrl": "http://openhands/spec"}

    def readiness(self):
        return self.ready


class MessagingServiceTest(unittest.TestCase):
    def setUp(self):
        self.secret = b"secret"
        self.parent = uuid.uuid4()
        self.child = uuid.uuid4()

    def main_token(self):
        return main_capability_token(self.secret, self.parent)

    def child_token(self, role="deputy"):
        return capability_token(
            self.secret,
            owning_main_id=self.parent,
            sender_id=self.child,
            task_key="issue-42",
            role=role,
        )

    @staticmethod
    def message(summary="Delivery passed"):
        return {
            "humanSummary": summary,
            "aiEvidence": {
                "outcome": "passed",
                "evidence": ["tests: PASS"],
                "findings": [],
                "nextBoundary": "review",
            },
        }

    def test_capability_is_signed_sender_bound_and_send_only(self):
        capability = inspect_capability(self.child_token(), self.secret)
        self.assertEqual(capability.owning_main_id, self.parent)
        self.assertEqual(capability.sender_id, self.child)
        self.assertEqual(capability.role, "deputy")
        with self.assertRaises(CapabilityError):
            inspect_capability(self.child_token()[:-1] + "x", self.secret)

    def test_signed_capability_remains_valid_for_the_discussion_lifetime(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret)

        result = service.send_message(self.child_token(), self.parent, "late-result", self.message())

        self.assertEqual(result, {"accepted": True, "messageKey": "late-result"})

    def test_send_message_checks_relationship_before_provider_post(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret)
        result = service.send_message(self.child_token(), self.parent, "result-1", self.message())
        self.assertEqual(result, {"accepted": True, "messageKey": "result-1"})
        self.assertEqual([call[0] for call in provider.calls], ["allowed", "send"])

    def test_parent_wake_uses_the_existing_spec_capability(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret)
        spec_id = deterministic_spec_chat_id(self.parent)

        service.send_message(self.main_token(), spec_id, "review", self.message())

        self.assertEqual(len(provider.calls[-1][1]), 4)

    def test_non_spec_wake_uses_the_existing_target_capability(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret)

        service.send_message(self.main_token(), self.child, "status", self.message())

        self.assertEqual(len(provider.calls[-1][1]), 4)

    def test_child_wake_uses_the_existing_parent_capability(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret)

        service.send_message(self.child_token(), self.parent, "result", self.message())

        self.assertEqual(len(provider.calls[-1][1]), 4)

    def test_only_parent_main_can_create_one_deterministic_spec_chat(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret)

        result = service.create_spec_chat(self.main_token())

        expected = deterministic_spec_chat_id(self.parent)
        self.assertEqual(result["specChatId"], str(expected))
        self.assertNotIn("capabilityRef", result)
        self.assertEqual(provider.calls[0][0], "create-spec")
        self.assertEqual(provider.calls[0][1][:2], (self.parent, expected))
        self.assertEqual(len(provider.calls[0][1]), 3)
        with self.assertRaisesRegex(CapabilityError, "Parent Main"):
            service.create_spec_chat(self.child_token())

    def test_wrong_target_and_self_target_fail_closed(self):
        denied = FakeProvider(allowed=False)
        service = MessagingService(denied, self.secret)
        with self.assertRaisesRegex(CapabilityError, "not allowed"):
            service.send_message(self.child_token(), uuid.uuid4(), "key", self.message())
        with self.assertRaisesRegex(CapabilityError, "not allowed"):
            service.send_message(self.main_token(), self.parent, "key", self.message())
        self.assertFalse(any(call[0] == "send" for call in denied.calls))

    def test_message_key_and_structured_message_are_bounded_before_provider_calls(self):
        service = MessagingService(FakeProvider(), self.secret)
        invalid = (
            ("", self.message()),
            ("x" * 201, self.message()),
            ("not allowed", self.message()),
            ("ghp_abcdefgh", self.message()),
            ("key", "legacy raw text"),
            ("key", {"humanSummary": " ", "aiEvidence": self.message()["aiEvidence"]}),
            ("key", {"humanSummary": "x" * 2001, "aiEvidence": self.message()["aiEvidence"]}),
            ("key", {"humanSummary": "summary", "aiEvidence": {"outcome": "ok"}}),
            ("key", self.message("Bearer credential-value")),
            ("key", self.message("unsafe <!-- marker")),
            ("key", {"humanSummary": "summary", "aiEvidence": {"outcome": "passed", "evidence": ["x" * 2000] * 11, "findings": [], "nextBoundary": "review"}}),
        )
        for key, message in invalid:
            with self.subTest(key=key, message=message), self.assertRaises(CapabilityError) as error:
                service.send_message(self.child_token(), self.parent, key, message)
            self.assertLessEqual(len(str(error.exception).encode()), 200)
            if key:
                self.assertNotIn(key, str(error.exception))
        self.assertEqual(service._provider.calls, [])

    def test_canonical_message_preserves_exact_admissible_evidence(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret)
        message = self.message()
        message["aiEvidence"]["revision"] = "abc123"

        service.send_message(self.child_token(), self.parent, "result", message)

        self.assertEqual(provider.calls[-1][1][3], message)

    def test_readiness_is_provider_only_and_fail_closed(self):
        self.assertTrue(MessagingService(FakeProvider(), self.secret).readiness())
        self.assertFalse(MessagingService(FakeProvider(ready=False), self.secret).readiness())


if __name__ == "__main__":
    unittest.main()
