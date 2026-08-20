from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evex_agent_messaging.capability import CapabilityError, capability_token, deterministic_child_id, main_capability_token, verify_capability  # noqa: E402
from evex_agent_messaging.service import MessagingService  # noqa: E402


class FakeProvider:
    def __init__(self):
        self.calls = []

    def create_child(self, parent_id, child_id, role, task_key, mission):
        self.calls.append(("create", parent_id, child_id, role, task_key, mission))
        return {"created": True}

    def send_message(self, target_id, message_key, kind, text):
        self.calls.append(("send", target_id, message_key, kind, text))
        return {"accepted": True, "messageKey": message_key}

    def cancel_mission(self, target_id, message_key, task_key):
        self.calls.append(("cancel", target_id, message_key, task_key))
        return {"accepted": True}

    def resume_mission(self, target_id, message_key, task_key):
        self.calls.append(("resume", target_id, message_key, task_key))
        return {"accepted": True}


class MessagingTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 20, tzinfo=timezone.utc)
        self.secret = b"test-secret"
        self.main = uuid.UUID("11111111-1111-4111-8111-111111111111")

    def main_token(self):
        return capability_token(self.secret, owning_main_id=self.main, child_id=self.main, task_key="root", role="main", allowed_actions={"create_child"}, issued_at=self.now - timedelta(minutes=1), expires_at=self.now + timedelta(hours=1))

    def test_capability_is_signed_and_target_bound(self):
        child = deterministic_child_id(self.main, "writer-604")
        token = capability_token(self.secret, owning_main_id=self.main, child_id=child, task_key="writer-604", role="writer", allowed_actions={"send_message"}, issued_at=self.now, expires_at=self.now + timedelta(hours=1))
        self.assertEqual(verify_capability(token, self.secret, now=self.now, action="send_message", target_id=child).child_id, child)
        with self.assertRaises(CapabilityError):
            verify_capability(token, self.secret, now=self.now, action="send_message", target_id=self.main)

    def test_trusted_dispatcher_mints_main_capability(self):
        token = main_capability_token(self.secret, self.main, issued_at=self.now, expires_at=self.now + timedelta(hours=1))
        verified = verify_capability(token, self.secret, now=self.now, action="create_child", target_id=self.main)
        self.assertEqual(verified.role, "main")

    def test_service_creates_deterministic_child_and_sends(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        first = service.create_child(self.main_token(), "writer-604", "writer", "Implement the bounded fix")
        second = service.create_child(self.main_token(), "writer-604", "writer", "Implement the bounded fix")
        self.assertEqual(first["childId"], second["childId"])
        child = uuid.UUID(first["childId"])
        self.assertEqual(service.send_message(first["capability"], child, "result-1", "RESULT", "PASS")["accepted"], True)
        self.assertEqual(service.cancel_mission(first["capability"], child, "cancel-1")["accepted"], True)
        self.assertEqual(service.resume_mission(first["capability"], child, "resume-1")["accepted"], True)
        self.assertEqual([call[0] for call in provider.calls], ["create", "create", "send", "cancel", "resume"])

    def test_child_can_only_report_to_owning_main_and_request_decision(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        child = service.create_child(self.main_token(), "qa-604", "qa", "Run QA")
        self.assertTrue(service.send_to_parent(child["capability"], {"messageKey": "result-1", "kind": "RESULT", "status": "PASS"})["accepted"])
        self.assertTrue(service.request_user_decision(child["capability"], "Choose rollout", ["A", "B", "C"])["accepted"])
        self.assertTrue(service.publish_navigation_links(child["capability"], {"main": "https://openhands.local/conversations/x"})["accepted"])
        self.assertEqual([call[1] for call in provider.calls if call[0] == "send"], [self.main, self.main, self.main])

    def test_expired_or_wrong_role_capability_fails(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        bad = capability_token(self.secret, owning_main_id=self.main, child_id=self.main, task_key="root", role="writer", allowed_actions={"create_child"}, issued_at=self.now - timedelta(hours=2), expires_at=self.now - timedelta(hours=1))
        with self.assertRaises(CapabilityError):
            service.create_child(bad, "writer", "writer", "x")
