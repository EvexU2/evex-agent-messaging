# EVEX Agent Messaging Gateway

Provider-neutral MCP transport for Parent Main, Child Main, and Role Child conversation operations.
Agents call this MCP; they do not call OpenHands' Conversation API directly.

## Tools

- `create_child`: validate the exact role, model, `medium|high` reasoning effort, complete structured
  Mission, role-compatible mutation envelope, and clean deterministic Git checkout,
  bind Child identity plus callback capability, then create one deterministic Conversation. The
  provider verifies the exact branch/head, switches and verifies the requested model, atomically
  admits tools, starts the Mission, and returns without waiting for the Child turn. No
  Conversation API call occurs when checkout authority is missing or mismatched. The provider switches
  and verifies the requested model before Mission delivery. Read-only Review/QA require no mutations;
  Spec/Writer/Repair require exact mutations; Plan Author/Review/QA are read-only. Source roles receive only Messaging; only
  explicit integrated Writer/QA/Repair Missions may request `capabilities: ["runtime_environment"]`; the provider
  binds that exact capability into the Child launch so the pod wrapper materializes Runtime MCP only
  for that Child.
- `send_to_parent`: deliver a structured `RESULT` to the capability's owning Main; transport derives
  identity, kind, and replay key. Each Child Mission and authorized `RESUME_MISSION` event carries a
  provider-derived opaque `callbackGeneration`; `send_to_parent.result.callbackGeneration` must echo
  that exact current value. Messaging re-derives it from bounded provider event history and rejects
  stale, foreign, missing, malformed, ambiguous, incomplete, or truncated evidence before delivery.
  Provider-emitted control records are HMAC-authenticated before they can win a result, input, resume,
  cancellation-replay, or replacement decision. This required field is a compatibility change for Child
  result callers. A native terminal `CANCELLED` Child rejects late `RESULT` and `NEEDS_INPUT` callbacks.
- `send_callback_fallback`: after exactly three current-generation, canonical, byte-identical
  `send_to_parent` inputs receive provider-classified retryable transport errors, converge the one
  immutable-Mission-authorized recovery marker with the server-held repository-scoped GitHub App
  credential. The no-argument tool derives target, body, retry evidence, and credential server-side;
  an exact App-authored marker replay is a no-op and never proves Mission completion.
- `request_user_decision`: deliver an A/B/C-style question to the owning Main. It likewise requires the
  exact current `callbackGeneration`, so an earlier continuation cannot recreate a cleared input gate.
- `cancel_mission`: stop the exact Child task. The owning Main must provide its deterministic Child,
  task, and stable cancellation key. Cancellation serializes against a terminal result and resume;
  it succeeds only after OpenHands reports native terminal `cancelled`, and an identical key replays
  that terminal `CANCELLED` outcome. A finished/error/stuck Child is not relabeled as cancelled.
- `resume_mission`: resume the exact Child task with a non-empty JSON context of verified facts; the
  context cannot expand its immutable Mission authority and is rejected after terminal cancellation.
  After an accepted terminal result, one fresh deterministic finding key re-admits the same Specialist
  once and advances its opaque callback generation. Exact key/context replay is a no-op; changed
  context and later fresh-key cycles remain fail-closed and repeatable respectively.
- `publish_navigation_links`: publish informational Issue/Main/Child/PR links to the owning Main.
- `get_usage`: read live per-Conversation model, reasoning effort, uncached/cached/cache-write input,
  output with reasoning as a subset, cache-hit rate, long-context turns, and a versioned official
  Standard API-equivalent USD estimate. Main may read itself or a deterministic Child of its task;
  this is stateless observability, never workflow authority or a ChatGPT subscription invoice.

The Platform Operator may set `EVEX_WRITE_MISSION_ADMISSION_PAUSED=true` during a Skills-authority
cutover. It rejects only new or resumed Spec/Writer work with the stable
`write_mission_admission_paused` outcome before durable admission; Plan, Review, and QA continue.
Live write-Mission inventory and drain requests remain provider-adapter controls, not MCP tools: a
drain request goes to the owning Main, which alone obtains terminal cancellation proof.

Capabilities are compact opaque `evx1_` HMAC-SHA256 references. They bind the owning Main, Child, task key, role,
allowed action, and expiry. The server holds the OpenHands credential; it never appears in tool input,
tool output, or child environment variables. There is no persistent state: callers provide a stable
message key and the Main deduplicates semantic replays.

Children must complete one accepted `send_to_parent` call before they finish. The fallback marker is
wake-only and cannot replace that evidence. There is no completion endpoint, poller, receipt store, or
generic second callback channel. If a process crash prevents both callback and authorized fallback,
the owning Issue remains incomplete and an operator restarts its deterministic Main in Recovery Mode;
the Main reuses current Issues, branches, pull requests, and verified evidence.

The trusted Event Gateway/host mints the short-lived Main capability with
`main_capability_token(...)` and binds it to that Conversation's MCP transport as Bearer auth. Child
capabilities are bound the same way during provider admission. Agents never read, copy, or supply a
capability in tool arguments, and the MCP exposes no capability-minting tool.

The Parent creates or fetches only the persistent bare mirror at
`/home/openhands/workspace/mirrors/<owner>--<repository>.git`. `create_child` derives
`/home/openhands/workspace/delivery/child-<child UUID>`, creates the exact branch worktree when absent,
and validates repository, origin, branch, head, and cleanliness before touching the Conversation API.
It never clones/fetches remotely, deletes, resets, or repurposes an existing worktree.

## Replacement admission

`create_child` accepts an optional `replacement` proof for a cancelled predecessor. It names the
cancelled deterministic Child, task, and cancellation key, plus the Main's post-terminal and
immediate-pre-admission authorized branch/Draft-PR projections. Messaging compares those two
projections and verifies native terminal cancellation through the provider; it never reads GitHub or
stores cancellation, replacement, callback, or workflow state. A changed projection is rejected so
the Main can preserve or explicitly supersede the in-flight work and repeat both reads. The replacement
uses a new task key and therefore a distinct Child identity.

## Run

The in-cluster server uses Streamable HTTP; the trusted host supplies the per-Conversation Bearer
capability in MCP configuration:

```sh
export EVEX_MESSAGING_SECRET='long-random-secret'
export OPENHANDS_URL='http://openhands:8000'
export OPENHANDS_API_KEY='server-only-key'
export OPENHANDS_PUBLIC_URL='http://openhands.example/canvas'
export EVEX_MESSAGING_FALLBACK_GITHUB_APP_ID='12345'
export EVEX_MESSAGING_FALLBACK_GITHUB_INSTALLATION_ID='67890'
export EVEX_MESSAGING_FALLBACK_GITHUB_APP_PRIVATE_KEY="$(cat /run/secrets/messaging-fallback.pem)"
export EVEX_MESSAGING_FALLBACK_GITHUB_APP_LOGIN='messaging-fallback[bot]'
export EVEX_MESSAGING_TRANSPORT=http
PYTHONPATH=src python3 -m evex_agent_messaging
```

Only the adapter reads the OpenHands variables. A fake provider can be injected in tests or by a host
embedding the service, so skills and Missions remain runtime-neutral. The stdio framing helper is for
protocol tests only; capability-bound tool execution uses authenticated HTTP.

## Validation

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -q
```
