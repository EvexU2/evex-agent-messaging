# EVEX Agent Messaging

- Keep the MCP provider-neutral; OpenHands details belong only in `provider.py`.
- No database, queue, ledger, poller, or workflow state.
- Only Parent Main may create or reuse its one deterministic Spec Chat. Specialist Conversations stay
  controlled through the runtime's native task lifecycle and may send only one result to their exact
  owning coordinator through the existing Messaging route.
- Every lifecycle or message operation requires an HMAC capability bound to Main, sender, task, and exact action.
- Authorize the requested target from exact sender/target Discussion facts; never add inventory search.
- Use the standard-library implementation unless a dependency is strictly necessary.
- Run `python3 -m unittest discover -s tests -p 'test_*.py' -q` before handoff.

## EVEX-only skill policy — hard stop

Use only `evex-*` skills. Before any work, identify the required EVEX skill. If no matching EVEX skill exists, or any non-EVEX skill would be needed: **FULL STOP. Do nothing further. Ask a human for direction.** Never substitute, read, invoke, copy, or delegate to a non-EVEX skill.
