from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
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
        self.callback_succeeded = False

    def create_child(self, parent_id, child_id, role, task_key, mission, capability_ref, capabilities, model, reasoning_effort):
        self.calls.append(("create", parent_id, child_id, role, task_key, mission, capability_ref, capabilities, model, reasoning_effort))
        return {"created": True}

    def send_message(self, target_id, message_key, kind, text):
        self.calls.append(("send", target_id, message_key, kind, text))
        return {"accepted": True, "messageKey": message_key}

    def cancel_mission(self, target_id, message_key, task_key, owning_main_id):
        self.calls.append(("cancel", target_id, message_key, task_key, owning_main_id))
        return {"accepted": True}

    def resume_mission(self, target_id, message_key, task_key, context):
        self.calls.append(("resume", target_id, message_key, task_key, context))
        return {"accepted": True}

    def wait_until_terminal(self, target_id):
        self.calls.append(("wait-terminal", target_id))
        return "finished"

    def terminal_response(self, target_id):
        self.calls.append(("terminal-response", target_id))
        return "Welche Option soll gelten?\nA ...\nB ..."

    def parent_callback_succeeded(self, target_id):
        self.calls.append(("callback-succeeded", target_id))
        return self.callback_succeeded

    def usage(self, target_id):
        self.calls.append(("usage", target_id))
        return {"conversationId": str(target_id), "cacheHitRate": 0.9}


class MessagingTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 20, tzinfo=timezone.utc)
        self.secret = b"test-secret"
        self.main = uuid.UUID("11111111-1111-4111-8111-111111111111")

    def main_token(self):
        return main_capability_token(
            self.secret,
            self.main,
            issued_at=self.now - timedelta(minutes=1),
            expires_at=self.now + timedelta(hours=1),
        )

    def mission(self, *, checkout=True):
        value = {
            "immediateTask": "Your task now: implement the bounded fix.",
            "links": {"issue": "https://github.com/EvexU2/evex-u-workspace/issues/604"},
            "allowedMutations": ["write the assigned branch"],
            "prohibitions": ["Do not merge"],
            "skills": ["evex-delivery-writer"],
            "evidence": ["run focused tests"],
        }
        if checkout:
            value["checkout"] = {
                "repository": "EvexU2/evex-u-core",
                "branch": "fix/604",
                "headSha": "a" * 40,
            }
        return value

    def read_only_mission(self):
        value = self.mission()
        value["allowedMutations"] = []
        return value

    def create(self, service, *args, **kwargs):
        kwargs.setdefault("model", "gpt-5.6-luna")
        kwargs.setdefault("reasoning_effort", "medium")
        return service.create_child(*args, **kwargs)

    def test_capability_is_signed_and_target_bound(self):
        child = deterministic_child_id(self.main, "writer-604")
        token = capability_token(self.secret, owning_main_id=self.main, child_id=child, task_key="writer-604", role="writer", allowed_actions={"send_message"}, issued_at=self.now, expires_at=self.now + timedelta(hours=1))
        self.assertEqual(verify_capability(token, self.secret, now=self.now, action="send_message", target_id=child).child_id, child)
        with self.assertRaises(CapabilityError):
            verify_capability(token, self.secret, now=self.now, action="send_message", target_id=self.main)

    def test_trusted_dispatcher_mints_main_capability(self):
        token = main_capability_token(self.secret, self.main, issued_at=self.now, expires_at=self.now + timedelta(hours=1))
        self.assertTrue(token.startswith("evx1_"))
        self.assertLess(len(token), 160)
        verified = verify_capability(token, self.secret, now=self.now, action="create_child", target_id=self.main)
        self.assertEqual(verified.role, "main")

    def test_malformed_opaque_reference_has_one_safe_error(self):
        for value in ("", "not-a-ref", "evx1_invalid", "evx1_" + "A" * 20):
            with self.subTest(value=value), self.assertRaisesRegex(CapabilityError, "unknown or invalid capability reference"):
                verify_capability(value, self.secret, now=self.now, action="create_child", target_id=self.main)

    def test_service_creates_deterministic_child_and_sends(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        first = self.create(service, self.main_token(), "writer-604", "writer", self.mission())
        second = self.create(service, self.main_token(), "writer-604", "writer", self.mission())
        self.assertEqual(first["childId"], second["childId"])
        child = uuid.UUID(first["childId"])
        self.assertEqual(service.send_message(first["capabilityRef"], child, "result-1", "RESULT", "PASS")["accepted"], True)
        self.assertEqual(service.cancel_mission(self.main_token(), child, "writer-604", "cancel-1")["accepted"], True)
        self.assertEqual(service.resume_mission(self.main_token(), child, "writer-604", "resume-1", {"dependency": "cleared"})["accepted"], True)
        with self.assertRaisesRegex(CapabilityError, "verified facts"):
            service.resume_mission(
                self.main_token(), child, "writer-604", "resume-empty", {}
            )
        self.assertEqual([call[0] for call in provider.calls], ["create", "create", "send", "cancel", "resume"])

    def test_main_reads_own_and_deterministic_child_usage_only(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        child = deterministic_child_id(self.main, "writer-604")

        self.assertEqual(
            service.get_usage(self.main_token(), self.main, "root")["conversationId"],
            str(self.main),
        )
        self.assertEqual(
            service.get_usage(self.main_token(), child, "writer-604")["conversationId"],
            str(child),
        )
        with self.assertRaisesRegex(CapabilityError, "not the Main or its deterministic Child"):
            service.get_usage(
                self.main_token(),
                uuid.UUID("33333333-3333-4333-8333-333333333333"),
                "writer-604",
            )

    def test_main_inspects_its_transport_authority_without_provider_calls(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)

        authority = service.inspect_authority(self.main_token())

        self.assertEqual(
            authority,
            {
                "role": "main",
                "taskKey": "root",
                "allowedActions": [
                    "cancel_mission",
                    "create_child",
                    "read_usage",
                    "resume_mission",
                ],
                "expiresAt": "2026-08-20T01:00:00Z",
            },
        )
        self.assertEqual(provider.calls, [])

    def test_deputy_inspects_its_transport_authority(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        deputy = uuid.UUID("22222222-2222-4222-8222-222222222222")
        token = capability_token(
            self.secret,
            owning_main_id=self.main,
            child_id=deputy,
            task_key="issue-650",
            role="deputy",
            allowed_actions={"send_message", "create_child"},
            issued_at=self.now - timedelta(minutes=1),
            expires_at=self.now + timedelta(hours=2),
        )

        authority = service.inspect_authority(token)

        self.assertEqual(authority["role"], "deputy")
        self.assertEqual(authority["taskKey"], "issue-650")
        self.assertEqual(authority["allowedActions"], ["create_child", "send_message"])
        self.assertEqual(provider.calls, [])

    def test_child_inspects_its_transport_authority(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        child = self.create(
            service, self.main_token(), "issue-650-writer", "writer", self.mission()
        )
        provider.calls.clear()

        authority = service.inspect_authority(child["capabilityRef"])

        self.assertEqual(authority["role"], "writer")
        self.assertEqual(authority["taskKey"], "issue-650-writer")
        self.assertEqual(
            authority["allowedActions"],
            ["cancel_mission", "resume_mission", "send_message"],
        )
        self.assertEqual(provider.calls, [])

    def test_inspect_authority_rejects_missing_and_invalid_capabilities(self):
        service = MessagingService(FakeProvider(), self.secret, clock=lambda: self.now)

        for token in (None, "", "not-a-ref", "evx1_invalid"):
            with self.subTest(token=token), self.assertRaises(CapabilityError):
                service.inspect_authority(token)

    def test_inspect_authority_rejects_forged_capability_without_echoing_it(self):
        service = MessagingService(FakeProvider(), self.secret, clock=lambda: self.now)
        token = self.main_token()
        forged = token[:10] + ("A" if token[10] != "A" else "B") + token[11:]

        with self.assertRaises(CapabilityError) as raised:
            service.inspect_authority(forged)

        self.assertNotIn(forged, str(raised.exception))

    def test_inspect_authority_rejects_expired_capability_without_echoing_it(self):
        service = MessagingService(FakeProvider(), self.secret, clock=lambda: self.now)
        expired = main_capability_token(
            self.secret,
            self.main,
            issued_at=self.now - timedelta(hours=2),
            expires_at=self.now,
        )

        with self.assertRaises(CapabilityError) as raised:
            service.inspect_authority(expired)

        self.assertNotIn(expired, str(raised.exception))

    def test_role_child_capability_is_valid_for_exactly_twenty_four_hours(self):
        service = MessagingService(FakeProvider(), self.secret, clock=lambda: self.now)

        child = self.create(service,
            self.main_token(), "writer-24h", "writer", self.mission()
        )
        verified = verify_capability(
            child["capabilityRef"],
            self.secret,
            now=self.now,
            action="send_message",
            target_id=uuid.UUID(child["childId"]),
        )

        self.assertEqual(verified.expires_at - verified.issued_at, timedelta(hours=24))

    def test_main_cannot_cancel_child_outside_its_deterministic_task(self):
        service = MessagingService(FakeProvider(), self.secret, clock=lambda: self.now)
        foreign_child = uuid.uuid4()
        with self.assertRaisesRegex(CapabilityError, "deterministic Child"):
            service.cancel_mission(
                self.main_token(), foreign_child, "writer-604", "cancel-foreign"
            )

    def test_terminal_hook_wakes_parent_with_stable_semantic_key(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        child = self.create(service, self.main_token(), "review-612", "reviewer", self.read_only_mission())

        first = service.terminal_wake(child["capabilityRef"])
        second = service.terminal_wake(child["capabilityRef"])

        self.assertTrue(first["accepted"])
        self.assertEqual(first["messageKey"], second["messageKey"])
        self.assertNotIn("wait-terminal", [call[0] for call in provider.calls])
        sends = [call for call in provider.calls if call[0] == "send"]
        self.assertEqual([call[1] for call in sends], [self.main, self.main])
        self.assertTrue(all(call[3] == "RECOVERY_WAKE" for call in sends))
        envelope = json.loads(sends[0][4])
        self.assertEqual(
            envelope["terminalResponse"],
            "Welche Option soll gelten?\nA ...\nB ...",
        )

    def test_terminal_hook_is_noop_after_successful_explicit_callback(self):
        provider = FakeProvider()
        provider.callback_succeeded = True
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        child = self.create(
            service, self.main_token(), "review-noop", "reviewer", self.read_only_mission()
        )

        result = service.terminal_wake(child["capabilityRef"])

        self.assertEqual(result, {"accepted": True, "alreadyReported": True})
        self.assertNotIn("terminal-response", [call[0] for call in provider.calls])
        self.assertNotIn("send", [call[0] for call in provider.calls])

    def test_runtime_capability_is_explicit_per_child_mission(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)

        self.create(service, self.main_token(), "writer-source", "writer", self.mission())
        self.create(service,
            self.main_token(),
            "qa-integrated",
            "qa",
            self.read_only_mission(),
            capabilities=["runtime_environment"],
        )

        creates = [call for call in provider.calls if call[0] == "create"]
        self.assertEqual(creates[0][-3], frozenset())
        self.assertEqual(creates[1][-3], frozenset({"runtime_environment"}))
        with self.assertRaises(CapabilityError):
            self.create(service,
                self.main_token(), "writer-broad", "writer", self.mission(), capabilities=["all_tools"]
            )
        with self.assertRaisesRegex(CapabilityError, "limited to QA or repair"):
            self.create(service,
                self.main_token(),
                "writer-runtime",
                "writer",
                self.mission(),
                capabilities=["runtime_environment"],
            )

    def test_child_can_only_report_to_owning_main_and_request_decision(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        child = self.create(service, self.main_token(), "qa-604", "qa", self.read_only_mission())
        self.assertTrue(service.send_to_parent(child["capabilityRef"], {"messageKey": "result-1", "kind": "RESULT", "status": "PASS"})["accepted"])
        self.assertTrue(service.request_user_decision(child["capabilityRef"], "Choose rollout", ["A", "B", "C"])["accepted"])
        self.assertTrue(service.publish_navigation_links(child["capabilityRef"], {"main": "https://openhands.local/conversations/x"})["accepted"])
        self.assertEqual([call[1] for call in provider.calls if call[0] == "send"], [self.main, self.main, self.main])

    def test_send_to_parent_derives_transport_fields_from_bound_result(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        child = self.create(service, self.main_token(), "qa-lean", "qa", self.read_only_mission())

        first = service.send_to_parent(
            child["capabilityRef"], {"outcome": "PASS", "summary": "Focused QA passed"}
        )
        second = service.send_to_parent(
            child["capabilityRef"], {"summary": "Focused QA passed", "outcome": "PASS"}
        )

        self.assertTrue(first["accepted"])
        self.assertEqual(first["messageKey"], second["messageKey"])
        sent = [call for call in provider.calls if call[0] == "send"]
        self.assertEqual(sent[-1][3], "RESULT")
        envelope = json.loads(sent[-1][4])
        self.assertEqual(envelope["kind"], "RESULT")
        self.assertEqual(json.loads(envelope["text"])["outcome"], "PASS")

    def test_deputy_uses_standard_result_to_report_to_parent(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        deputy = uuid.UUID("22222222-2222-4222-8222-222222222222")
        deputy_token = capability_token(
            self.secret,
            owning_main_id=self.main,
            child_id=deputy,
            task_key="issue-626",
            role="deputy",
            allowed_actions={"create_child", "send_message", "cancel_mission", "resume_mission"},
            issued_at=self.now,
            expires_at=self.now + timedelta(hours=24),
        )

        result = service.send_to_parent(
            deputy_token,
            {
                "messageKey": "deputy-result:626:candidate-passed:abc",
                "kind": "RESULT",
                "outcome": "candidate-passed",
            },
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(provider.calls[-1][1], self.main)
        self.assertEqual(provider.calls[-1][3], "RESULT")

    def test_deputy_specialist_reports_to_the_creating_deputy(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        deputy = uuid.UUID("22222222-2222-4222-8222-222222222222")
        deputy_token = capability_token(
            self.secret,
            owning_main_id=self.main,
            child_id=deputy,
            task_key="issue-626",
            role="deputy",
            allowed_actions={"create_child", "send_message", "cancel_mission", "resume_mission"},
            issued_at=self.now,
            expires_at=self.now + timedelta(hours=24),
        )

        reviewer = self.create(service,
            deputy_token,
            "review-f8bb35f",
            "reviewer",
            self.read_only_mission(),
        )
        reviewer_capability = verify_capability(
            reviewer["capabilityRef"],
            self.secret,
            now=self.now,
            action="send_message",
            target_id=uuid.UUID(reviewer["childId"]),
        )
        sent = service.send_to_parent(
            reviewer["capabilityRef"],
            {"messageKey": "review:f8bb35f:pass", "kind": "RESULT", "outcome": "PASS"},
        )

        self.assertEqual(reviewer_capability.owning_main_id, deputy)
        self.assertEqual(provider.calls[0][5]["owningMainId"], str(deputy))
        self.assertTrue(sent["accepted"])
        self.assertEqual(provider.calls[-1][1], deputy)

    def test_expired_or_wrong_role_capability_fails(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        bad = capability_token(self.secret, owning_main_id=self.main, child_id=self.main, task_key="root", role="writer", allowed_actions={"create_child"}, issued_at=self.now - timedelta(hours=2), expires_at=self.now - timedelta(hours=1))
        with self.assertRaises(CapabilityError):
            self.create(service, bad, "writer", "writer", self.mission())

    def test_create_child_builds_bound_mission_before_provider_call(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)

        result = self.create(service, self.main_token(), "writer-bound", "writer", self.mission())

        mission = provider.calls[0][5]
        self.assertEqual(mission["owningMainId"], str(self.main))
        self.assertEqual(mission["childId"], result["childId"])
        self.assertEqual(mission["taskKey"], "writer-bound")
        self.assertEqual(mission["role"], "writer")
        self.assertEqual(mission["callback"]["tool"], "send_to_parent")
        self.assertNotIn("capabilityRef", mission["callback"])

    def test_create_child_rejects_incomplete_mission_before_provider_call(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)

        for mission in ({}, {"immediateTask": "Implement"}, {**self.mission(), "checkout": None}):
            with self.subTest(mission=mission), self.assertRaises(CapabilityError):
                self.create(service, self.main_token(), "writer-invalid", "writer", mission)
        self.assertEqual(provider.calls, [])

    def test_create_child_rejects_restatement_sized_mission(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        mission = self.mission()
        mission["evidence"] = ["x" * 6000]

        with self.assertRaisesRegex(CapabilityError, "bounded"):
            self.create(service, self.main_token(), "writer-huge", "writer", mission)

        self.assertEqual(provider.calls, [])

    def test_role_model_reasoning_and_mutation_envelope_fail_closed(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        writable = self.mission()
        writable["allowedMutations"] = ["commit and push only the assigned branch"]

        with self.assertRaisesRegex(CapabilityError, "model or reasoning"):
            service.create_child(
                self.main_token(), "missing-profile-604", "writer", writable
            )

        self.create(service,
            self.main_token(),
            "plan-author-604",
            "plan-author",
            writable,
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )
        created = provider.calls[-1]
        self.assertEqual(created[-2:], ("gpt-5.6-sol", "high"))

        with self.assertRaisesRegex(CapabilityError, "read-only"):
            self.create(service,
                self.main_token(),
                "review-604",
                "reviewer",
                writable,
                model="gpt-5.6-terra",
                reasoning_effort="medium",
            )
        with self.assertRaisesRegex(CapabilityError, "unsupported Child role"):
            self.create(service,
                self.main_token(),
                "wait-ci-604",
                "waiter",
                self.read_only_mission(),
                model="gpt-5.6-luna",
                reasoning_effort="medium",
            )
        with self.assertRaisesRegex(CapabilityError, "write-authorized"):
            self.create(service,
                self.main_token(),
                "writer-empty-604",
                "writer",
                self.read_only_mission(),
                model="gpt-5.6-terra",
                reasoning_effort="medium",
            )
        with self.assertRaisesRegex(CapabilityError, "unsupported Child role"):
            self.create(service,
                self.main_token(),
                "legacy-planner-604",
                "planner",
                writable,
                model="gpt-5.6-sol",
                reasoning_effort="medium",
            )
        with self.assertRaisesRegex(CapabilityError, "model or reasoning"):
            self.create(service,
                self.main_token(),
                "bad-profile-604",
                "writer",
                writable,
                model="gpt-5.6-sol",
                reasoning_effort="low",
            )
        invalid_mutation = self.mission()
        invalid_mutation["allowedMutations"] = [""]
        with self.assertRaisesRegex(CapabilityError, "string array"):
            self.create(service,
                self.main_token(),
                "blank-mutation-604",
                "writer",
                invalid_mutation,
                model="gpt-5.6-sol",
                reasoning_effort="medium",
            )
