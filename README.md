# EVEX Agent Messaging

Cluster-internal authenticated lifecycle and transport for the one interactive Spec Chat plus bounded
messages between already-known durable OpenHands Discussions. Messaging never inventories Discussions
and does not control native subagents.

## Contract

The MCP exposes exactly:

```text
create_spec_chat()
send_message(targetId, messageKey, message)
```

Only Parent Main may call `create_spec_chat`. It deterministically creates or reuses the Issue's one
Spec Chat, derives the Workspace repository and `spec/issue-<number>` branch from the verified Parent
Discussion, validates the Parent's clean `main` checkout, and derives one independent isolated Spec
checkout from its observed head. The caller supplies no repository, branch, or SHA. Messaging needs no
GitHub credential, public-egress rule, or shared mirror for this operation. It fixes the role to Spec
on Sol/high and returns its stable ID and Canvas URL. The operation has no generic role, Mission,
callback, task-control, or Conversation-search surface.

Only Parent Main, direct Child Main, and interactive Spec Chat receive a transport-bound HMAC Bearer
capability. The capability identifies the sender and owning Parent. Before posting, the provider reads
both the exact target and sender Discussions and verifies their relationship and operator-matching
environment context from admission tags. It never searches or inventories Conversations.
Provider JSON responses are capped at 1 MiB because exact Conversation reads also include growing
usage statistics. This transport bound does not increase the 20,000-byte outgoing message budget;
over-limit responses still fail before parsing or dependent event delivery.
The signed capability remains valid for its Discussion lifetime; it has no independent expiry or
refresh lifecycle. Ordinary messages therefore post only the target event and never rewrite target
secrets.

The message is exactly `{humanSummary, aiEvidence}`: a non-empty plain-language `humanSummary` of at
most 2,000 UTF-8 bytes and `aiEvidence` of `{outcome, revision?, evidence, findings, nextBoundary}`.
The canonical compact JSON is at most 20,000 UTF-8 bytes. The provider visibly projects only the
summary and places the canonical envelope in a versioned renderer-hidden machine block, preserving
the exact evidence for the receiver without a legacy raw-text path. Malformed, oversized,
credential-bearing, or unrenderable input fails before any provider mutation with a bounded
content-free error.

Every newly created Spec Chat starts human dialogue in `de-DE`; an explicit language change stays on
that Chat when it is reused. Durable artifacts remain English.

The provider then posts one bounded user event and returns `accepted: true` only after OpenHands accepts
that request. `messageKey` is correlation data, not a lock or receipt. Multiple genuine messages are
allowed. The receiver re-reads GitHub, Git, Spec, and runtime facts before acting.

There is no generic Child creation, callback kind/generation, result lock, human-question relay,
resume, cancel, replacement, usage, GitHub fallback, queue, poller, or persistent state.
Native Plan, Writer, Review, QA, Repair, and Spec Review use their owning Discussion's task handles.

Creation returns the observed Spec checkout repository, branch, and current head as evidence. Those
observations never become caller authority or replay input; an existing deterministic Spec Chat and
checkout win on replay.

## Run

Use the single operator configuration in `evex-u-k8s/.env`, prepared from that repository's
`.env.example`. Its preflight maps approved values into `evex-agent-platform`; do not create a
second Messaging `.env` template. This Python service reads its process environment only: it does
not parse or source an `.env` file.

| Service input | Kubernetes / canonical configuration source | Standalone behavior |
| --- | --- | --- |
| `EVEX_ENVIRONMENT_ID` | Same key in canonical `.env`, then `evex-agent-platform` | Required |
| `EVEX_INTAKE_LABEL` | Same key in canonical `.env`, then `evex-agent-platform` | Required |
| `OPENHANDS_URL` | Same key in canonical `.env`, then `evex-agent-platform` | Required HTTP(S) internal origin; no path except `/` |
| `OPENHANDS_PUBLIC_URL` | Same key in canonical `.env`, then `evex-agent-platform` | Required HTTP(S) public URL ending in `/canvas` (optional trailing slash) |
| `EVEX_MESSAGING_SECRET` | Runtime-managed `openhands-auth` Secret, same key | Trusted host supplies the existing per-environment HMAC secret |
| `OPENHANDS_API_KEY` | Runtime-managed `openhands-auth` Secret, key `LOCAL_BACKEND_API_KEY` | Trusted host supplies the existing OpenHands session key |
| `EVEX_MESSAGING_TRANSPORT` | Fixed deployment value `http` | Optional `http` or `stdio`; default `stdio` |
| `EVEX_MESSAGING_HOST` | Default bind address `0.0.0.0` | Optional nonempty bind host/IP |
| `EVEX_MESSAGING_PORT` | Fixed deployment value `3101` | Optional integer `1`–`65535`; default `3101` |

Transport, host, and port are standalone controls, not extra canonical `.env` inputs. Kubernetes
Service/probe ports remain coordinated deployment constants. Both URLs reject credentials, query,
fragment, whitespace, and malformed ports. Invalid configuration fails before serving with a sanitized
error; a transport typo never silently selects stdio. Production additionally requires HTTPS for
the public URL and rejects known local hosts in both URLs: `localhost`, `.localhost` names,
loopback IPs, and unspecified IPs (including IPv4-mapped IPv6). Ambiguous numeric IPv4 forms
(such as `127.1` or hexadecimal addresses) are rejected in production. Encoded hostnames and
backslashes are rejected in every environment. Production host classification includes IDNA
normalization and performs no DNS lookup.
Internal HTTP service origins such as `openhands.evex-agents.svc.cluster.local` remain supported;
development may use HTTP and local hosts. This configuration check does not prove deployment,
reachability, authentication, or production readiness.

The OpenHands session key and Messaging signing secret remain stable runtime-managed credentials.
Do not copy them into the canonical `.env`, export them blindly from Kubernetes, regenerate them
on startup, or replace them with a personal GitHub token. The trusted standalone launcher supplies
these credentials securely as part of the six required inputs, then runs:

```sh
PYTHONPATH=src python3 -m evex_agent_messaging
```

Both environment inputs are required without defaults or whitespace normalization. Production must
explicitly configure `production` with `agent:ready`; development uses `dev:<developer>` with
`agent:dev:ready:<developer>`. The developer suffix matches `[a-z0-9][a-z0-9-]{0,33}`.
A development environment cannot use the production label.

The Parent's `evexenvironment` and `evexintakelabel` tags must match this deployment before
Spec lifecycle mutations. New Spec Chats receive the same pair in tags and runtime secrets;
reuse validates context and checkout before model, capability, or event mutation. Every message
requires current sender and target facts with the same configured pair. Missing, malformed, duplicate,
or foreign context is rejected; untagged existing Discussions require fresh valid admission, not an
automatic context backfill. The per-environment HMAC format and public MCP shapes are unchanged.
Internal messaging does not read GitHub labels or acquire GitHub credentials, and removing an Issue
intake label does not stop these already-admitted internal Conversations. There is no environment
handover, new service, state store, or background loop.

The trusted host supplies the per-Discussion capability as the MCP Bearer credential. Agents never
read or pass it as a tool argument. The OpenHands session credential stays server-side.

## Validation

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -q
python3 -m compileall -q src tests
```
