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
the exact target Discussion—and, for Parent-to-Child/Spec messages, the exact sender—and verifies the
relationship from their admission tags. It never searches or inventories Conversations.
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

```sh
export EVEX_MESSAGING_SECRET='long-random-secret'
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
