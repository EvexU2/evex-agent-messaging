from __future__ import annotations

from pathlib import Path
import base64
import hashlib
import hmac
import struct
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evex_agent_messaging.capability import (  # noqa: E402
    CapabilityError,
    capability_token,
    inspect_capability,
    issue_capability_token,
    deterministic_spec_chat_id,
)
from evex_agent_messaging.service import MessagingService  # noqa: E402
from evex_agent_messaging import capability as capabilities  # noqa: E402


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

    def start_specialist(self, *args):
        self.calls.append(("start-specialist", args))
        return {"created": True, "conversationUrl": "http://openhands/specialist", "status": "running"}

    def readiness(self):
        return self.ready


class MessagingServiceTest(unittest.TestCase):
    def setUp(self):
        self.secret = b"secret"
        self.parent = uuid.uuid4()
        self.child = uuid.uuid4()

    def main_token(self):
        return issue_capability_token(self.secret, self.parent)

    def child_token(self, role="subissue"):
        return capability_token(
            self.secret,
            owning_issue_id=self.parent,
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
        self.assertEqual(capability.owning_issue_id, self.parent)
        self.assertEqual(capability.sender_id, self.child)
        self.assertEqual(capability.role, "subissue")
        with self.assertRaises(CapabilityError):
            inspect_capability(self.child_token()[:-1] + "x", self.secret)

    def test_parent_starts_one_direct_messaging_specialist(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret)

        result = service.start_specialist(
            self.main_token(),
            mission_key="plan-initial",
            prompt="Draft the bounded plan.",
            agent_type="plan",
            description="Draft plan",
            skills=["evex-delivery-planning"],
        )

        self.assertTrue(result["created"])
        self.assertEqual(uuid.UUID(result["conversationId"]).version, 5)
        call = provider.calls[0]
        self.assertEqual(call[0], "start-specialist")
        delegated = inspect_capability(call[1][2], self.secret)
        self.assertEqual(delegated.role, "specialist")
        self.assertEqual(delegated.owning_issue_id, self.parent)

    def test_specialist_description_uses_the_published_256_character_limit(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret)

        service.start_specialist(
            self.main_token(),
            mission_key="plan-long-description",
            prompt="Draft the bounded plan.",
            agent_type="plan",
            description="x" * 256,
            skills=["evex-delivery-planning"],
        )

        self.assertEqual(provider.calls[0][1][3]["description"], "x" * 256)
        service.start_specialist(
            self.main_token(),
            mission_key="plan-normalized-description",
            prompt="Draft the bounded plan.",
            agent_type="plan",
            description="x" + " " * 255,
            skills=["evex-delivery-planning"],
        )
        self.assertEqual(provider.calls[1][1][3]["description"], "x")

        with self.assertRaisesRegex(CapabilityError, "description exceeds 256 characters"):
            service.start_specialist(
                self.main_token(),
                mission_key="plan-too-long-description",
                prompt="Draft the bounded plan.",
                agent_type="plan",
                description="x" * 257,
                skills=["evex-delivery-planning"],
            )
        with self.assertRaisesRegex(CapabilityError, "description exceeds 256 characters"):
            service.start_specialist(
                self.main_token(),
                mission_key="plan-collapsible-description",
                prompt="Draft the bounded plan.",
                agent_type="plan",
                description="x" + " " * 256,
                skills=["evex-delivery-planning"],
            )
        self.assertEqual(len(provider.calls), 2)

    def test_specialist_starts_and_messages_one_direct_child_specialist(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret)
        specialist = uuid.uuid4()
        token = capability_token(
            self.secret,
            owning_issue_id=self.parent,
            sender_id=specialist,
            task_key="plan-initial",
            role="specialist",
        )
        result = service.start_specialist(
            token,
            mission_key="nested-review",
            prompt="Review the bounded candidate.",
            agent_type="code-review",
            description="Nested review",
            skills=[],
        )

        child_capability = inspect_capability(provider.calls[0][1][2], self.secret)
        self.assertEqual(child_capability.owning_issue_id, specialist)
        self.assertEqual(child_capability.sender_id, uuid.UUID(result["conversationId"]))

    def test_frozen_v2_capability_bytes_are_unchanged(self):
        owner = uuid.UUID("11111111-1111-4111-8111-111111111111")
        sender = uuid.UUID("22222222-2222-4222-8222-222222222222")
        expected = {
            "issue": "evx2_AhEREREREUERgRERERERERERERERERFBEYERERERERERAQMABHJvb3Rutjf5dleSJ6vwP79dGatYJTDYi2U70A5PWLatyaH9Rg",
            "subissue": "evx2_AhEREREREUERgREREREREREiIiIiIiJCIoIiIiIiIiIiAgIACGlzc3VlLTQyYX3YyOqYyAG-eSWChzl7mtZ2uGquaUVQglzWuFG_B4c",
            "spec": "evx2_AhEREREREUERgREREREREREiIiIiIiJCIoIiIiIiIiIiAwIACGlzc3VlLTQyXYd27yB8UqwCn9U_CZABuo9D_g_RN-SUR41KZNucBXU",
        }
        for role, frozen in expected.items():
            with self.subTest(role=role):
                token = capability_token(
                    b"frozen-test-secret", owning_issue_id=owner,
                    sender_id=owner if role == "issue" else sender,
                    task_key="root" if role == "issue" else "issue-42", role=role,
                )
                self.assertEqual(token, frozen)
                self.assertEqual(inspect_capability(token, b"frozen-test-secret").role, role)

    def test_project_capability_is_distinct_deterministic_and_send_only(self):
        token = capabilities.project_capability_token(self.secret, self.child, "native-project-id")
        self.assertEqual(token, capabilities.project_capability_token(self.secret, self.child, "native-project-id"))
        self.assertTrue(token.startswith("evx3_"))
        parsed = inspect_capability(token, self.secret)
        self.assertIsInstance(parsed, capabilities.ProjectCapability)
        self.assertEqual((parsed.sender_id, parsed.project_id, parsed.role), (self.child, "native-project-id", "project"))
        self.assertFalse(hasattr(parsed, "owning_issue_id"))
        self.assertFalse(hasattr(parsed, "task_key"))
        provider = FakeProvider()
        service = MessagingService(provider, self.secret)
        with self.assertRaises(CapabilityError):
            service.create_spec_chat(token)
        self.assertEqual(provider.calls, [])
        service.send_message(token, self.parent, "project-result", self.message())
        self.assertEqual(provider.calls[0], ("allowed", (self.child, self.parent, "project", None, "native-project-id")))
        self.assertEqual([call[0] for call in provider.calls], ["allowed", "send"])

    def test_project_and_delivery_capabilities_cannot_leak_in_messages(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret)
        for value in (capabilities.project_capability_token(self.secret, self.child, "native-project-id"), self.main_token()):
            with self.assertRaises(CapabilityError):
                service.send_message(self.main_token(), self.child, "key", self.message(value))
            with self.assertRaises(CapabilityError):
                service.send_message(self.main_token(), self.child, value, self.message())
        self.assertEqual(provider.calls, [])

    def test_embedded_capability_review_regression(self):
        tokens = (capabilities.project_capability_token(self.secret, self.child, "native-project-id"), self.main_token())
        for token in tokens:
            for location in ("humanSummary", "aiEvidence", "messageKey"):
                with self.subTest(version=token[:4], location=location):
                    provider = FakeProvider()
                    service = MessagingService(provider, self.secret)
                    value = "reference_" + token
                    message, key = self.message(), "key"
                    if location == "humanSummary":
                        message["humanSummary"] = value
                    elif location == "aiEvidence":
                        message["aiEvidence"]["evidence"] = [value]
                    else:
                        key = value
                    with self.assertRaises(CapabilityError) as error:
                        service.send_message(self.main_token(), self.child, key, message)
                    self.assertEqual(provider.calls, [])
                    self.assertLess(len(str(error.exception)), 200)
                    self.assertNotIn(token, str(error.exception))

    def test_project_capability_rejects_tampering_and_mixed_signed_formats(self):
        token = capabilities.project_capability_token(self.secret, self.child, "native-project-id")
        encoded = token.removeprefix("evx3_")
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))[:-32]

        def signed(value, prefix="evx3_"):
            return prefix + base64.urlsafe_b64encode(value + hmac.new(self.secret, value, hashlib.sha256).digest()).rstrip(b"=").decode()

        malformed = [
            "evx3_", "evx3_" + "x" * 1024, token + "=", token[:-2], "evx2_" + encoded,
            "evx3_" + self.main_token()[5:],
            signed(bytes([2]) + payload[1:]),
            signed(payload[:17] + bytes([3]) + payload[18:]),
            signed(payload[:18] + struct.pack(">H", 999) + payload[20:]),
            signed(payload + b"extra"),
            signed(payload[:18] + struct.pack(">H", 1) + b"\x00"),
            signed(payload[:18] + struct.pack(">H", 1) + b"\xff"),
            signed(payload[:18] + struct.pack(">H", 257) + b"x" * 257),
        ]
        raw = bytearray(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        raw[3] ^= 1
        malformed.append("evx3_" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode())
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(CapabilityError):
                inspect_capability(value, self.secret)
        with self.assertRaises(CapabilityError):
            inspect_capability(token, b"foreign-secret")

    def test_project_capability_ids_are_bounded_opaque_visible_ascii(self):
        for value in ("", "x" * 257, "with space", "\n", "ä", None, 4):
            with self.subTest(value=value), self.assertRaises(CapabilityError):
                capabilities.project_capability_token(self.secret, self.child, value)
        for value in ("x", "opaque:project-id", "x" * 256):
            token = capabilities.project_capability_token(self.secret, self.child, value)
            self.assertEqual(inspect_capability(token, self.secret).project_id, value)

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

    def test_only_issue_conversation_can_create_one_deterministic_spec_chat(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret)

        result = service.create_spec_chat(self.main_token())

        expected = deterministic_spec_chat_id(self.parent)
        self.assertEqual(result["specChatId"], str(expected))
        self.assertNotIn("capabilityRef", result)
        self.assertEqual(provider.calls[0][0], "create-spec")
        self.assertEqual(provider.calls[0][1][:2], (self.parent, expected))
        self.assertEqual(len(provider.calls[0][1]), 3)
        with self.assertRaisesRegex(CapabilityError, "Issue Conversation"):
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

    def test_terminal_message_accepts_one_bounded_complete_artifact(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret)
        message = self.message()
        message["aiEvidence"]["artifact"] = (
            "<!-- evex-delivery-plan -->\nComplete reviewed plan\n"
            "<!-- evex-plan-slice:v1 id=one -->"
        )

        service.send_message(self.child_token(), self.parent, "result", message)

        self.assertEqual(provider.calls[-1][1][3], message)

    def test_oversized_evidence_item_names_the_exact_repairable_bound(self):
        service = MessagingService(FakeProvider(), self.secret)
        message = self.message()
        message["aiEvidence"]["evidence"] = ["x" * 2001]

        with self.assertRaisesRegex(
            CapabilityError,
            r"aiEvidence\.evidence\[0\] exceeds 2000 UTF-8 bytes",
        ):
            service.send_message(self.child_token(), self.parent, "result", message)

        self.assertEqual(service._provider.calls, [])

    def test_readiness_is_provider_only_and_fail_closed(self):
        self.assertTrue(MessagingService(FakeProvider(), self.secret).readiness())
        self.assertFalse(MessagingService(FakeProvider(ready=False), self.secret).readiness())


if __name__ == "__main__":
    unittest.main()
