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
  Spec/Plan Author/Writer/Repair require exact mutations; Review/QA are read-only. Source roles receive only Messaging; only
  explicit QA/repair Missions may request `capabilities: ["runtime_environment"]`; the provider
  binds that exact capability into the Child launch so the pod wrapper materializes Runtime MCP only
  for that Child.
- `send_to_parent`: deliver a structured `RESULT` to the capability's owning Main; transport derives
  identity, kind, and replay key.
- `request_user_decision`: deliver an A/B/C-style question to the owning Main.
- `cancel_mission`: stop the exact Child task.
- `resume_mission`: resume the exact Child task with a non-empty JSON context of verified facts; the
  context cannot expand its immutable Mission authority.
- `publish_navigation_links`: publish informational Issue/Main/Child/PR links to the owning Main.
- `get_usage`: read live per-Conversation model, reasoning effort, uncached/cached/cache-write input,
  output with reasoning as a subset, cache-hit rate, long-context turns, and a versioned official
  Standard API-equivalent USD estimate. Main may read itself or a deterministic Child of its task;
  this is stateless observability, never workflow authority or a ChatGPT subscription invoice.

Capabilities are compact opaque `evx1_` HMAC-SHA256 references. They bind the owning Main, Child, task key, role,
allowed action, and expiry. The server holds the OpenHands credential; it never appears in tool input,
tool output, or child environment variables. There is no persistent state: callers provide a stable
message key and the Main deduplicates semantic replays.

Each created Child receives a synchronous native Stop hook. The hook calls the private
`/completion-hook` endpoint with its scoped reference. Live Child event evidence makes it a successful
no-op after an accepted `send_to_parent`; otherwise it sends one stable `RECOVERY_WAKE` containing the
bounded terminal `FinishAction` response to the owning Main. Ordinary assistant text is not terminal
evidence, so a nonterminal Child cannot trigger a recovery wake. This is the provider-neutral fallback
when a model finishes without calling `send_to_parent`.
Repeated hooks reuse the same semantic message key and do not require a database, poller, read tool,
or receipt store.

The trusted Event Gateway/host mints the short-lived Main capability with
`main_capability_token(...)` and binds it to that Conversation's MCP transport as Bearer auth. Child
capabilities are bound the same way during provider admission. Agents never read, copy, or supply a
capability in tool arguments, and the MCP exposes no capability-minting tool.

The Parent creates or fetches only the persistent bare mirror at
`/home/openhands/workspace/mirrors/<owner>--<repository>.git`. `create_child` derives
`/home/openhands/workspace/delivery/child-<child UUID>`, creates the exact branch worktree when absent,
and validates repository, origin, branch, head, and cleanliness before touching the Conversation API.
It never clones/fetches remotely, deletes, resets, or repurposes an existing worktree.

## Run

The in-cluster server uses Streamable HTTP; the trusted host supplies the per-Conversation Bearer
capability in MCP configuration:

```sh
export EVEX_MESSAGING_SECRET='long-random-secret'
export OPENHANDS_URL='http://openhands:8000'
export OPENHANDS_API_KEY='server-only-key'
export OPENHANDS_PUBLIC_URL='http://openhands.example/canvas'
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
