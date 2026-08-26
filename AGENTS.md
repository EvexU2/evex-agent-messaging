# EVEX Agent Messaging

- Keep the MCP provider-neutral; OpenHands details belong only in `provider.py`.
- No database, queue, ledger, poller, or workflow state.
- Every child operation requires an HMAC capability bound to Main, Child, task, target, and action.
- Use the standard-library implementation unless a dependency is strictly necessary.
- Run `python3 -m unittest discover -s tests -p 'test_*.py' -q` before handoff.

## EVEX-only skill policy — hard stop

Use only `evex-*` skills. Before any work, identify the required EVEX skill. If no matching EVEX skill exists, or any non-EVEX skill would be needed: **FULL STOP. Do nothing further. Ask a human for direction.** Never substitute, read, invoke, copy, or delegate to a non-EVEX skill.
