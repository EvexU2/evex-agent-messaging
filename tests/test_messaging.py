from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    verify_capability,
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

    def readiness(self):
        return self.ready


class MessagingServiceTest(unittest.TestCase):
    def setUp(self):
        self.secret = b"secret"
        self.now = datetime(2026, 8, 27, tzinfo=timezone.utc)
        self.parent = uuid.uuid4()
        self.child = uuid.uuid4()

    def main_token(self):
        return main_capability_token(
            self.secret,
            self.parent,
            issued_at=self.now,
            expires_at=self.now + timedelta(hours=24),
        )

    def child_token(self, role="deputy"):
        return capability_token(
            self.secret,
            owning_main_id=self.parent,
            sender_id=self.child,
            task_key="issue-42",
            role=role,
            issued_at=self.now,
            expires_at=self.now + timedelta(hours=24),
        )

    def test_capability_is_signed_sender_bound_and_send_only(self):
        capability = inspect_capability(self.child_token(), self.secret)
        self.assertEqual(capability.owning_main_id, self.parent)
        self.assertEqual(capability.sender_id, self.child)
        self.assertEqual(capability.role, "deputy")
        with self.assertRaises(CapabilityError):
            inspect_capability(self.child_token()[:-1] + "x", self.secret)

    def test_expired_capability_fails_closed(self):
        with self.assertRaisesRegex(CapabilityError, "expired"):
            verify_capability(
                self.child_token(),
                self.secret,
                now=self.now + timedelta(days=2),
            )

    def test_send_message_checks_relationship_before_provider_post(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        result = service.send_message(self.child_token(), self.parent, "result-1", "PR passed")
        self.assertEqual(result, {"accepted": True, "messageKey": "result-1"})
        self.assertEqual([call[0] for call in provider.calls], ["allowed", "send"])

    def test_wrong_target_and_self_target_fail_closed(self):
        denied = FakeProvider(allowed=False)
        service = MessagingService(denied, self.secret, clock=lambda: self.now)
        with self.assertRaisesRegex(CapabilityError, "not allowed"):
            service.send_message(self.child_token(), uuid.uuid4(), "key", "text")
        with self.assertRaisesRegex(CapabilityError, "not allowed"):
            service.send_message(self.main_token(), self.parent, "key", "text")
        self.assertFalse(any(call[0] == "send" for call in denied.calls))

    def test_message_key_and_text_are_bounded(self):
        service = MessagingService(FakeProvider(), self.secret, clock=lambda: self.now)
        for key, text in (("", "text"), ("x" * 201, "text"), ("key", " "), ("key", "x" * 20001)):
            with self.subTest(key=len(key), text=len(text)), self.assertRaises(CapabilityError):
                service.send_message(self.child_token(), self.parent, key, text)

    def test_readiness_is_provider_only_and_fail_closed(self):
        self.assertTrue(MessagingService(FakeProvider(), self.secret).readiness())
        self.assertFalse(MessagingService(FakeProvider(ready=False), self.secret).readiness())


if __name__ == "__main__":
    unittest.main()
