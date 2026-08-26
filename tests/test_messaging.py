from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import sys
import unittest
import uuid
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evex_agent_messaging.capability import CapabilityError, capability_token, deterministic_child_id, main_capability_token, verify_capability  # noqa: E402
from evex_agent_messaging.provider import OpenHandsProvider  # noqa: E402
from evex_agent_messaging.service import MessagingService  # noqa: E402


class FakeProvider:
    def __init__(self):
        self.calls = []

    def create_child(self, parent_id, child_id, role, task_key, mission, capability_ref, capabilities, model, reasoning_effort):
        self.calls.append(("create", parent_id, child_id, role, task_key, mission, capability_ref, capabilities, model, reasoning_effort))
        return {"created": True}

    def send_message(self, target_id, message_key, kind, text):
        self.calls.append(("send", target_id, message_key, kind, text))
        return {"accepted": True, "messageKey": message_key}

    def cancel_mission(self, target_id, message_key, task_key, owning_main_id):
        self.calls.append(("cancel", target_id, message_key, task_key, owning_main_id))
        return {"accepted": True}

    def resume_mission(self, target_id, message_key, task_key, context, owning_main_id=None):
        self.calls.append(("resume", target_id, message_key, task_key, context, owning_main_id))
        return {"accepted": True}

    def send_child_message(self, child_id, target_id, message_key, kind, text):
        self.calls.append(("child-send", child_id, target_id, message_key, kind, text))
        return {"accepted": True, "messageKey": message_key}

    def replacement_cancelled(self, target_id, task_key, message_key, owning_main_id):
        self.calls.append(("replacement-cancelled", target_id, task_key, message_key, owning_main_id))
        return True

    def wait_until_terminal(self, target_id):
        self.calls.append(("wait-terminal", target_id))
        return "finished"

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

    def reviewer_mission(self):
        value = self.read_only_mission()
        value["links"]["specificationPr"] = (
            "https://github.com/EvexU2/evex-u-core/pull/836"
        )
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
        for context in (
            {"allowedMutations": ["push main"]},
            {"finding": {"checkout": {"branch": "main"}}},
            {"capabilities": ["runtime_environment"]},
        ):
            with self.subTest(context=context), self.assertRaisesRegex(
                CapabilityError, "cannot expand Mission authority"
            ):
                service.resume_mission(
                    self.main_token(), child, "writer-604", "resume-authority", context
                )
        self.assertEqual([call[0] for call in provider.calls], ["create", "create", "send", "cancel", "resume"])

    def test_reviewer_candidate_authority_is_one_canonical_mission_pr(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)

        admitted = self.create(
            service,
            self.main_token(),
            "reviewer-836",
            "reviewer",
            self.reviewer_mission(),
        )

        self.assertEqual(
            provider.calls[0][5]["links"]["specificationPr"],
            "https://github.com/EvexU2/evex-u-core/pull/836",
        )
        child = uuid.UUID(admitted["childId"])
        service.resume_mission(
            self.main_token(),
            child,
            "reviewer-836",
            "resume:repaired",
            {"currentRevision": "b" * 40, "findings": ["P2-1"]},
        )
        self.assertEqual(provider.calls[-1][4]["currentRevision"], "b" * 40)

        for value in (
            "http://github.com/EvexU2/evex-u-core/pull/836",
            "https://github.com/evexu2/evex-u-core/pull/836",
            "https://github.com/EvexU2/evex-u-core/pull/836/",
            "https://github.com/EvexU2/foreign/pull/836",
        ):
            mission = self.reviewer_mission()
            mission["links"]["specificationPr"] = value
            with self.subTest(value=value), self.assertRaisesRegex(
                CapabilityError, "canonical specificationPr"
            ):
                self.create(
                    service,
                    self.main_token(),
                    "reviewer-invalid-pr-836",
                    "reviewer",
                    mission,
                )

    def test_resume_candidate_context_is_equality_only(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        admitted = self.create(
            service,
            self.main_token(),
            "reviewer-836",
            "reviewer",
            self.reviewer_mission(),
        )
        child = uuid.UUID(admitted["childId"])

        for context in (
            {"currentRevision": "not-a-sha"},
            {"specificationPr": "https://github.com/EvexU2/evex-u-core/pull/837"},
            {"pullRequest": "https://github.com/EvexU2/evex-u-core/pull/837"},
            {"candidate": "b" * 40},
            {"ref": "refs/pull/837/head"},
            {"head": "b" * 40},
            {"current_revision": "b" * 40},
        ):
            with self.subTest(context=context), self.assertRaises(CapabilityError):
                service.resume_mission(
                    self.main_token(),
                    child,
                    "reviewer-836",
                    "resume:authority-expansion",
                    context,
                )

        self.assertEqual(
            [call[0] for call in provider.calls],
            ["create"],
        )

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

    def test_replacement_requires_terminal_proof_new_identity_and_stable_authorized_projection(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        cancelled_task = "writer-604"
        cancelled_child = deterministic_child_id(self.main, cancelled_task)
        mission = self.mission()
        mission["checkout"] = {**mission["checkout"], "branch": "fix/605", "headSha": "b" * 40}
        projection = {
            "branch": "fix/605",
            "headSha": "b" * 40,
            "draftPullRequest": "https://github.com/EvexU2/evex-agent-messaging/pull/34",
        }
        proof = {
            "cancelledChildId": str(cancelled_child),
            "cancelledTaskKey": cancelled_task,
            "cancellationKey": "cancel:604",
            "postTerminalProjection": projection,
            "preAdmissionProjection": dict(projection),
        }

        replacement = self.create(
            service, self.main_token(), "writer-605", "writer", mission, replacement=proof
        )

        self.assertNotEqual(replacement["childId"], str(cancelled_child))
        self.assertEqual(provider.calls[0][0], "replacement-cancelled")
        self.assertEqual(provider.calls[1][0], "create")

        changed = {**proof, "preAdmissionProjection": {**projection, "headSha": "c" * 40}}
        with self.assertRaisesRegex(CapabilityError, "reconcile and restart"):
            self.create(service, self.main_token(), "writer-606", "writer", mission, replacement=changed)
        self.assertEqual(len(provider.calls), 2)

    def test_replacement_requires_provider_native_cancellation_proof(self):
        provider = FakeProvider()
        provider.replacement_cancelled = lambda *_args: False
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        task = "writer-604"
        projection = {
            "branch": "fix/604", "headSha": "a" * 40,
            "draftPullRequest": "https://github.com/EvexU2/evex-agent-messaging/pull/34",
        }
        proof = {
            "cancelledChildId": str(deterministic_child_id(self.main, task)),
            "cancelledTaskKey": task,
            "cancellationKey": "cancel:604",
            "postTerminalProjection": projection,
            "preAdmissionProjection": dict(projection),
        }

        with self.assertRaisesRegex(CapabilityError, "native terminal CANCELLED"):
            self.create(service, self.main_token(), "writer-605", "writer", self.mission(), replacement=proof)

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
        self.create(service,
            self.main_token(),
            "writer-integrated",
            "writer",
            self.mission(),
            capabilities=["runtime_environment"],
        )

        creates = [call for call in provider.calls if call[0] == "create"]
        self.assertEqual(creates[0][-3], frozenset())
        self.assertEqual(creates[1][-3], frozenset({"runtime_environment"}))
        self.assertEqual(creates[2][-3], frozenset({"runtime_environment"}))
        with self.assertRaises(CapabilityError):
            self.create(service,
                self.main_token(), "writer-broad", "writer", self.mission(), capabilities=["all_tools"]
            )
        with self.assertRaisesRegex(CapabilityError, "limited to writer, QA, or repair"):
            self.create(service,
                self.main_token(),
                "review-runtime",
                "reviewer",
                self.read_only_mission(),
                capabilities=["runtime_environment"],
            )

    def test_plan_author_mission_is_read_only(self):
        service = MessagingService(FakeProvider(), self.secret, clock=lambda: self.now)

        child = self.create(
            service,
            self.main_token(),
            "plan-read-only",
            "plan-author",
            self.read_only_mission(),
        )

        self.assertTrue(child["created"])
        with self.assertRaisesRegex(CapabilityError, "plan author missions are read-only"):
            self.create(
                service,
                self.main_token(),
                "plan-write",
                "plan-author",
                self.mission(),
            )

    def test_paused_write_admission_rejects_spec_and_writer_before_provider_mutation(self):
        provider = FakeProvider()
        service = MessagingService(
            provider, self.secret, clock=lambda: self.now, write_mission_admission_paused=True
        )

        for role in ("spec", "writer"):
            with self.subTest(role=role), self.assertRaisesRegex(
                CapabilityError, "^write_mission_admission_paused$"
            ):
                self.create(service, self.main_token(), f"{role}-paused", role, self.mission())

        admitted = self.create(
            service, self.main_token(), "review-paused", "reviewer", self.read_only_mission()
        )
        self.assertTrue(admitted["created"])
        self.assertEqual([call[0:4] for call in provider.calls], [("create", self.main, uuid.UUID(admitted["childId"]), "reviewer")])

    def test_child_can_only_report_to_owning_main_and_request_decision(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        child = self.create(service, self.main_token(), "qa-604", "qa", self.read_only_mission())
        self.assertTrue(service.send_to_parent(child["capabilityRef"], {"callbackGeneration": "evxg1_" + "0" * 64, "messageKey": "result-1", "kind": "RESULT", "status": "PASS"})["accepted"])
        self.assertTrue(service.request_user_decision(child["capabilityRef"], "Choose rollout", ["A", "B", "C"], "evxg1_" + "0" * 64)["accepted"])
        self.assertTrue(service.publish_navigation_links(child["capabilityRef"], {"main": "https://openhands.local/conversations/x"})["accepted"])
        self.assertEqual(
            [call[2] for call in provider.calls if call[0] == "child-send"],
            [self.main, self.main],
        )
        self.assertEqual([call[1] for call in provider.calls if call[0] == "send"], [self.main])

    def test_send_to_parent_ignores_legacy_callback_generation_metadata(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        child = self.create(service, self.main_token(), "qa-generation", "qa", self.read_only_mission())

        results = [
            service.send_to_parent(
                child["capabilityRef"],
                {
                    "messageKey": "caller-selected-a",
                    "kind": "RESULT",
                    "outcome": "PASS",
                },
            ),
            service.send_to_parent(
                child["capabilityRef"],
                {
                    "callbackGeneration": "legacy-generation-one",
                    "messageKey": "caller-selected-b",
                    "kind": "RESULT",
                    "outcome": "PASS",
                },
            ),
            service.send_to_parent(
                child["capabilityRef"],
                {
                    "callbackGeneration": {
                        "mission": {"allowedMutations": ["ignored metadata"]}
                    },
                    "kind": "RESULT",
                    "outcome": "PASS",
                },
            ),
        ]

        self.assertTrue(all(result["accepted"] for result in results))
        sent = [call for call in provider.calls if call[0] == "child-send"]
        self.assertEqual(len({call[3] for call in sent}), 1)
        for call in sent:
            envelope = json.loads(call[5])
            self.assertNotIn("callbackGeneration", envelope)
            self.assertNotIn("callbackGeneration", json.loads(envelope["text"]))

    def test_send_to_parent_derives_transport_fields_from_bound_result(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        child = self.create(service, self.main_token(), "qa-lean", "qa", self.read_only_mission())

        first = service.send_to_parent(
            child["capabilityRef"], {"callbackGeneration": "evxg1_" + "0" * 64, "outcome": "PASS", "summary": "Focused QA passed"}
        )
        second = service.send_to_parent(
            child["capabilityRef"], {"callbackGeneration": "evxg1_" + "0" * 64, "summary": "Focused QA passed", "outcome": "PASS"}
        )

        self.assertTrue(first["accepted"])
        self.assertEqual(first["messageKey"], second["messageKey"])
        sent = [call for call in provider.calls if call[0] == "child-send"]
        self.assertEqual(sent[-1][4], "RESULT")
        envelope = json.loads(sent[-1][5])
        self.assertEqual(envelope["kind"], "RESULT")
        self.assertEqual(json.loads(envelope["text"])["outcome"], "PASS")
        self.assertNotIn("callbackGeneration", json.loads(envelope["text"]))

    def test_send_to_parent_rejects_invalid_payload_without_provider_mutation(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        child = self.create(
            service, self.main_token(), "qa-invalid-result", "qa", self.read_only_mission()
        )

        invalid_results = (
            {"kind": "OTHER", "outcome": "PASS"},
            {"kind": "RESULT", "owning_main_id": str(uuid.uuid4())},
            {"kind": "RESULT", "mission": {"allowedMutations": ["push main"]}},
            {"kind": "RESULT", "details": {"mission": {"scope": "expand"}}},
            {
                "kind": "RESULT",
                "findings": [{"allowed_mutations": ["push main"]}],
            },
            {"kind": "RESULT", "details": {"transport": "raw-provider"}},
            {
                "kind": "RESULT",
                "findings": [
                    {"evidence": {"capability_ref": "evx1_caller-selected"}}
                ],
            },
            {"kind": "RESULT", "summary": "x" * 20001},
            {"kind": "RESULT", "summary": {"not-json": object()}},
        )
        for result in invalid_results:
            with self.subTest(result=result), self.assertRaises(CapabilityError):
                service.send_to_parent(child["capabilityRef"], result)

        self.assertEqual([call[0] for call in provider.calls], ["create"])

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
                "callbackGeneration": "evxg1_" + "0" * 64,
                "kind": "RESULT",
                "outcome": "candidate-passed",
            },
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(provider.calls[-1][2], self.main)
        self.assertEqual(provider.calls[-1][4], "RESULT")

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
            {"callbackGeneration": "evxg1_" + "0" * 64, "messageKey": "review:f8bb35f:pass", "kind": "RESULT", "outcome": "PASS"},
        )

        self.assertEqual(reviewer_capability.owning_main_id, deputy)
        self.assertEqual(provider.calls[0][5]["owningMainId"], str(deputy))
        self.assertTrue(sent["accepted"])
        self.assertEqual(provider.calls[-1][2], deputy)

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
        self.assertEqual(
            mission["callback"],
            {
                "tool": "send_to_parent",
                "requiredBeforeFinish": True,
                "successEvidence": {"accepted": True},
                "onFailure": (
                    "Retry the identical result at most twice; if every attempt fails, "
                    "preserve that result as the final response for Recovery Mode."
                ),
            },
        )
        self.assertNotIn("capabilityRef", mission["callback"])

    def test_display_title_is_normalized_and_bound_for_navigation(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        mission = self.mission()
        mission["displayTitle"] = "  Runtime   resolution  "

        self.create(service, self.main_token(), "writer-title", "writer", mission)

        self.assertEqual(provider.calls[0][5]["displayTitle"], "Runtime resolution")

    def test_display_title_requires_bounded_text_and_canonical_workspace_issue(self):
        service = MessagingService(FakeProvider(), self.secret, clock=lambda: self.now)
        invalid = []
        for title in ("x", "x" * 61, 42):
            mission = self.mission()
            mission["displayTitle"] = title
            invalid.append(mission)
        mission = self.mission()
        mission["displayTitle"] = "Runtime resolution"
        mission["links"]["issue"] = "https://github.com/EvexU2/other/issues/604"
        invalid.append(mission)

        for mission in invalid:
            with self.subTest(mission=mission), self.assertRaises(CapabilityError):
                self.create(service, self.main_token(), "writer-invalid-title", "writer", mission)

    def test_legacy_mission_without_display_title_remains_accepted(self):
        service = MessagingService(FakeProvider(), self.secret, clock=lambda: self.now)

        result = self.create(
            service, self.main_token(), "writer-legacy", "writer", self.mission()
        )

        self.assertTrue(result["created"])

    def test_create_child_accepts_recovery_reviewer_mission(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        mission = {
            "immediateTask": (
                "Your task now: independently review the exact governed skill candidate."
            ),
            "displayTitle": "Governed skill candidate",
            "links": {
                "issue": "https://github.com/EvexU2/evex-u-workspace/issues/687",
                "specification": (
                    "https://github.com/EvexU2/evex-u-workspace/blob/main/"
                    "docs/02_specs/platform-foundation/delivery/agent-delivery/"
                    "openhands-autonomous-delivery.md"
                ),
                "pullRequest": "https://github.com/EvexU2/evex-agent-skills/pull/105",
            },
            "checkout": {
                "repository": "EvexU2/evex-agent-skills",
                "branch": "recovery-issue-687-integrated-review-fcca45c",
                "headSha": "fcca45cb7534e835688e3143afa83bb0e11c9c7d",
            },
            "allowedMutations": [],
            "prohibitions": [
                "Do not mutate source, branches, pull requests, or GitHub state.",
                "Do not inspect provider implementation or credentials.",
            ],
            "skills": [
                "evex-delivery-specialist",
                "evex-delivery-protocol",
                "evex-delivery-reviewer",
                "evex-review",
                "evex-u-code-review",
            ],
            "evidence": [
                "Review the exact checkout findings-first.",
                "Return the verdict through send_to_parent.",
            ],
        }

        result = self.create(
            service,
            self.main_token(),
            "recovery-issue-687-integrated-review-fcca45c",
            "reviewer",
            mission,
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )

        self.assertTrue(result["created"])
        self.assertEqual(1, len(provider.calls))
        self.assertEqual(mission["checkout"], provider.calls[0][5]["checkout"])
        self.assertEqual([], provider.calls[0][5]["allowedMutations"])

    def test_create_child_names_each_missing_required_mission_field(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)

        for field in (
            "allowedMutations",
            "checkout",
            "evidence",
            "immediateTask",
            "links",
            "prohibitions",
            "skills",
        ):
            mission = self.read_only_mission()
            del mission[field]
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    CapabilityError,
                    rf"missing required fields: {field}",
                ),
            ):
                self.create(
                    service,
                    self.main_token(),
                    f"reviewer-missing-{field}",
                    "reviewer",
                    mission,
                )

        self.assertEqual([], provider.calls)

    def test_create_child_names_each_provider_owned_mission_field(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)

        for field in (
            "callback",
            "capabilities",
            "childId",
            "owningMainId",
            "role",
            "taskKey",
        ):
            mission = self.read_only_mission()
            mission[field] = "caller-controlled"
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    CapabilityError,
                    rf"provider-owned fields: {field}",
                ),
            ):
                self.create(
                    service,
                    self.main_token(),
                    f"reviewer-reserved-{field}",
                    "reviewer",
                    mission,
                )

        self.assertEqual([], provider.calls)

    def test_create_child_accepts_same_task_after_field_specific_repair(self):
        provider = FakeProvider()
        service = MessagingService(provider, self.secret, clock=lambda: self.now)
        mission = self.read_only_mission()
        del mission["evidence"]

        with self.assertRaisesRegex(
            CapabilityError,
            "missing required fields: evidence",
        ):
            self.create(
                service,
                self.main_token(),
                "recovery-reviewer-687",
                "reviewer",
                mission,
            )

        self.assertEqual([], provider.calls)
        mission["evidence"] = ["review the exact checkout findings-first"]
        result = self.create(
            service,
            self.main_token(),
            "recovery-reviewer-687",
            "reviewer",
            mission,
        )

        self.assertTrue(result["created"])
        self.assertEqual(1, len(provider.calls))

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
            self.read_only_mission(),
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
