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
  explicit integrated Writer/QA/Repair Missions may request `runtime_environment`; existing narrower
  Writer/Repair exceptions remain compatible. A QA Environment grant additionally binds the exact
  Environment/generation, Candidate/configuration digests, scenarios, namespace, literal `toolbox`
  Pod, reviewed manifest digest, and expiry. Its `principalId` is `sha256:` plus the lowercase
  SHA-256 hex digest of UTF-8 canonical JSON (recursive key sort and no whitespace) for exactly
  `schemaVersion`, `owningMainId`, `childId`, `taskKey`, and `role`; UUIDs are lowercase canonical
  strings. Scenarios are lowercase, sorted, unique identifiers matching
  `[a-z0-9][a-z0-9._-]*`. The frozen cross-runtime vector is
  `tests/fixtures/environment-grant-v1.json`. Messaging injects only its opaque
  `EVEX_RUNTIME_ENVIRONMENT_GRANT`; the Runtime MCP revalidates the signed claims before every
  invocation. Skill-authoring Writer and QA Missions may separately request `model_pressure`; the
  independently derived `modelPressureGeneration` never reuses Environment identity.
- `run_model_pressure`: re-read the signed Mission and live Child identity, revalidate exact skill
  Candidate, scenario, model/mode, expiry, and `modelPressureGeneration`, then invoke the isolated
  provider-held model credential. Writer grants admit only `red|green`; QA grants admit only `forward`.
  The capability and live signed binding are checked again after completion and before any report is
  returned. The provider endpoint must be public HTTPS on effective port 443; userinfo, query,
  fragment, redirects, non-public/reserved addresses, and local/service DNS suffixes fail before an
  Authorization header is constructed. IPv4 literals and every DNS answer also enforce the exact
  14-CIDR K8s egress exclusion contract pinned in `tests/fixtures/model-provider-network-v1.json`.
  The result requires exactly one unique outcome for every requested assertion and contains only
  binding identity, boolean assertion outcomes, outcome, allowlisted diagnosis codes, bounded token
  counts, and assertion-keyed evaluator-status enums with no free-form provider text. Prompt-containing
  evidence, full prompts/completions, raw provider requests/logs, credentials, callback secrets, and
  transcript archives are never returned.
- `send_to_parent`: deliver a structured `RESULT` to the capability's owning Main; transport derives
  identity, kind, and replay key. Each Child Mission and authorized `RESUME_MISSION` event carries a
  provider-derived opaque `callbackGeneration`; `send_to_parent.result.callbackGeneration` must echo
  that exact current value. Messaging re-derives it from bounded provider event history and rejects
  stale, foreign, missing, malformed, ambiguous, incomplete, or truncated evidence before delivery.
  Provider-emitted control records are HMAC-authenticated before they can win a result, input, resume,
  cancellation-replay, or replacement decision. This required field is a compatibility change for Child
  result callers. A native terminal `CANCELLED` Child rejects late `RESULT` and `NEEDS_INPUT` callbacks.
- `request_user_decision`: deliver an A/B/C-style question to the owning Main. It likewise requires the
  exact current `callbackGeneration`, so an earlier continuation cannot recreate a cleared input gate.
- `cancel_mission`: stop the exact Child task. The owning Main must provide its deterministic Child,
  task, and stable cancellation key. Cancellation serializes against a terminal result and resume;
  it succeeds only after OpenHands reports native terminal `cancelled`, and an identical key replays
  that terminal `CANCELLED` outcome. A finished/error/stuck Child is not relabeled as cancelled.
- `resume_mission`: resume the exact Child task with a non-empty JSON context of verified facts; the
  context cannot expand its immutable Mission authority and is rejected after terminal cancellation.
  After an accepted terminal result, one fresh deterministic finding key re-admits the same Specialist
  once and advances its opaque callback generation. Exact key/context replay emits no duplicate event
  or turn; a Reviewer replay still performs required authenticated checkout reconciliation before
  returning accepted. Changed context and later fresh-key cycles remain fail-closed and repeatable
  respectively. For a read-only
  Reviewer whose signed Mission canonically binds `links.specificationPr`, the provider uses its
  server-held GitHub credential to derive the same open Draft PR's current head. Under the Child lock
  it permits only an optional `context.currentRevision` equality assertion, verifies clean monotonic
  Mission-head/checkout/PR-head ancestry, performs a guarded fast-forward, and revalidates the PR and
  checkout immediately before emitting one signed resume event. It never fetches, resets, changes
  Reviewer identity/callback, or exposes a generic GitHub operation.
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
allowed action, and expiry. The server holds the OpenHands credential and the optional platform
`GITHUB_TOKEN` used only by the exact Reviewer PR-read boundary; neither credential appears in tool
input, tool output, or child environment variables. There is no persistent state: callers provide a
stable message key and the Main deduplicates semantic replays.

Children must complete one accepted `send_to_parent` call before they finish. There is no provider
Stop hook, completion endpoint, poller, receipt store, or second callback channel. If a process crash
prevents that callback, the owning Issue remains incomplete and an operator restarts its deterministic
Main in Recovery Mode; the Main reuses current Issues, branches, pull requests, and verified evidence.

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
# Required only when Environment grants are admitted; shared server-side with Runtime MCP.
export RUNTIME_MCP_GRANT_SECRET='at-least-32-random-bytes-server-only'
# Required only for model-pressure execution; never exposed to the Child.
export EVEX_MODEL_PRESSURE_PROVIDER_URL='https://example.com/model-pressure/evaluate'
export EVEX_MODEL_PRESSURE_PROVIDER_API_KEY='server-only-model-key'
# Required only for Reviewer Missions that bind links.specificationPr.
export GITHUB_TOKEN='server-only-platform-token'
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
