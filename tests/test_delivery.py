from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evex_agent_messaging.delivery import (  # noqa: E402
    DeliveryContractError,
    MainDeliveryRequest,
)
from evex_agent_messaging.service import MessagingService  # noqa: E402


CONVERSATION_ID = uuid.UUID("3a819d55-f778-5eba-844f-2a20efce78cc")
DELIVERY_SECRET = b"d" * 32


def request(
    *,
    role: str = "issue",
    allow_create: bool = True,
    recovery_mode: bool = False,
    event: str = "issues",
    action: str = "labeled",
) -> dict:
    subissue = role == "subissue"
    repository = "EvexU2/example" if subissue else "EvexU2/evex-u-workspace"
    return {
        "schemaVersion": "evex.agent-delivery/1",
        "target": {
            "environmentId": "dev:lars",
            "intakeLabel": "agent:dev:ready:lars",
            "conversationId": str(CONVERSATION_ID),
            "issueRepository": repository,
            "issueNumber": 42,
            "issueTitle": "Bounded delivery",
            "deliveryRole": role,
            "parentIssue": 40 if subissue else None,
            "allowCreate": allow_create,
            "recoveryMode": recovery_mode,
            "source": {
                "repository": repository,
                "branch": "agent/example" if subissue else "main",
            },
        },
        "event": {
            "schemaVersion": "evex.github-event/1",
            "eventKey": "github:delivery-1:issues:labeled",
            "deliveryGuid": "delivery-1",
            "event": event,
            "action": action,
            "repository": repository,
            "resourceUrl": f"https://github.com/{repository}/issues/42",
            "resourceNumber": 42,
            "actor": "taxaos",
            "installationId": 7,
            "payloadDigest": "a" * 64,
            "observedAt": datetime(2026, 9, 3, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    }


@dataclass
class FakeProvider:
    result: dict = field(default_factory=lambda: {
        "accepted": True,
        "conversationId": str(CONVERSATION_ID),
        "outcome": "created",
    })
    calls: list[MainDeliveryRequest] = field(default_factory=list)

    def deliver_main(self, value: MainDeliveryRequest) -> dict:
        self.calls.append(value)
        return self.result

    def readiness(self) -> bool:
        return True


class DeliveryContractTests(unittest.TestCase):
    def test_valid_issue_request_is_typed(self) -> None:
        value = MainDeliveryRequest.parse(request())

        self.assertEqual(value.target.environment_id, "dev:lars")
        self.assertEqual(value.target.intake_label, "agent:dev:ready:lars")
        self.assertEqual(value.target.conversation_id, CONVERSATION_ID)
        self.assertEqual(value.target.delivery_role, "issue")
        self.assertEqual(value.target.source.repository, "EvexU2/evex-u-workspace")
        self.assertFalse(value.target.recovery_mode)

    def test_valid_recovery_request_is_typed(self) -> None:
        value = MainDeliveryRequest.parse(request(
            role="subissue",
            recovery_mode=True,
            event="issue_comment",
            action="created",
        ))

        self.assertEqual(value.target.delivery_role, "subissue")
        self.assertEqual(value.target.parent_issue, 40)
        self.assertTrue(value.target.recovery_mode)

    def test_unknown_fields_fail_closed(self) -> None:
        value = request()
        value["unexpected"] = True

        with self.assertRaises(DeliveryContractError):
            MainDeliveryRequest.parse(value)

    def test_missing_environment_authority_fails_closed(self) -> None:
        for key in ("environmentId", "intakeLabel"):
            value = request()
            del value["target"][key]
            with self.subTest(key=key), self.assertRaises(DeliveryContractError):
                MainDeliveryRequest.parse(value)

    def test_recovery_requires_create_permission(self) -> None:
        with self.assertRaises(DeliveryContractError):
            MainDeliveryRequest.parse(request(
                allow_create=False,
                recovery_mode=True,
                event="issue_comment",
                action="created",
            ))

    def test_initial_intake_cannot_claim_recovery(self) -> None:
        with self.assertRaises(DeliveryContractError):
            MainDeliveryRequest.parse(request(recovery_mode=True))

    def test_issue_and_subissue_source_bindings_are_exact(self) -> None:
        invalid_issue = request()
        invalid_issue["target"]["source"]["repository"] = "EvexU2/example"
        invalid_subissue = request(role="subissue")
        invalid_subissue["target"]["parentIssue"] = None

        for value in (invalid_issue, invalid_subissue):
            with self.subTest(value=value):
                with self.assertRaises(DeliveryContractError):
                    MainDeliveryRequest.parse(value)

    def test_event_envelope_is_exact_and_bounded(self) -> None:
        invalid = request()
        invalid["event"]["payloadDigest"] = "not-a-digest"

        with self.assertRaises(DeliveryContractError):
            MainDeliveryRequest.parse(invalid)

    def test_full_utf8_github_title_bound_is_accepted(self) -> None:
        value = request()
        value["target"]["issueTitle"] = "🛠" * 256

        parsed = MainDeliveryRequest.parse(value)

        self.assertEqual(parsed.target.issue_title, "🛠" * 256)


class DeliveryServiceTests(unittest.TestCase):
    def test_dedicated_credential_allows_delivery(self) -> None:
        provider = FakeProvider()
        service = MessagingService(provider, b"m" * 32, delivery_secret=DELIVERY_SECRET)

        result = service.deliver_main(DELIVERY_SECRET.decode(), request())

        self.assertEqual(result["outcome"], "created")
        self.assertEqual(len(provider.calls), 1)

    def test_agent_capability_cannot_authorize_delivery(self) -> None:
        provider = FakeProvider()
        service = MessagingService(provider, b"m" * 32, delivery_secret=DELIVERY_SECRET)

        with self.assertRaises(PermissionError):
            service.deliver_main("evx2_not-a-service-credential", request())

        self.assertEqual(provider.calls, [])

    def test_missing_non_creatable_target_is_a_normal_result(self) -> None:
        provider = FakeProvider(result={
            "accepted": False,
            "reason": "target_missing_not_intake_authorized",
        })
        service = MessagingService(provider, b"m" * 32, delivery_secret=DELIVERY_SECRET)

        result = service.deliver_main(
            DELIVERY_SECRET.decode(),
            request(allow_create=False, event="issue_comment", action="created"),
        )

        self.assertEqual(result, {
            "accepted": False,
            "reason": "target_missing_not_intake_authorized",
        })
        self.assertEqual(len(provider.calls), 1)

    def test_malformed_request_never_reaches_provider(self) -> None:
        provider = FakeProvider()
        service = MessagingService(provider, b"m" * 32, delivery_secret=DELIVERY_SECRET)
        invalid = request()
        invalid["target"]["deliveryRole"] = "provider-specific"

        with self.assertRaises(DeliveryContractError):
            service.deliver_main(DELIVERY_SECRET.decode(), invalid)

        self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
