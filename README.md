# EVEX Agent Messaging Gateway

Provider-neutral MCP transport for Parent Main, Child Main, and Role Child conversation operations.
Agents call this MCP; they do not call OpenHands' Conversation API directly.

## Tools

- `create_child_conversation`: create one deterministic Child Conversation from a signed Parent capability.
- `send_agent_message`: send one bounded `RESULT`, `NEEDS_INPUT`, or control message through the owning tree.
- `cancel_agent_mission`: interrupt a Child and deliver the bound cancellation message.

Capabilities are self-contained HMAC-SHA256 tokens. They bind the owning Main, Child, task key, role,
allowed action, and expiry. The server holds the OpenHands credential; it never appears in tool input,
tool output, or child environment variables. There is no persistent state: callers provide a stable
message key and the Main deduplicates semantic replays.

The trusted Event Gateway/host mints the short-lived Main capability with
`main_capability_token(...)` and injects it into the Main Mission. The MCP does not expose a capability
minting tool to agents.

## Run

The server uses MCP stdio framing (`Content-Length` headers):

```sh
export EVEX_MESSAGING_SECRET='long-random-secret'
export OPENHANDS_URL='http://openhands:8000'
export OPENHANDS_API_KEY='server-only-key'
export OPENHANDS_PUBLIC_URL='http://openhands.example/canvas'
PYTHONPATH=src python3 -m evex_agent_messaging
```

Only the adapter reads the OpenHands variables. A fake provider can be injected in tests or by a host
embedding the service, so skills and Missions remain runtime-neutral.

## Validation

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -q
```
