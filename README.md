# EVEX Agent Messaging

Cluster-internal authenticated creation and transport for the interactive Spec Chat and bounded
Specialist Conversations. Messaging never inventories Conversations or owns workflow state.

## Contract

The MCP exposes exactly:

```text
create_spec_chat()
start_specialist(missionKey, prompt, agentType, description, skills?)
send_message(targetId, messageKey, message)
```

Only Issue Main may call `create_spec_chat`. It deterministically creates or reuses the Issue's one
Spec Chat, derives the Workspace repository and `spec/issue-<number>` branch from the verified Issue
Discussion, validates the Issue Main's clean `main` checkout, and derives one independent isolated Spec
checkout from its observed head. The caller supplies no repository, branch, or SHA. Messaging needs no
GitHub credential, public-egress rule, or shared mirror for this operation. New chats bind the
OpenHands-owned `spec` role, `evex-delivery-spec` skill and the currently selected supported
Agent Profile (`acp` or native `openhands`); the profile, rather than Messaging, owns the model.
Messaging never calls the ACP model-switch endpoint. It stages the canonical bootstrap without
starting an ordinary turn. The Spec Chat proceeds from that bootstrap and uses no OpenHands Delivery
Goal. Only freshly admitted `v3` Spec Chats are reusable. Retained earlier generations remain
untouched and fail closed without metadata migration, event delivery, or model switching. The
operation returns the stable ID and Canvas URL and has no generic role, Mission, callback,
task-control, or Conversation-search surface.

Issue Main, direct Subissue Main, and interactive Spec Chat retain their byte-identical `evx2_`
transport-bound HMAC Bearer capabilities. A Specialist receives the same capability format,
bound to its exact Conversation and immediate Owner. It may return to that Owner and send to a direct
Specialist child whose live admission binds that child to it. Existing role
bytes are unchanged; `specialist` adds role byte `4`. Before posting, the provider reads
the exact target Discussion—and, for Parent-to-Child/Spec messages, the exact sender—and verifies the
relationship from their admission tags. It never searches or inventories Conversations.
Provider JSON responses are capped at 1 MiB because exact Conversation reads also include growing
usage statistics. This transport bound does not increase the 20,000-byte outgoing message budget;
over-limit responses still fail before parsing or dependent event delivery.
The signed capability remains valid for its Discussion lifetime; it has no independent expiry or
refresh lifecycle. Ordinary messages therefore post only the target event and never rewrite target
secrets.

### Project admission (consumer implementation; host producer required)

The nominated Project Chat uses a distinct send-only `evx3_` capability. Messaging remains the sole
signer. Its payload is `version 3 | sender UUID (16 bytes) | send-only action (2) | Project ID byte
length (uint16, big-endian) | Project ID | HMAC-SHA256`. The Project capability has no owning Main or
task key. Native node IDs are opaque, nonempty visible ASCII, bounded to 256 bytes; no node-ID prefix
is inferred. Existing `evx2_` bytes and public Messaging operations are unchanged; Messaging now
mints the same capability bytes while admitting a Main through its private Gateway operation.

Both Project→root Issue and root Issue→Project sends read both exact authenticated
`GET /api/conversations/{canonicalUuid}` objects on every call. Only the host-computed
`evexProjectAdmission` projection supplies Project authority, never tags, caller-selected roles,
token viewers, cached facts, or generic finished-turn status. All nested projection keys and types
are strict; schema version is the integer `1` (not a boolean).

```text
evexProjectAdmission = {
  schemaVersion: 1, conversationId: canonicalUuid,
  role: "project" | "parent-main", lifecycle: "eligible" | "terminal",
  project: {
    id: nativeProjectId, accountablePmId: nativeUserId, nominatedChatId: canonicalUuid,
    state: "open" | "closed", accountability: "unique" | "ambiguous",
    subjectAccess: "allowed" | "denied"
  },
  root: null | {
    id: nativeWorkspaceIssueId, repository: "EvexU2/evex-u-workspace", number: positiveInteger,
    parentMainId: canonicalUuid, accountableProjectId: nativeProjectId,
    accountablePmId: nativeUserId, pmAssigned: boolean, membershipProjectId: nativeProjectId,
    state: "eligible" | "terminal", projectChatAccess: "allowed" | "denied"
  }
}
```

`root` is null only for Project; the Issue requires the root object. The host's projection attests its
verified role, original attributable PM-event provenance and fresh native GitHub facts. Messaging
cross-checks sender/endpoint identities, nominated Chat, Project, same PM, exact Issue UUID, root
accountability, native membership and PM assignment. Both endpoints must be eligible, open, uniquely
accountable and accessible. Missing/malformed/stale, closed, terminal, denied, ambiguous, foreign,
Subissue/Spec or peer bindings produce zero event writes. There is no fallback while the host producer
is absent. A successful event is still only a wake: recipients must revalidate current facts and
original decision authority before acting.

### Private Project capability provisioning

The existing HTTP process accepts only the internal host trigger:

```text
POST /internal/project-capability
Authorization: Bearer <existing host service credential>
Content-Type: application/json

{"schemaVersion":1,"conversationId":"<canonical UUID>"}
```

The existing OpenHands service credential authenticates this trigger only, not the PM. The body is
limited to 1,024 bytes with exact keys; duplicate keys, noncanonical UUIDs, extra fields and unsupported
versions fail closed. The provider alone checks the host credential and uses the existing authenticated
host API. No public MCP initialize/list/send operation provisions a capability, and no public mint,
inventory, lifecycle or control tool is added.

Messaging reads that exact currently nominated eligible Project Chat, derives its deterministic
capability with the existing Messaging secret, and sends exactly one existing
`POST /api/conversations/{id}/secrets` request containing only:

```text
{"secrets":{"EVEX_AGENT_MESSAGING_CAPABILITY":{"kind":"StaticSecret","value":"<capability>"}}}
```

The required host response is exactly `{"success":true,"evexProjectCapability":{...}}`, whose inner
object is exactly `{schemaVersion:1, conversationId:<same UUID>, projectId:<same ID>, bindingVerified:true}`.
A generic legacy success, missing field, identity mismatch or malformed response is unverified.
The private endpoint returns only that inner version/identity/verified-binding object, never the
capability, MCP-loaded status, raw provider content or an admission receipt.

The host must revalidate admission and serialize its live/durable comparison before writing. Equal
live and durable bindings are a no-op without ACP refresh; equal live but missing durable binding
repairs durable state only; a different binding uses the existing resume-secret path once. The host
must verify persistence after writing. These are host obligations, not guarantees established by
Messaging's consumer tests. No extra read API, comparison header, hash or receipt is introduced.
Timeout or unknown outcome causes no automatic retry. A later normal exact-object trigger reads
current admission again and relies on the host's compare-before-write behavior.

Source delivery order is Messaging → host producer, with no circular runtime dependency. The host
producer is currently unavailable, including the admitted per-PM GitHub entitlement/access path;
consumer fixture passes are not installed support or general-PM access proof. Host authentication,
PM provenance, persistence/no-refresh behavior, combined two-root Canary and exact-revision runtime
proof remain required before rollout acceptance. No deployment, activation or live evaluation is
part of this source change.

Architecture impact: public MCP operations remain three; Messaging creates bounded durable Specialist Conversations but no
checkout, service, workflow store, recovery transport or background loop. It admits one additional
relationship class for the already-existing PM-nominated Chat. PM interaction creates/nominates that
Chat in the host; the Project/PM owns its authority, it has no source Writer/checkout, and admitted
messages wake only a bounded processing turn. Closed Projects and terminal Delivery actors remain
ineligible. The private provisioning request is internal wiring in the existing process.

The message is exactly `{humanSummary, aiEvidence}`: a non-empty plain-language `humanSummary` of at
most 2,000 UTF-8 bytes and `aiEvidence` of `{outcome, revision?, evidence, findings, nextBoundary}`.
The canonical compact JSON is at most 20,000 UTF-8 bytes. The provider visibly projects only the
summary and places the canonical envelope in a versioned renderer-hidden machine block, preserving
the exact evidence for the receiver without a legacy raw-text path. Malformed, oversized,
credential-bearing, or unrenderable input fails before any provider mutation with a bounded
content-free error.

The canonical EVEX dialogue skills own Eve's language, terminology, and explanation depth. Messaging
does not impose a conversation language or locale authority tag, and replay does not rewrite the
conversation's language or launch instructions. The skills follow the user's own prose from the
first response, retain explicit Chat-local preferences, and use German only when no initial language
signal exists. Durable artifacts remain English. Existing conversations keep their original launch
instructions; this change does not migrate or replace them.

The provider then posts one bounded user event and returns `accepted: true` only after OpenHands accepts
that request. `messageKey` is correlation data, not a lock or receipt. Multiple genuine messages are
allowed. The receiver re-reads GitHub, Git, Spec, and runtime facts before acting.

There is no generic Child creation, callback kind/generation, result lock, human-question relay,
resume, cancel, replacement, usage, GitHub fallback, queue, poller, or persistent state. A coordinator
or Mission-authorized Specialist creates a bounded direct Specialist with `start_specialist`. Creator
and direct child then communicate bidirectionally with `send_message`; questions, findings,
follow-ups, releases, cancellation, and the child's terminal return all use that same operation.
Siblings, unrelated peers, and transitive routes remain forbidden.

Creation returns the observed Spec checkout repository, branch, and current head as evidence. Those
observations never become caller authority or replay input; an existing deterministic Spec Chat and
checkout win on replay.

## Run

The same HTTP process also exposes provider-neutral `POST /internal/agent-deliveries` for the Gateway.
It is not an MCP tool. A dedicated Bearer credential is checked before the exact request body is
parsed. Messaging then owns Main admission, creation, identity verification, wake delivery, and the
short retry of safe OpenHands GETs. A missing target that the routed event may not create returns the
normal result `{"accepted":false,"reason":"target_missing_not_intake_authorized"}`.

```sh
export EVEX_MESSAGING_SECRET='long-random-secret'
export EVEX_GATEWAY_DELIVERY_SECRET='separate-long-random-secret'
export EVEX_DELIVERY_ADMISSION_KEY='long-random-admission-key'
export OPENHANDS_URL='http://openhands:8000'
export OPENHANDS_PUBLIC_URL='http://openhands.local/canvas'
export OPENHANDS_API_KEY='server-only-key'
export EVEX_MESSAGING_TRANSPORT=http
PYTHONPATH=src python3 -m evex_agent_messaging
```

The trusted host supplies the per-Discussion capability as the MCP Bearer credential. Agents never
read or pass it as a tool argument. The OpenHands session credential stays server-side.

## Validation

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -q
python3 -m compileall -q src tests
```
