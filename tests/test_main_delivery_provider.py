from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evex_agent_messaging.delivery import MainDeliveryRequest  # noqa: E402
from evex_agent_messaging.provider import OpenHandsProvider, ProviderError  # noqa: E402


CONVERSATION_ID = uuid.UUID("3a819d55-f778-5eba-844f-2a20efce78cc")
PROFILE_ID = "44444444-4444-4444-8444-444444444444"


def delivery_request(
    *,
    role: str = "issue",
    allow_create: bool = True,
    recovery_mode: bool = False,
    event: str = "issues",
    action: str = "labeled",
) -> MainDeliveryRequest:
    subissue = role == "subissue"
    repository = "EvexU2/evex-agent-skills" if subissue else "EvexU2/evex-u-workspace"
    return MainDeliveryRequest.parse({
        "schemaVersion": "evex.agent-delivery/1",
        "target": {
            "conversationId": str(CONVERSATION_ID),
            "issueRepository": repository,
            "issueNumber": 297 if subissue else 1067,
            "issueTitle": "Duration fix",
            "deliveryRole": role,
            "parentIssue": 1067 if subissue else None,
            "allowCreate": allow_create,
            "recoveryMode": recovery_mode,
            "source": {
                "repository": repository,
                "branch": "agent/duration-fix" if subissue else "main",
            },
        },
        "event": {
            "schemaVersion": "evex.github-event/1",
            "eventKey": "github:delivery-1:issues:labeled",
            "deliveryGuid": "delivery-1",
            "event": event,
            "action": action,
            "repository": repository,
            "resourceUrl": f"https://github.com/{repository}/issues/297",
            "resourceNumber": 297,
            "actor": "taxaos",
            "installationId": 7,
            "payloadDigest": "a" * 64,
            "observedAt": datetime(2026, 9, 3, tzinfo=timezone.utc).isoformat(),
        },
    })


class FakeTransport:
    def __init__(self, responses: list[dict | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method: str, path: str, body: dict | None) -> dict:
        self.calls.append((method, path, body))
        if path == "/server_info":
            return {"capabilities": ["evex_delivery_admission_v1"]}
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class MainDeliveryProviderTests(unittest.TestCase):
    def provider(self, responses: list[dict | Exception]) -> tuple[OpenHandsProvider, FakeTransport]:
        transport = FakeTransport(responses)
        provider = OpenHandsProvider(
            "http://openhands",
            "api-key",
            transport=transport,
            public_url="http://openhands.local/canvas",
            admission_key=b"a" * 32,
            messaging_secret=b"m" * 32,
            clock=lambda: 100.0,
            sleeper=lambda _delay: None,
        )
        return provider, transport

    @staticmethod
    def identity(provider: OpenHandsProvider, request: MainDeliveryRequest) -> dict:
        tags = {**provider._main_tags(request), "evexagentprofile": PROFILE_ID}
        working_dir = provider._main_workspace(request)["working_dir"]
        tags["evexadmission"] = provider._expected_admission_marker(
            request.target.conversation_id,
            PROFILE_ID,
            working_dir,
            tags,
            capability_ref=provider._main_delivery_capability(request),
        )
        return {
            "id": str(request.target.conversation_id),
            "tags": tags,
            "workspace": {"working_dir": working_dir},
            "launched_agent_profile": {"agent_profile_id": PROFILE_ID},
            "execution_status": "idle",
        }

    def test_missing_non_creatable_target_is_normal_irrelevant_result(self) -> None:
        request = delivery_request(
            allow_create=False, event="issue_comment", action="created"
        )
        provider, transport = self.provider([ProviderError("missing", status=404)])

        result = provider.deliver_main(request)

        self.assertEqual(result, {
            "accepted": False,
            "reason": "target_missing_not_intake_authorized",
        })
        self.assertEqual(len(transport.calls), 1)

    def test_existing_target_is_verified_and_woken_without_bootstrap(self) -> None:
        request = delivery_request()
        provider, transport = self.provider([])
        transport.responses = [self.identity(provider, request), {}]

        result = provider.deliver_main(request)

        self.assertEqual(result["outcome"], "woken")
        event = transport.calls[-1][2]["content"][0]["text"]
        self.assertIn("EVEX_GITHUB_EVENT", event)
        self.assertNotIn("Immediate task", event)

    def test_new_subissue_receives_profile_capability_title_and_bootstrap(self) -> None:
        request = delivery_request(role="subissue")
        provider, transport = self.provider([])
        created = self.identity(provider, request)
        transport.responses = [
            ProviderError("missing", status=404),
            {"active_agent_profile_id": PROFILE_ID, "profiles": [
                {"id": PROFILE_ID, "agent_kind": "acp"},
            ]},
            {},
            {},
            created,
            {},
        ]

        result = provider.deliver_main(request)

        self.assertEqual(result["outcome"], "created")
        create = next(body for method, path, body in transport.calls if method == "POST" and path == "/api/conversations")
        self.assertEqual(create["tags"]["evexdeliveryrole"], "subissue")
        self.assertTrue(create["secrets"]["EVEX_AGENT_MESSAGING_CAPABILITY"]["value"].startswith("evx2_"))
        patch = next(body for method, _path, body in transport.calls if method == "PATCH")
        self.assertEqual(patch["title"], "#1067 / #297 · skills · Subissue · Duration fix")
        event = transport.calls[-1][2]["content"][0]["text"]
        self.assertIn("Subissue Main", event)
        self.assertNotIn("RECOVERY MODE", event)

    def test_issue_title_uses_canonical_root_grammar(self) -> None:
        request = delivery_request()

        self.assertEqual(
            OpenHandsProvider._main_title(request),
            "#1067 · Issue · Duration fix",
        )

    def test_recovery_bootstrap_is_creation_only(self) -> None:
        request = delivery_request(
            role="subissue",
            recovery_mode=True,
            event="issue_comment",
            action="created",
        )
        provider, transport = self.provider([])
        transport.responses = [self.identity(provider, request), {}]

        provider.deliver_main(request)

        text = transport.calls[-1][2]["content"][0]["text"]
        self.assertNotIn("RECOVERY MODE", text)

    def test_missing_recovery_target_gets_recovery_bootstrap_on_creation(self) -> None:
        request = delivery_request(
            role="subissue",
            recovery_mode=True,
            event="issue_comment",
            action="created",
        )
        provider, transport = self.provider([])
        transport.responses = [
            ProviderError("missing", status=404),
            {"active_agent_profile_id": PROFILE_ID, "profiles": [
                {"id": PROFILE_ID, "agent_kind": "acp"},
            ]},
            {},
            {},
            self.identity(provider, request),
            {},
        ]

        result = provider.deliver_main(request)

        self.assertEqual(result["outcome"], "created")
        text = transport.calls[-1][2]["content"][0]["text"]
        self.assertIn("RECOVERY MODE", text)
        self.assertIn("recovery-mode.md", text)

    def test_only_safe_get_is_retried(self) -> None:
        request = delivery_request()
        provider, transport = self.provider([])
        transport.responses = [
            ProviderError("temporary", status=503),
            self.identity(provider, request),
            ProviderError("temporary", status=503),
        ]

        with self.assertRaises(ProviderError) as caught:
            provider.deliver_main(request)

        self.assertEqual(caught.exception.reason, "runtime_unavailable")
        self.assertEqual([call[0] for call in transport.calls], ["GET", "GET", "POST"])

    def test_unknown_create_outcome_reconciles_as_created_without_reposting(self) -> None:
        request = delivery_request()
        provider, transport = self.provider([])
        created = self.identity(provider, request)
        transport.responses = [
            ProviderError("missing", status=404),
            {"active_agent_profile_id": PROFILE_ID, "profiles": [
                {"id": PROFILE_ID, "agent_kind": "acp"},
            ]},
            ProviderError("unknown mutation outcome"),
            created,
            {},
            {},
        ]

        result = provider.deliver_main(request)

        self.assertEqual(result["outcome"], "created")
        creates = [call for call in transport.calls if call[:2] == ("POST", "/api/conversations")]
        self.assertEqual(len(creates), 1)
        self.assertEqual(sum(call[0] == "PATCH" for call in transport.calls), 1)
        self.assertIn("Immediate task", transport.calls[-1][2]["content"][0]["text"])

    def test_create_conflict_repairs_only_compatible_missing_admission(self) -> None:
        request = delivery_request()
        provider, transport = self.provider([])
        repaired = self.identity(provider, request)
        partial = {
            **repaired,
            "tags": {
                key: value
                for key, value in repaired["tags"].items()
                if key != "evexadmission"
            },
        }
        transport.responses = [
            ProviderError("missing", status=404),
            {"active_agent_profile_id": PROFILE_ID, "profiles": [
                {"id": PROFILE_ID, "agent_kind": "acp"},
            ]},
            ProviderError("conflict", status=409),
            partial,
            {},
            repaired,
            {},
            {},
        ]

        result = provider.deliver_main(request)

        self.assertEqual(result["outcome"], "created")
        secret_call = next(
            body for method, path, body in transport.calls
            if method == "POST" and path.endswith("/secrets")
        )
        self.assertEqual(set(secret_call["secrets"]), {
            "EVEX_AGENT_MESSAGING_CAPABILITY",
            "EVEX_DELIVERY_ADMISSION",
        })
        self.assertIn("Immediate task", transport.calls[-1][2]["content"][0]["text"])

    def test_create_conflict_never_repairs_an_invalid_admission(self) -> None:
        request = delivery_request()
        provider, transport = self.provider([])
        invalid = self.identity(provider, request)
        invalid["tags"]["evexadmission"] = "v3:messaging:" + "0" * 64
        transport.responses = [
            ProviderError("missing", status=404),
            {"active_agent_profile_id": PROFILE_ID, "profiles": [
                {"id": PROFILE_ID, "agent_kind": "acp"},
            ]},
            ProviderError("conflict", status=409),
            invalid,
        ]

        with self.assertRaises(ProviderError) as caught:
            provider.deliver_main(request)

        self.assertEqual(caught.exception.reason, "target_identity_mismatch")
        self.assertFalse(any(path.endswith("/secrets") for _method, path, _body in transport.calls))

    def test_busy_and_invalid_statuses_are_distinct(self) -> None:
        request = delivery_request()
        for status, reason in (("running", "target_busy"), ("starting", "target_not_wakeable")):
            provider, _transport = self.provider([])
            identity = self.identity(provider, request)
            identity["execution_status"] = status
            provider.transport.responses = [identity]

            with self.subTest(status=status):
                with self.assertRaises(ProviderError) as caught:
                    provider.deliver_main(request)
                self.assertEqual(caught.exception.reason, reason)
