# ADR 0001 — User-scoped tools via closure factory

**Date:** 2026-05-10
**Status:** Accepted
**Slice:** 2

## Context

The trip-tracker agent has seven CRUD tools that all operate on a per-user
`Watches` table: `add_watch`, `list_watches`, `update_watch`, `pause_watch`,
`resume_watch`, `remove_watch`, `get_fare_history`. Every one of them needs
the caller's `userId` (Cognito `sub`) to scope reads and writes correctly.

The naive design — declaring each tool with a `userId: str` parameter and
relying on the agent to fill it in from the request — is unsafe in two ways:

1. **The LLM is now an authority on identity.** A prompt-injected instruction
   inside any tool result, MCP response, or even a malicious user message
   could persuade the model to call `update_watch(userId="someone-else", ...)`.
   Every tool result and every external string becomes part of the trust
   boundary for *who you are*.
2. **The schema documents an exfiltration path.** The very fact that `userId`
   appears in the tool surface invites the model to treat it as a tunable
   parameter rather than a fixed identity.

We need a pattern where the verified identity is bound to the tools at
request time and cannot be changed by anything the LLM does or sees.

## Decision

The watch CRUD tools are built per request via a factory function:

```python
# watches.py
def make_watch_tools(user_id: str) -> list:
    @tool(name="add_watch", ...)
    def add_watch(origin, destination, ...):
        return create_watch(user_id=user_id, origin=origin, ...)
    # ... six more, each closing over user_id
    return [add_watch, list_watches_tool, update_watch_tool, ...]
```

`agent.py` calls `make_watch_tools(user.id)` *after* JWT verification and
passes the resulting list into `Agent(tools=...)`. `user_id` is captured in
each closure's lexical scope, so it never appears in the tool's JSON schema
and the LLM has no way to address it as an argument.

Ownership is also enforced a second time at the data layer: every function
in `watches.py` keys reads and writes on `(userId, watchId)`. A fabricated
`watchId` belonging to another user simply returns no row — there's no code
path that accepts a `watchId` without also requiring the matching `userId`.
For `FareHistory` (whose partition key is only `watchId`), the data-access
function calls `get_watch(user_id, watch_id)` first, returning early if the
ownership check fails.

Tests in `tests/test_watches.py` cover both layers: the tool-factory layer
asserts `user_id` does not appear in any tool's input schema; the data layer
asserts cross-user reads and writes return `None` / empty.

## Consequences

**Good:**
- The LLM's authority over user identity is zero. Even a perfectly-engineered
  prompt-injection can't escalate to another user's data because the
  parameter doesn't exist in the tool surface.
- The fix is local and small. No new framework, no middleware, no central
  policy engine — just a function that returns functions.
- Two layers of defense: the closure removes the parameter; the DDB key
  rejects the operation. Either failure mode alone is enough.
- The `tools.py` module keeps the simple module-level pattern for tools that
  genuinely don't depend on identity (`get_user_location`, `get_todays_date`).
  The split makes "which tools have access to which data" obvious at a glance.

**Cost:**
- Tools are constructed per request rather than at module load. At Lambda
  request rates (one per chat turn), this is invisible — small object
  allocations, no I/O.
- Anyone adding a new user-scoped tool has to know to add it inside
  `make_watch_tools`, not at module scope. The split file (`watches.py` vs
  `tools.py`) is the convention that signals this; the ADR is the
  documentation.

**What we're explicitly not doing:**
- Not relying on the LLM to "do the right thing" via prompt instructions.
  Prompts that tell a model "always pass the current user's id" are not
  security; they're suggestions.
- Not putting `userId` in the tool schema with a runtime validator that
  rewrites it. That works but leaves the parameter visible — a less
  defensible design when the schema is the contract a reviewer reads first.

## References
- Production-readiness companion spec §3.4
- Design spec §4 (tool surface)
- `lambdas/travel-agent/watches.py`
- `lambdas/travel-agent/tests/test_watches.py`
