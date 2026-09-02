from __future__ import annotations

import json
import hashlib
import hmac
import io
import copy
import hashlib
import hmac
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
from evex_agent_messaging.capability import (  # noqa: E402
    CapabilityError, capability_token, main_capability_token, project_capability_token,
)
from evex_agent_messaging.service import MessagingService  # noqa: E402
from evex_agent_messaging.mcp_server import McpServer  # noqa: E402

ACP_PROFILE_ID = "44444444-4444-4444-8444-444444444444"
OPENAI_PROFILE_ID = "55555555-5555-4555-8555-555555555555"
FOREIGN_PROFILE_ID = "66666666-6666-4666-8666-666666666666"


def canonical_profile_id(value):
    return {
        "acp": ACP_PROFILE_ID,
        "openai-production": OPENAI_PROFILE_ID,
        "foreign": FOREIGN_PROFILE_ID,
    }.get(value, value)


def configured_provider(*args, **kwargs):
    return OpenHandsProvider(
        *args,
        environment_id="dev:lars",
        intake_label="agent:dev:ready:lars",
        **kwargs,
    )


class FakeTransport:
    def __init__(self, responses, *, server_capabilities=None):
        self.responses, self.calls = list(responses), []
        self.server_info_requests = 0
        self.server_capabilities = (
            ["evex_delivery_admission_v1"]
            if server_capabilities is None
            else list(server_capabilities)
        )

    def __call__(self, method, path, body):
        if method == "GET" and path == "/server_info":
            self.server_info_requests += 1
            return {"capabilities": self.server_capabilities}
        self.calls.append((method, path, body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def discussion(conversation_id, role, **tags):
    return {
        "id": str(conversation_id),
        "tags": {
            "project": "evex-u",
            "evexdeliveryrole": role,
            "evexenvironment": "dev:lars",
            "evexintakelabel": "agent:dev:ready:lars",
            **tags,
        },
    }


def profiles(active="acp", kind="acp"):
    active = canonical_profile_id(active)
    return {
        "active_agent_profile_id": active,
        "profiles": [{"id": active, "agent_kind": kind}],
    }


def spec_discussion(conversation_id, parent_id, *, legacy=False, profile="acp"):
    profile = canonical_profile_id(profile)
    value = discussion(
        conversation_id,
        "spec",
        evexrole="role-child",
        evextask="issue-40-spec",
        evexissue="EvexU2/evex-u-workspace#40",
        evexparent=str(parent_id),
        evexrepository="EvexU2/evex-u-workspace",
        evexbranch="spec/issue-40",
        evexreasoning="high",
        **({} if legacy else {
            "evexskills": "evex-delivery-spec",
            "evexagentprofile": profile,
        }),
    )
    value["workspace"] = {"working_dir": f"/tmp/spec-{conversation_id}"}
    value["launched_agent_profile"] = {"agent_profile_id": profile}
    if legacy:
        value["tags"]["evexmodel"] = "gpt-5.6-sol"
        value["current_model_id"] = "gpt-5.6-sol"
    else:
        descriptor = {
            "conversation_id": str(conversation_id),
            "parent_conversation_id": "",
            "profile_id": profile,
            "working_dir": value["workspace"]["working_dir"],
            "worktree": False,
            "tags": value["tags"],
        }
        canonical = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
        signature = hmac.new(
            b"admission-key" * 4,
            f"evex-delivery-admission:v1\0messaging\0{canonical}".encode(),
            hashlib.sha256,
        ).hexdigest()
        token = f"v1:messaging:{signature}"
        value["tags"]["evexadmission"] = (
            "v1:messaging:" + hashlib.sha256(token.encode()).hexdigest()
        )
    return value


def goal_event(objective):
    return {
        "kind": "ConversationStateUpdateEvent",
        "key": "goal",
        "value": {"objective": objective, "status": "running"},
    }


class OpenHandsProviderTest(unittest.TestCase):
    def setUp(self):
        self.parent, self.child, self.spec = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def provider(self, responses, *, server_capabilities=None):
        transport = FakeTransport(
            responses, server_capabilities=server_capabilities
        )
        return configured_provider(
            "http://openhands",
            "key",
            transport=transport,
            public_url="http://openhands.local/canvas",
            admission_key=b"admission-key" * 4,
        ), transport

    def test_create_spec_chat_reuses_exact_parent_issue_and_fixed_role(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        created = spec_discussion(self.spec, self.parent)
        objective = OpenHandsProvider._spec_goal("EvexU2/evex-u-workspace#40")
        provider, transport = self.provider([
            parent,
            ProviderError("missing", status=404),
            profiles(),
            {},
            created,
            {},
            {},
            {"items": []},
            {},
            {"items": [goal_event(objective)]},
        ])
        provider.workspace_root = "/tmp"
        provider.admission_key += b"\n"
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
                return_value=(parent_checkout, "a" * 40),
            ),
            patch.object(
                provider, "_ensure_checkout", return_value="a" * 40
            ) as ensure,
            patch.object(
                provider, "_validate_existing_checkout", return_value="a" * 40
            ),
        ):
            result = provider.create_spec_chat(
                self.parent,
                self.spec,
                "evx2_spec",
            )

        self.assertTrue(result["created"])
        self.assertEqual(result["checkout"], checkout)
        self.assertEqual(result["conversationUrl"], f"http://openhands.local/canvas/conversations/{self.spec}")
        ensure.assert_called_once_with(self.spec, checkout, parent_checkout)
        create = next(call for call in transport.calls if call[:2] == ("POST", "/api/conversations"))
        self.assertEqual(create[2]["tags"]["evexdeliveryrole"], "spec")
        self.assertEqual(create[2]["tags"]["evexskills"], "evex-delivery-spec")
        self.assertEqual(create[2]["tags"]["evexagentprofile"], ACP_PROFILE_ID)
        self.assertEqual(create[2]["tags"]["evexenvironment"], "dev:lars")
        self.assertEqual(create[2]["tags"]["evexintakelabel"], "agent:dev:ready:lars")
        self.assertEqual(create[2]["secrets"]["EVEX_ENVIRONMENT_ID"], {
            "kind": "StaticSecret", "value": "dev:lars",
        })
        self.assertEqual(create[2]["secrets"]["EVEX_INTAKE_LABEL"], {
            "kind": "StaticSecret", "value": "agent:dev:ready:lars",
        })
        self.assertNotIn("evexlocale", create[2]["tags"])
        self.assertNotIn("evexbasehead", create[2]["tags"])
        self.assertNotIn("language", create[2])
        self.assertNotIn("agent_launch_additions", create[2])
        self.assertNotIn("EVEX_AGENT_ROLE", create[2]["secrets"])
        self.assertNotIn("EVEX_REASONING_EFFORT", create[2]["secrets"])
        self.assertNotIn("mcp_config", create[2])
        self.assertNotIn("evexmodel", create[2]["tags"])
        descriptor = {
            "conversation_id": str(self.spec),
            "parent_conversation_id": "",
            "profile_id": ACP_PROFILE_ID,
            "working_dir": create[2]["workspace"]["working_dir"],
            "worktree": False,
            "tags": create[2]["tags"],
        }
        canonical = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
        signature = hmac.new(
            b"admission-key" * 4,
            f"evex-delivery-admission:v1\0messaging\0{canonical}".encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(
            create[2]["secrets"]["EVEX_DELIVERY_ADMISSION"],
            {"kind": "StaticSecret", "value": f"v1:messaging:{signature}"},
        )
        self.assertFalse(create[2]["worktree"])
        self.assertFalse(any("switch_acp_model" in path for _, path, _ in transport.calls))
        self.assertIn((
            "POST", f"/api/conversations/{self.spec}/goal",
            {"objective": objective, "max_iterations": 100}
        ), transport.calls)
        prompt_call = next(
            call for call in transport.calls
            if call[:2] == ("POST", f"/api/conversations/{self.spec}/events")
        )
        self.assertIs(prompt_call[2]["run"], False)
        self.assertFalse(any(path == "/api/conversations/search" for _, path, _ in transport.calls))

    def test_reused_spec_chat_receives_the_current_durable_capability(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        existing = spec_discussion(self.spec, self.parent)
        existing["language"] = "fr-FR"
        objective = OpenHandsProvider._spec_goal("EvexU2/evex-u-workspace#40")
        provider, transport = self.provider([
            parent, existing, {}, {"items": [goal_event(objective)]}
        ])
        provider.workspace_root = "/tmp"
        with (
            patch.object(
                provider,
                "_validated_parent_checkout",
                return_value=(
                    Path("/tmp/issue-40-source/evex-u-workspace"),
                    "a" * 40,
                ),
            ),
            patch.object(
                provider, "_validate_existing_checkout", return_value="b" * 40
            ),
            patch.object(provider, "_has_initial_prompt", return_value=True),
        ):
            result = provider.create_spec_chat(
                self.parent,
                self.spec,
                "evx2_current",
            )

        self.assertFalse(result["created"])
        self.assertEqual(existing["language"], "fr-FR")
        for method, path, body in transport.calls:
            if method in {"POST", "PATCH"} and isinstance(body, dict):
                with self.subTest(method=method, path=path):
                    self.assertNotIn("language", body)
                    self.assertNotIn("agent_launch_additions", body)
        self.assertEqual(result["checkout"], {
            "repository": "EvexU2/evex-u-workspace",
            "branch": "spec/issue-40",
            "headSha": "b" * 40,
        })
        self.assertIn((
            "POST",
            f"/api/conversations/{self.spec}/secrets",
            {"secrets": {"EVEX_AGENT_MESSAGING_CAPABILITY": {
                "kind": "StaticSecret", "value": "evx2_current",
            }}},
        ), transport.calls)

    def test_create_spec_chat_reconciles_an_ambiguous_create_response(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        created = spec_discussion(self.spec, self.parent)
        objective = OpenHandsProvider._spec_goal("EvexU2/evex-u-workspace#40")
        provider, transport = self.provider([
            parent,
            ProviderError("missing", status=404),
            profiles(),
            ProviderError("connection closed"),
            created,
            {},
            {},
            {"items": []},
            {},
            {"items": [goal_event(objective)]},
        ])
        provider.workspace_root = "/tmp"
        with (
            patch.object(
                provider,
                "_validated_parent_checkout",
                return_value=(
                    Path("/tmp/issue-40-source/evex-u-workspace"),
                    "a" * 40,
                ),
            ),
            patch.object(
                provider, "_ensure_checkout", return_value="a" * 40
            ),
            patch.object(
                provider, "_validate_existing_checkout", return_value="a" * 40
            ),
        ):
            result = provider.create_spec_chat(
                self.parent,
                self.spec,
                "evx2_spec",
            )

        self.assertTrue(result["created"])
        self.assertIn(("GET", f"/api/conversations/{self.spec}", None), transport.calls)

    def test_create_spec_chat_reconciles_an_ambiguous_initial_prompt(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        existing = spec_discussion(self.spec, self.parent)
        expected_prompt = (
            "EVEX_SPEC_CHAT\n"
            "Issue: https://github.com/EvexU2/evex-u-workspace/issues/40\n"
            f"Parent Main: {self.parent}\n"
            "Your task now: run the interactive Spec Chat for this Issue using the admitted "
            "EVEX Spec skills. Start by reading the current Issue and living Specification."
        )
        prompt_event = {
            "kind": "MessageEvent",
            "source": "user",
            "llm_message": {"content": [{"type": "text", "text": expected_prompt}]},
        }
        provider, transport = self.provider([
            parent,
            existing,
            {},
            {"items": []},
            ProviderError("connection closed"),
            {"items": [prompt_event]},
            {"items": [goal_event(OpenHandsProvider._spec_goal(
                "EvexU2/evex-u-workspace#40"
            ))]},
        ])
        provider.workspace_root = "/tmp"
        with (
            patch.object(
                provider,
                "_validated_parent_checkout",
                return_value=(
                    Path("/tmp/issue-40-source/evex-u-workspace"),
                    "a" * 40,
                ),
            ),
            patch.object(
                provider, "_validate_existing_checkout", return_value="a" * 40
            ),
        ):
            result = provider.create_spec_chat(
                self.parent,
                self.spec,
                "evx2_spec",
            )

        self.assertFalse(result["created"])
        self.assertIn((
            "GET",
            f"/api/conversations/{self.spec}/events/search"
            "?limit=1&source=user&sort_order=TIMESTAMP",
            None,
        ), transport.calls)

    def test_native_openhands_profile_uses_the_same_spec_contract(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        created = spec_discussion(
            self.spec, self.parent, profile="openai-production"
        )
        objective = OpenHandsProvider._spec_goal("EvexU2/evex-u-workspace#40")
        provider, transport = self.provider([
            parent,
            ProviderError("missing", status=404),
            profiles("openai-production", "openhands"),
            {},
            created,
            {},
            {},
            {"items": []},
            {},
            {"items": [goal_event(objective)]},
        ])
        provider.workspace_root = "/tmp"
        with (
            patch.object(
                provider,
                "_validated_parent_checkout",
                return_value=(
                    Path("/tmp/issue-40-source/evex-u-workspace"), "a" * 40,
                ),
            ),
            patch.object(provider, "_ensure_checkout", return_value="a" * 40),
            patch.object(
                provider, "_validate_existing_checkout", return_value="a" * 40
            ),
        ):
            provider.create_spec_chat(self.parent, self.spec, "evx2_spec")

        payload = next(
            call[2]
            for call in transport.calls
            if call[:2] == ("POST", "/api/conversations")
        )
        self.assertEqual(payload["agent_profile_id"], OPENAI_PROFILE_ID)
        self.assertEqual(
            payload["tags"]["evexagentprofile"], OPENAI_PROFILE_ID
        )
        self.assertFalse(any("switch_acp_model" in path for _, path, _ in transport.calls))

    def test_unsupported_profile_fails_before_conversation_creation(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        provider, transport = self.provider([
            parent,
            ProviderError("missing", status=404),
            profiles("foreign", "other"),
        ])
        provider.workspace_root = "/tmp"
        with (
            patch.object(
                provider,
                "_validated_parent_checkout",
                return_value=(
                    Path("/tmp/issue-40-source/evex-u-workspace"), "a" * 40,
                ),
            ),
            patch.object(provider, "_ensure_checkout", return_value="a" * 40),
        ):
            with self.assertRaisesRegex(ProviderError, "supported active Agent Profile"):
                provider.create_spec_chat(self.parent, self.spec, "evx2_spec")

        self.assertFalse(
            any(call[:2] == ("POST", "/api/conversations") for call in transport.calls)
        )

    def test_non_uuid_profile_fails_readiness_and_creation_before_mutation(self):
        invalid_profiles = profiles("not-a-uuid", "acp")
        provider, _ = self.provider([invalid_profiles])
        self.assertFalse(provider.readiness())

        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        provider, transport = self.provider([
            parent,
            ProviderError("missing", status=404),
            invalid_profiles,
        ])
        provider.workspace_root = "/tmp"
        with (
            patch.object(
                provider,
                "_validated_parent_checkout",
                return_value=(
                    Path("/tmp/issue-40-source/evex-u-workspace"), "a" * 40,
                ),
            ),
            patch.object(provider, "_ensure_checkout", return_value="a" * 40),
        ):
            with self.assertRaisesRegex(ProviderError, "supported active Agent Profile"):
                provider.create_spec_chat(self.parent, self.spec, "evx2_spec")
        self.assertFalse(any(
            call[:2] == ("POST", "/api/conversations") for call in transport.calls
        ))

    def test_legacy_spec_chat_is_reused_without_metadata_migration_or_model_switch(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        legacy = spec_discussion(self.spec, self.parent, legacy=True)
        provider, transport = self.provider([parent, legacy, legacy, {}])
        provider.workspace_root = "/tmp"
        with (
            patch.object(
                provider,
                "_validated_parent_checkout",
                return_value=(
                    Path("/tmp/issue-40-source/evex-u-workspace"), "a" * 40,
                ),
            ),
            patch.object(
                provider, "_validate_existing_checkout", return_value="b" * 40
            ),
            patch.object(provider, "_has_initial_prompt", return_value=True),
        ):
            result = provider.create_spec_chat(
                self.parent, self.spec, "evx2_current"
            )

        self.assertFalse(result["created"])
        self.assertFalse(any("switch_acp_model" in path for _, path, _ in transport.calls))
        self.assertFalse(any(path.endswith("/goal") for _, path, _ in transport.calls))

    def test_new_spec_profile_binding_is_verified_on_reuse(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        mismatched = spec_discussion(self.spec, self.parent)
        mismatched["launched_agent_profile"] = {
            "agent_profile_id": OPENAI_PROFILE_ID
        }
        provider, _ = self.provider([parent, mismatched, mismatched])
        provider.workspace_root = "/tmp"
        with (
            patch.object(
                provider,
                "_validated_parent_checkout",
                return_value=(
                    Path("/tmp/issue-40-source/evex-u-workspace"), "a" * 40,
                ),
            ),
        ):
            with self.assertRaisesRegex(ProviderError, "does not match authority"):
                provider.create_spec_chat(self.parent, self.spec, "evx2_current")

    def test_new_spec_requires_server_owned_messaging_admission_before_mutation(self):
        for marker in (
            "v1:gateway:" + "a" * 64,
            "v1:messaging:bad",
            "v1:messaging:" + "a" * 64,
        ):
            with self.subTest(marker=marker):
                parent = discussion(
                    self.parent,
                    "parent-main",
                    evexissue="EvexU2/evex-u-workspace#40",
                    evexsourcerepository="EvexU2/evex-u-workspace",
                    evexsourcebranch="main",
                )
                parent["workspace"] = {
                    "working_dir": "/tmp/issue-40-source/evex-u-workspace"
                }
                damaged = spec_discussion(self.spec, self.parent)
                damaged["tags"]["evexadmission"] = marker
                provider, transport = self.provider([parent, damaged, damaged])
                provider.workspace_root = "/tmp"
                with patch.object(
                    provider,
                    "_validated_parent_checkout",
                    return_value=(
                        Path("/tmp/issue-40-source/evex-u-workspace"), "a" * 40,
                    ),
                ):
                    with self.assertRaisesRegex(
                        ProviderError, "does not match authority"
                    ):
                        provider.create_spec_chat(
                            self.parent, self.spec, "evx2_current"
                        )
                self.assertFalse(any(
                    method == "POST" for method, _, _ in transport.calls
                ))

    def test_current_spec_identity_is_migrated_with_descriptor_bound_hmac(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        current = spec_discussion(self.spec, self.parent)
        current["tags"].pop("evexadmission")
        migrated = spec_discussion(self.spec, self.parent)
        provider, transport = self.provider([
            parent,
            current,
            {},
            migrated,
            {},
        ])
        provider.workspace_root = "/tmp"
        with (
            patch.object(
                provider,
                "_validated_parent_checkout",
                return_value=(
                    Path("/tmp/issue-40-source/evex-u-workspace"), "a" * 40,
                ),
            ),
            patch.object(
                provider, "_validate_existing_checkout", return_value="a" * 40
            ),
            patch.object(provider, "_has_initial_prompt", return_value=True),
            patch.object(provider, "_ensure_spec_goal"),
        ):
            result = provider.create_spec_chat(
                self.parent, self.spec, "evx2_current"
            )

        self.assertFalse(result["created"])
        migration = next(
            body["tags"]["evexadmissionrequest"]
            for method, _path, body in transport.calls
            if method == "PATCH" and body and "tags" in body
        )
        self.assertRegex(migration, r"^v1:messaging:[0-9a-f]{64}$")
        self.assertEqual(transport.server_info_requests, 1)

    def test_spec_creation_requires_runtime_admission_capability(self):
        provider, transport = self.provider([], server_capabilities=[])

        with self.assertRaisesRegex(ProviderError, "capability is unavailable"):
            provider._require_admission_capability()

        self.assertEqual(transport.server_info_requests, 1)
        self.assertEqual(transport.calls, [])

    def test_legacy_spec_without_exact_old_model_markers_is_rejected(self):
        for marker in ("tag-missing", "tag-mismatch", "current-missing", "current-mismatch"):
            with self.subTest(marker=marker):
                parent = discussion(
                    self.parent,
                    "parent-main",
                    evexissue="EvexU2/evex-u-workspace#40",
                    evexsourcerepository="EvexU2/evex-u-workspace",
                    evexsourcebranch="main",
                )
                parent["workspace"] = {
                    "working_dir": "/tmp/issue-40-source/evex-u-workspace"
                }
                legacy = spec_discussion(self.spec, self.parent, legacy=True)
                if marker == "tag-missing":
                    del legacy["tags"]["evexmodel"]
                elif marker == "tag-mismatch":
                    legacy["tags"]["evexmodel"] = "other"
                elif marker == "current-missing":
                    del legacy["current_model_id"]
                else:
                    legacy["current_model_id"] = "other"
                provider, transport = self.provider([parent, legacy, legacy])
                provider.workspace_root = "/tmp"
                with patch.object(
                    provider,
                    "_validated_parent_checkout",
                    return_value=(
                        Path("/tmp/issue-40-source/evex-u-workspace"), "a" * 40,
                    ),
                ):
                    with self.assertRaisesRegex(
                        ProviderError, "does not match authority"
                    ):
                        provider.create_spec_chat(
                            self.parent, self.spec, "evx2_current"
                        )
                self.assertFalse(any(
                    method == "POST" for method, _, _ in transport.calls
                ))

    def test_partial_new_spec_metadata_cannot_downgrade_to_legacy(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="main",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        partial = spec_discussion(self.spec, self.parent)
        del partial["tags"]["evexagentprofile"]
        provider, _ = self.provider([parent, partial, partial])
        provider.workspace_root = "/tmp"
        with patch.object(
            provider,
            "_validated_parent_checkout",
            return_value=(
                Path("/tmp/issue-40-source/evex-u-workspace"), "a" * 40,
            ),
        ):
            with self.assertRaisesRegex(ProviderError, "does not match authority"):
                provider.create_spec_chat(self.parent, self.spec, "evx2_current")

    def test_ambiguous_goal_start_is_reconciled_from_durable_goal_event(self):
        objective = OpenHandsProvider._spec_goal("EvexU2/evex-u-workspace#40")
        provider, transport = self.provider([
            {"items": []},
            ProviderError("connection closed"),
            {"items": [goal_event(objective)]},
        ])

        provider._ensure_spec_goal(self.spec, objective)

        self.assertEqual(
            [call[:2] for call in transport.calls],
            [
                ("GET", f"/api/conversations/{self.spec}/events/search?limit=100&kind=ConversationStateUpdateEvent&sort_order=TIMESTAMP_DESC"),
                ("POST", f"/api/conversations/{self.spec}/goal"),
                ("GET", f"/api/conversations/{self.spec}/events/search?limit=100&kind=ConversationStateUpdateEvent&sort_order=TIMESTAMP_DESC"),
            ],
        )

    def test_interrupted_spec_goal_resumes_on_idempotent_reuse(self):
        objective = OpenHandsProvider._spec_goal("EvexU2/evex-u-workspace#40")
        interrupted = goal_event(objective)
        interrupted["value"]["status"] = "interrupted"
        running = goal_event(objective)
        provider, transport = self.provider([
            {"items": [interrupted]},
            {},
            {"items": [running]},
        ])

        provider._ensure_spec_goal(self.spec, objective)

        self.assertEqual(
            transport.calls,
            [
                (
                    "GET",
                    f"/api/conversations/{self.spec}/events/search?limit=100&kind=ConversationStateUpdateEvent&sort_order=TIMESTAMP_DESC",
                    None,
                ),
                (
                    "POST",
                    f"/api/conversations/{self.spec}/goal/resume",
                    {},
                ),
                (
                    "GET",
                    f"/api/conversations/{self.spec}/events/search?limit=100&kind=ConversationStateUpdateEvent&sort_order=TIMESTAMP_DESC",
                    None,
                ),
            ],
        )

    def test_ambiguous_spec_goal_resume_reconciles_running_state(self):
        objective = OpenHandsProvider._spec_goal("EvexU2/evex-u-workspace#40")
        interrupted = goal_event(objective)
        interrupted["value"]["status"] = "interrupted"
        running = goal_event(objective)
        provider, transport = self.provider([
            {"items": [interrupted]},
            ProviderError("connection closed"),
            {"items": [running]},
        ])

        provider._ensure_spec_goal(self.spec, objective)

        self.assertEqual(len(transport.calls), 3)

    def test_goal_lookup_paginates_before_starting_a_duplicate_round(self):
        objective = OpenHandsProvider._spec_goal("EvexU2/evex-u-workspace#40")
        provider, transport = self.provider([
            {"items": [], "next_page_id": "older-goals"},
            {"items": [goal_event(objective)]},
        ])

        provider._ensure_spec_goal(self.spec, objective)

        self.assertEqual(len(transport.calls), 2)
        self.assertIn("page_id=older-goals", transport.calls[1][1])
        self.assertFalse(any(method == "POST" for method, _, _ in transport.calls))

    def test_newer_different_goal_boundary_fails_closed(self):
        expected = OpenHandsProvider._spec_goal("EvexU2/evex-u-workspace#40")
        provider, transport = self.provider([
            {"items": [goal_event("different authority"), goal_event(expected)]},
        ])

        with self.assertRaisesRegex(ProviderError, "goal authority does not match"):
            provider._ensure_spec_goal(self.spec, expected)

        self.assertEqual(len(transport.calls), 1)
        self.assertFalse(any(method == "POST" for method, _, _ in transport.calls))

    def test_capped_goal_does_not_start_a_second_round(self):
        objective = OpenHandsProvider._spec_goal("EvexU2/evex-u-workspace#40")
        capped = goal_event(objective)
        capped["value"]["status"] = "capped"
        provider, transport = self.provider([{"items": [capped]}])

        with self.assertRaisesRegex(ProviderError, "terminal or invalid"):
            provider._ensure_spec_goal(self.spec, objective)

        self.assertEqual(len(transport.calls), 1)
        self.assertFalse(any(method == "POST" for method, _, _ in transport.calls))

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
            provider = configured_provider(
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
            }
            self.assertEqual(
                provider._validated_parent_checkout(
                    parent, parent_tags, "40"
                ),
                (source.resolve(), head),
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

            (source / "uncommitted.md").write_text("dirty\n")
            with self.assertRaisesRegex(ProviderError, "must be clean"):
                provider._validated_parent_checkout(parent, parent_tags, "40")

    def test_deleted_spec_conversation_reuses_its_clean_advanced_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "delivery" / "issue-40-source" / "evex-u-workspace"
            source.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(source), "remote", "add", "origin", "https://github.com/EvexU2/evex-u-workspace.git"], check=True)
            (source / "spec.md").write_text("base\n")
            subprocess.run(["git", "-C", str(source), "add", "spec.md"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "base"], check=True)
            parent_head = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            provider = configured_provider(
                "http://openhands",
                "key",
                public_url="http://openhands.local/canvas",
                workspace_root=str(root / "delivery"),
            )
            checkout = {
                "repository": "EvexU2/evex-u-workspace",
                "branch": "spec/issue-40",
                "headSha": parent_head,
            }
            provider._ensure_checkout(self.spec, checkout, source)
            spec_path = provider._checkout_path(self.spec)
            subprocess.run(["git", "-C", str(spec_path), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(spec_path), "config", "user.name", "Test"], check=True)
            (spec_path / "spec.md").write_text("reviewed candidate\n")
            subprocess.run(["git", "-C", str(spec_path), "add", "spec.md"], check=True)
            subprocess.run(["git", "-C", str(spec_path), "commit", "-q", "-m", "spec candidate"], check=True)
            spec_head = subprocess.run(
                ["git", "-C", str(spec_path), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            self.assertNotEqual(spec_head, parent_head)
            self.assertEqual(
                provider._ensure_checkout(self.spec, checkout, source),
                spec_head,
            )

    def test_parent_checkout_identity_mismatch_fails_before_spec_mutation(self):
        parent = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#40",
            evexsourcerepository="EvexU2/evex-u-workspace",
            evexsourcebranch="develop",
        )
        parent["workspace"] = {
            "working_dir": "/tmp/issue-40-source/evex-u-workspace"
        }
        provider, transport = self.provider([parent])
        with self.assertRaisesRegex(ProviderError, "Parent Main checkout authority"):
            provider.create_spec_chat(self.parent, self.spec, "evx1_spec")

        self.assertEqual(len(transport.calls), 1)

    def test_spec_checkout_provisioning_has_no_shared_mirror_or_worktree_path(self):
        source = (ROOT / "src/evex_agent_messaging/provider.py").read_text()

        self.assertNotIn(' / "mirrors" / ', source)
        self.assertNotIn('"worktree", "add"', source)

    def test_child_and_spec_can_target_only_their_bound_parent(self):
        for role in ("deputy", "spec"):
            sender_role = "child-main" if role == "deputy" else "spec"
            issue = "EvexU2/evex-u-workspace#40"
            sender = discussion(
                self.child,
                sender_role,
                evexparentissue=issue,
                evexparent=str(self.parent),
            )
            provider, transport = self.provider([
                discussion(self.parent, "parent-main", evexissue=issue), sender,
            ])
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

    def test_send_message_projects_visible_summary_and_hidden_canonical_evidence(self):
        provider, transport = self.provider([{}])
        message = {
            "humanSummary": "Review passed; no action is needed.",
            "aiEvidence": {"outcome": "passed", "evidence": ["tests: PASS"], "findings": [], "nextBoundary": "merge"},
        }
        result = provider.send_message(self.parent, self.child, "result-1", message)
        self.assertEqual(result, {"accepted": True, "messageKey": "result-1"})
        self.assertEqual(len(transport.calls), 1)
        method, path, body = transport.calls[0]
        self.assertEqual((method, path), ("POST", f"/api/conversations/{self.child}/events"))
        projection = body["content"][0]["text"]
        self.assertTrue(projection.startswith(message["humanSummary"] + "\n<!-- evex-agent-message:v1 "))
        self.assertTrue(projection.endswith(" -->"))
        envelope = json.loads(projection.removeprefix(message["humanSummary"] + "\n<!-- evex-agent-message:v1 ").removesuffix(" -->"))
        self.assertEqual(envelope, {"aiEvidence": message["aiEvidence"], "humanSummary": message["humanSummary"], "messageKey": "result-1", "senderId": str(self.parent)})

    def test_configured_credential_is_rejected_before_provider_mutation(self):
        transport = FakeTransport([])
        provider = configured_provider(
            "http://openhands", "configured-secret", transport=transport,
        )

        with self.assertRaisesRegex(ProviderError, "configured credential"):
            provider.send_message(
                self.parent,
                self.child,
                "key",
                {"humanSummary": "Delivery passed", "aiEvidence": {"outcome": "configured-secret", "evidence": [], "findings": [], "nextBoundary": "review"}},
            )

        self.assertEqual(transport.calls, [])

    def test_invalid_identity_and_readiness_fail_closed(self):
        provider, _ = self.provider([{"id": "bad", "tags": {}}])
        with self.assertRaises(ProviderError):
            provider.target_allowed(self.parent, self.child, "main", self.parent)
        provider, _ = self.provider([profiles()])
        self.assertTrue(provider.readiness())


class ProjectAdmissionTest(unittest.TestCase):
    """Consumer fixtures only: the host does not yet produce this admission."""

    def setUp(self):
        self.chat = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.parent = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        self.secret = b"messaging-test-secret"
        self.project_id = "native-project-node-id"
        self.project = {
            "id": self.project_id, "accountablePmId": "native-pm-node-id",
            "nominatedChatId": str(self.chat), "state": "open",
            "accountability": "unique", "subjectAccess": "allowed",
        }
        self.root = {
            "id": "native-workspace-issue-node-id", "repository": "EvexU2/evex-u-workspace",
            "number": 42, "parentMainId": str(self.parent),
            "accountableProjectId": self.project_id, "accountablePmId": "native-pm-node-id",
            "pmAssigned": True, "membershipProjectId": self.project_id,
            "state": "eligible", "projectChatAccess": "allowed",
        }
        self.message = {"humanSummary": "New project context is available.", "aiEvidence": {
            "outcome": "context", "evidence": ["original-decision-reference"], "findings": [],
            "nextBoundary": "Parent verifies original authority before action",
        }}

    def conversation(self, role):
        identity = self.chat if role == "project" else self.parent
        return {
            "id": str(identity),
            "tags": {
                "evexenvironment": "dev:lars",
                "evexintakelabel": "agent:dev:ready:lars",
            },
            "evexProjectAdmission": {
            "schemaVersion": 1, "conversationId": str(identity), "role": role,
            "lifecycle": "eligible", "project": copy.deepcopy(self.project),
            "root": None if role == "project" else copy.deepcopy(self.root),
        }}

    def service(self, responses):
        transport = FakeTransport(responses)
        provider = configured_provider("http://openhands", "private-service-key", transport=transport)
        return MessagingService(provider, self.secret), transport

    def token(self, direction):
        return (project_capability_token(self.secret, self.chat, self.project_id)
                if direction == "project" else main_capability_token(self.secret, self.parent))

    def test_project_both_directions_read_exact_endpoints_and_preserve_envelope(self):
        for direction in ("project", "parent-main"):
            sender = self.chat if direction == "project" else self.parent
            target = self.parent if direction == "project" else self.chat
            sender_value = self.conversation(direction)
            target_value = self.conversation("parent-main" if direction == "project" else "project")
            # No tags, user-selected role, generic finished-turn state, or cached facts are used.
            sender_value["status"] = target_value["status"] = "finished"
            service, transport = self.service([target_value, sender_value, {}] * 2)
            for key in ("first-fact", "later-fact"):
                with self.subTest(direction=direction, key=key):
                    self.assertEqual(service.send_message(self.token(direction), target, key, self.message),
                                     {"accepted": True, "messageKey": key})
            self.assertEqual([(method, path) for method, path, _ in transport.calls], [
                ("GET", f"/api/conversations/{target}"), ("GET", f"/api/conversations/{sender}"),
                ("POST", f"/api/conversations/{target}/events"),
            ] * 2)
            body = transport.calls[-1][2]
            self.assertTrue(body["run"])
            projected = body["content"][0]["text"]
            envelope = json.loads(projected.split("<!-- evex-agent-message:v1 ", 1)[1].removesuffix(" -->"))
            self.assertEqual(envelope, {**self.message, "messageKey": "later-fact", "senderId": str(sender)})

    def test_project_both_directions_reject_missing_or_foreign_peer_environment(self):
        for direction in ("project", "parent-main"):
            opposite = "parent-main" if direction == "project" else "project"
            target_id = self.parent if direction == "project" else self.chat
            for bad_side in ("sender", "target"):
                for bad_tags in ({}, {
                    "evexenvironment": "dev:else",
                    "evexintakelabel": "agent:dev:ready:else",
                }):
                    with self.subTest(
                        direction=direction, bad_side=bad_side, bad_tags=bad_tags,
                    ):
                        sender = self.conversation(direction)
                        target = self.conversation(opposite)
                        (sender if bad_side == "sender" else target)["tags"] = bad_tags
                        service, transport = self.service([target, sender])
                        with self.assertRaisesRegex(ProviderError, "environment"):
                            service.send_message(
                                self.token(direction), target_id, "denied", self.message,
                            )
                        self.assertEqual(
                            [method for method, _, _ in transport.calls], ["GET", "GET"]
                        )

    def test_project_message_key_credential_review_regression(self):
        for direction in ("project", "parent-main"):
            for key in ("private-service-key", "reference_private-service-key"):
                with self.subTest(direction=direction, key=key):
                    opposite = "parent-main" if direction == "project" else "project"
                    service, transport = self.service([
                        self.conversation(opposite), self.conversation(direction), {},
                    ])
                    target = self.parent if direction == "project" else self.chat
                    with self.assertRaises(ProviderError) as error:
                        service.send_message(self.token(direction), target, key, self.message)
                    self.assertEqual([method for method, _, _ in transport.calls], ["GET", "GET"])
                    self.assertLess(len(str(error.exception)), 200)
                    self.assertNotIn("private-service-key", str(error.exception))

    def test_project_denial_matrix_in_both_directions_has_zero_mutations(self):
        mutations = [
            ((), None), (("schemaVersion",), 2), (("schemaVersion",), True),
            (("unexpected",), "extra"), (("conversationId",), str(uuid.uuid4())),
            (("conversationId",), "not-a-uuid"), (("conversationId",), str(self.chat).upper()),
            (("lifecycle",), "terminal"), (("lifecycle",), []),
            (("role",), "child-main"), (("role",), "spec"), (("role",), []),
            (("project",), None), (("project", "extra"), True),
            (("project", "id"), "foreign-project"), (("project", "id"), ""),
            (("project", "id"), "x" * 257), (("project", "id"), "with space"),
            (("project", "id"), "ä"), (("project", "accountablePmId"), "different-pm"),
            (("project", "nominatedChatId"), str(uuid.uuid4())),
            (("project", "state"), "closed"), (("project", "accountability"), "ambiguous"),
            (("project", "subjectAccess"), "denied"),
        ]
        root_mutations = [
            (("root",), None), (("root", "extra"), True), (("root", "id"), ""),
            (("root", "repository"), "EvexU2/another-repo"), (("root", "number"), True),
            (("root", "number"), 0), (("root", "number"), 42.0),
            (("root", "parentMainId"), str(uuid.uuid4())),
            (("root", "accountableProjectId"), "foreign-project"),
            (("root", "accountablePmId"), "different-pm"),
            (("root", "pmAssigned"), False), (("root", "pmAssigned"), 1),
            (("root", "membershipProjectId"), "foreign-project"),
            (("root", "state"), "terminal"), (("root", "projectChatAccess"), "denied"),
        ]
        for direction in ("project", "parent-main"):
            for damaged_role in ("project", "parent-main"):
                cases = mutations + (root_mutations if damaged_role == "parent-main" else [(("root",), self.root)])
                for path, value in cases:
                    with self.subTest(direction=direction, damaged=damaged_role, path=path, value=value):
                        values = {role: self.conversation(role) for role in ("project", "parent-main")}
                        damaged = values[damaged_role]
                        if not path:
                            del damaged["evexProjectAdmission"]
                        else:
                            obj = damaged["evexProjectAdmission"]
                            for part in path[:-1]:
                                obj = obj[part]
                            obj[path[-1]] = value
                        opposite = "parent-main" if direction == "project" else "project"
                        service, transport = self.service([values[opposite], values[direction]])
                        with self.assertRaises((CapabilityError, ProviderError)):
                            service.send_message(self.token(direction), self.parent if direction == "project" else self.chat, "denied", self.message)
                        self.assertTrue(all(method == "GET" for method, _, _ in transport.calls))

    def test_project_projection_missing_fields_are_not_defaulted(self):
        for role in ("project", "parent-main"):
            original = self.conversation(role)
            for section in (None, "project", "root"):
                obj = original["evexProjectAdmission"] if section is None else original["evexProjectAdmission"][section]
                if obj is None:
                    continue
                for key in obj:
                    value = copy.deepcopy(original)
                    target = value["evexProjectAdmission"] if section is None else value["evexProjectAdmission"][section]
                    del target[key]
                    service, transport = self.service([value])
                    with self.subTest(role=role, section=section, key=key), self.assertRaises(ProviderError):
                        service._provider._project_admission(value, self.chat if role == "project" else self.parent)
                    self.assertEqual(transport.calls, [])

    def test_project_facts_are_fresh_on_each_send_and_unknown_send_is_not_retried(self):
        revoked = self.conversation("project")
        revoked["evexProjectAdmission"]["project"]["subjectAccess"] = "denied"
        service, transport = self.service([
            self.conversation("parent-main"), self.conversation("project"), {},
            self.conversation("parent-main"), revoked,
        ])
        service.send_message(self.token("project"), self.parent, "same-key", self.message)
        with self.assertRaises((CapabilityError, ProviderError)):
            service.send_message(self.token("project"), self.parent, "same-key", self.message)
        self.assertEqual([method for method, _, _ in transport.calls], ["GET", "GET", "POST", "GET", "GET"])
        service, transport = self.service([
            self.conversation("parent-main"), self.conversation("project"), ProviderError("unknown outcome"),
        ])
        with self.assertRaises(ProviderError):
            service.send_message(self.token("project"), self.parent, "uncertain", self.message)
        self.assertEqual([method for method, _, _ in transport.calls], ["GET", "GET", "POST"])

    def test_project_token_binding_outer_identity_and_peer_routes_are_denied(self):
        for direction in ("project", "parent-main"):
            opposite = "parent-main" if direction == "project" else "project"
            for variant in ("sender-id", "target-id", "outer-alias", "peer"):
                sender, target = self.conversation(direction), self.conversation(opposite)
                if variant == "sender-id":
                    sender["id"] = str(uuid.uuid4())
                elif variant == "target-id":
                    target["id"] = str(uuid.uuid4())
                elif variant == "outer-alias":
                    target["conversation_id"] = str(uuid.uuid4())
                else:
                    target["evexProjectAdmission"]["role"] = direction
                service, transport = self.service([target, sender])
                with self.subTest(direction=direction, variant=variant), self.assertRaises((CapabilityError, ProviderError)):
                    service.send_message(self.token(direction), self.parent if direction == "project" else self.chat, "denied", self.message)
                self.assertTrue(all(method == "GET" for method, _, _ in transport.calls))
        service, transport = self.service([self.conversation("parent-main"), self.conversation("project")])
        with self.assertRaises((CapabilityError, ProviderError)):
            service.send_message(project_capability_token(self.secret, self.chat, "other-project"), self.parent, "denied", self.message)
        self.assertEqual([method for method, _, _ in transport.calls], ["GET", "GET"])

    def test_project_projection_cannot_fall_back_to_delivery_tags(self):
        target = self.conversation("project")
        target["tags"] = discussion(self.chat, "child-main", evexparentissue="EvexU2/evex-u-workspace#42")["tags"]
        parent = self.conversation("parent-main")
        parent["tags"] = discussion(self.parent, "parent-main", evexissue="EvexU2/evex-u-workspace#42")["tags"]
        target["evexProjectAdmission"]["project"]["state"] = "closed"
        service, transport = self.service([target, parent])
        with self.assertRaises(ProviderError):
            service.send_message(self.token("parent-main"), self.chat, "denied", self.message)
        self.assertEqual([method for method, _, _ in transport.calls], ["GET", "GET"])

    def test_project_admitted_parent_keeps_ordinary_child_and_spec_routes(self):
        parent = self.conversation("parent-main")
        parent["tags"] = discussion(self.parent, "parent-main", evexissue="EvexU2/evex-u-workspace#42")["tags"]
        for role in ("child-main", "spec"):
            target_id = uuid.uuid4()
            target = discussion(target_id, role, evexparentissue="EvexU2/evex-u-workspace#42")
            service, transport = self.service([target, parent, {}])
            result = service.send_message(self.token("parent-main"), target_id, "ordinary", self.message)
            self.assertTrue(result["accepted"])
            self.assertEqual([method for method, _, _ in transport.calls], ["GET", "GET", "POST"])
            service, transport = self.service([parent, target, {}])
            token = capability_token(self.secret, owning_main_id=self.parent, sender_id=target_id,
                                     role="spec" if role == "spec" else "deputy", task_key="issue-42")
            self.assertTrue(service.send_message(token, self.parent, "ordinary", self.message)["accepted"])
            self.assertEqual([method for method, _, _ in transport.calls], ["GET", "GET", "POST"])

    def test_project_tags_cannot_supply_missing_projection(self):
        fake_chat = discussion(self.chat, "project", evexproject=self.project_id, evexpm="native-pm-node-id")
        service, transport = self.service([self.conversation("parent-main"), fake_chat])
        with self.assertRaises((CapabilityError, ProviderError)):
            service.send_message(self.token("project"), self.parent, "denied", self.message)
        self.assertTrue(all(method == "GET" for method, _, _ in transport.calls))

    def verified_binding(self):
        return {"success": True, "evexProjectCapability": {
            "schemaVersion": 1, "conversationId": str(self.chat), "projectId": self.project_id,
            "bindingVerified": True,
        }}

    def test_project_private_provision_is_deterministic_exact_and_content_free(self):
        service, transport = self.service([self.conversation("project"), self.verified_binding()] * 2)
        request = {"schemaVersion": 1, "conversationId": str(self.chat)}
        results = [service.provision_project_capability(request) for _ in range(2)]
        self.assertEqual(results, [self.verified_binding()["evexProjectCapability"]] * 2)
        self.assertEqual([(method, path) for method, path, _ in transport.calls], [
            ("GET", f"/api/conversations/{self.chat}"),
            ("POST", f"/api/conversations/{self.chat}/secrets"),
        ] * 2)
        expected = {"secrets": {"EVEX_AGENT_MESSAGING_CAPABILITY": {
            "kind": "StaticSecret", "value": project_capability_token(self.secret, self.chat, self.project_id),
        }}}
        self.assertEqual(transport.calls[1][2], expected)
        self.assertEqual(transport.calls[3][2], expected)
        self.assertNotIn("evx3_", json.dumps(results))
        self.assertNotIn("mcp", json.dumps(results).lower())

    def test_project_private_provision_rejects_untagged_context_before_secret_write(self):
        project = self.conversation("project")
        project["tags"] = {}
        service, transport = self.service([project])

        with self.assertRaisesRegex(ProviderError, "environment"):
            service.provision_project_capability({
                "schemaVersion": 1,
                "conversationId": str(self.chat),
            })

        self.assertEqual([method for method, _, _ in transport.calls], ["GET"])

    def test_project_private_provision_schema_denial_before_provider_calls(self):
        request = {"schemaVersion": 1, "conversationId": str(self.chat)}
        for invalid in (None, [], {}, {**request, "extra": True}, {**request, "schemaVersion": True},
                        {**request, "schemaVersion": "1"}, {**request, "conversationId": self.chat.hex},
                        {**request, "conversationId": str(self.chat).upper()},
                        {**request, "conversationId": 4}):
            service, transport = self.service([])
            with self.subTest(invalid=invalid), self.assertRaises(CapabilityError):
                service.provision_project_capability(invalid)
            self.assertEqual(transport.calls, [])

    def test_project_private_provision_requires_nominated_eligible_host_project(self):
        values = [discussion(self.chat, "project"), self.conversation("parent-main")]
        for path, replacement in (("nominatedChatId", str(uuid.uuid4())), ("subjectAccess", "denied"),
                                  ("state", "closed"), ("accountability", "ambiguous")):
            value = self.conversation("project")
            value["evexProjectAdmission"]["project"][path] = replacement
            values.append(value)
        for value in values:
            service, transport = self.service([value])
            with self.subTest(value=value), self.assertRaises(ProviderError):
                service.provision_project_capability({"schemaVersion": 1, "conversationId": str(self.chat)})
            self.assertEqual([method for method, _, _ in transport.calls], ["GET"])

    def test_project_private_provision_rejects_legacy_and_malformed_postconditions(self):
        invalid = [{}, {"success": True}, {"success": False}, {**self.verified_binding(), "extra": True}]
        for key, replacement in (("schemaVersion", True), ("schemaVersion", 2),
                                 ("conversationId", str(uuid.uuid4())), ("projectId", "foreign-project"),
                                 ("bindingVerified", False), ("bindingVerified", 1), ("extra", True)):
            response = self.verified_binding()
            response["evexProjectCapability"][key] = replacement
            invalid.append(response)
        for response in invalid:
            service, transport = self.service([self.conversation("project"), response])
            with self.subTest(response=response), self.assertRaises(ProviderError) as raised:
                service.provision_project_capability({"schemaVersion": 1, "conversationId": str(self.chat)})
            self.assertLess(len(str(raised.exception)), 200)
            self.assertNotIn("evx3_", str(raised.exception))
            self.assertEqual([method for method, _, _ in transport.calls], ["GET", "POST"])

    def test_project_private_timeout_does_not_retry_later_exact_trigger_revalidates(self):
        service, transport = self.service([
            self.conversation("project"), ProviderError("unknown outcome"),
            self.conversation("project"), self.verified_binding(),
        ])
        request = {"schemaVersion": 1, "conversationId": str(self.chat)}
        with self.assertRaises(ProviderError):
            service.provision_project_capability(request)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(service.provision_project_capability(request), self.verified_binding()["evexProjectCapability"])
        self.assertEqual(transport.calls[1], transport.calls[3])
        self.assertEqual([method for method, _, _ in transport.calls], ["GET", "POST", "GET", "POST"])

    def test_project_private_auth_uses_existing_service_credential_only_as_trigger(self):
        service, transport = self.service([])
        self.assertTrue(service.provisioning_allowed("private-service-key"))
        for credential in (None, "", "foreign", self.token("project"), "unicode-ä"):
            self.assertFalse(service.provisioning_allowed(credential))
        self.assertEqual(transport.calls, [])


class ConversationResponseBudgetTest(unittest.TestCase):
    LIMIT = 1024 * 1024

    def setUp(self):
        self.parent, self.child = uuid.uuid4(), uuid.uuid4()
        self.capability = capability_token(
            b"test-secret", owning_main_id=self.parent, sender_id=self.child,
            task_key="issue-927", role="deputy",
        )
        self.provider = configured_provider("http://openhands", "test-api-key")
        self.server = McpServer(MessagingService(self.provider, b"test-secret"))
        self.request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "send_message", "arguments": {
                "targetId": str(self.parent), "messageKey": "final-review",
                "message": {"humanSummary": "Review passed", "aiEvidence": {
                    "outcome": "PASS", "evidence": [], "findings": [], "nextBoundary": "spec review",
                }},
            }},
        }

    def parent_bytes(self, size, **overrides):
        value = discussion(
            self.parent,
            "parent-main",
            evexissue="EvexU2/evex-u-workspace#927",
        )
        value["stats"] = {"per_turn": "private-statistics" + "x" * 66000}
        value.update(overrides)
        raw = json.dumps(value).encode()
        self.assertLessEqual(len(raw), size)
        return raw + b" " * (size - len(raw))

    def send(self, raw):
        class Response(io.BytesIO):
            def read(inner, size=-1):
                inner.read_limit = size
                return super().read(size)

        response = Response(raw)
        sender = io.BytesIO(json.dumps(discussion(
            self.child,
            "child-main",
            evexparentissue="EvexU2/evex-u-workspace#927",
            evexparent=str(self.parent),
        )).encode())
        with patch(
            "urllib.request.urlopen",
            side_effect=[response, sender, io.BytesIO(b"{}")],
        ) as http:
            result = self.server.handle(self.request, capability_ref=self.capability)
        return result, http.call_args_list, response.read_limit

    def test_long_running_parent_and_exact_limit_allow_one_authorized_event(self):
        for size in (69143, self.LIMIT):
            with self.subTest(size=size):
                result, calls, read_limit = self.send(self.parent_bytes(size))
                self.assertEqual(result["result"]["structuredContent"], {
                    "accepted": True, "messageKey": "final-review",
                })
                self.assertEqual([call.args[0].method for call in calls], ["GET", "GET", "POST"])
                self.assertEqual(calls[0].args[0].full_url, f"http://openhands/api/conversations/{self.parent}")
                self.assertEqual(calls[1].args[0].full_url, f"http://openhands/api/conversations/{self.child}")
                self.assertEqual(calls[2].args[0].full_url, f"http://openhands/api/conversations/{self.parent}/events")
                self.assertTrue(json.loads(calls[2].args[0].data)["run"])
                self.assertEqual(read_limit, self.LIMIT + 1)
                self.assertEqual([call.kwargs["timeout"] for call in calls], [5.0, 5.0, 5.0])

    def test_over_limit_fails_before_parsing_or_event_post(self):
        with patch("evex_agent_messaging.provider.json.loads", wraps=json.loads) as parse:
            result, calls, read_limit = self.send(b"x" * (self.LIMIT + 1))
        self.assertFalse(any(isinstance(call.args[0], bytes) for call in parse.call_args_list))
        self.assertEqual(result["error"]["code"], -32000)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0].method, "GET")
        self.assertEqual(read_limit, self.LIMIT + 1)
        self.assertNotIn("private-statistics", json.dumps(result))

    def test_large_invalid_id_or_target_role_cannot_authorize_a_post(self):
        for overrides in (
            {"id": str(uuid.uuid4())},
            {"id": "invalid"},
            {"tags": {"project": "foreign", "evexdeliveryrole": "parent-main"}},
            {"tags": {"project": "evex-u", "evexdeliveryrole": "spec"}},
        ):
            with self.subTest(overrides=overrides):
                result, calls, _ = self.send(self.parent_bytes(69143, **overrides))
                self.assertIn("error", result)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].args[0].method, "GET")
                self.assertNotIn("private-statistics", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
