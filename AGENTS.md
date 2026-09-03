# EVEX Agent Messaging

- Keep the public MCP and private Gateway delivery contract provider-neutral; OpenHands details belong
  only in `provider.py`.
- Messaging owns Main admission, create/reconcile/verify/wake behavior and only short, safe provider
  GET retries. The Gateway owns GitHub routing and its bounded process-local delivery retry.
- No database, queue, ledger, poller, or workflow state.
- Only Issue Main may create or reuse its one deterministic Spec Chat. Specialist Conversations stay
  controlled through the runtime's native task lifecycle and may send only one result to their exact
  owning coordinator through the existing Messaging route.
- Every lifecycle or message operation requires an HMAC capability bound to Main, sender, task, and exact action.
- Authorize the requested target from exact sender/target Discussion facts; never add inventory search.
- Keep `POST /internal/deliver-main` private and out of `tools/list`; authenticate its dedicated
  Gateway credential before parsing its bounded exact JSON body.
- Use the standard-library implementation unless a dependency is strictly necessary.
- Run `python3 -m unittest discover -s tests -p 'test_*.py' -q` before handoff.

## EVEX-only skill policy — hard stop

Use only `evex-*` skills. Before any work, identify the required EVEX skill. If no matching EVEX skill exists, or any non-EVEX skill would be needed: **FULL STOP. Do nothing further. Ask a human for direction.** Never substitute, read, invoke, copy, or delegate to a non-EVEX skill.
