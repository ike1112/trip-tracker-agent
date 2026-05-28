# Launch-gating follow-up bugs

**Date:** 2026-05-19
**Origin:** surfaced during the launch-gating test coverage spec
(`docs/superpowers/specs/2026-05-19-launch-gating-test-coverage-design.md`)
and intentionally deferred from that spec's scope.

Each item is a real bug with a small fix. Each was deferred because the
launch-gating spec was scoped tight to one design pass. Filing them here
so they aren't lost.

---

## Bug 1 — `get_user_location` has no `urlopen` timeout

**Severity:** medium

**File:** `lambdas/travel-agent/tools.py`

**What's wrong.** `get_user_location` calls
`urllib.request.urlopen(f"http://ip-api.com/json/{ip}")` with no `timeout=`
argument. If ip-api.com is unreachable or slow, the call blocks until
the Lambda's 15-minute timeout fires. One slow upstream wedges the entire
invocation. A travel-agent user who triggers `get_user_location` (e.g.
asks "what trips can I take from here?") sees their chat hang.

**Fix.** Add a timeout to the `urlopen` call:

```python
resp = request.urlopen(f"http://ip-api.com/json/{ip}", timeout=5).read()
```

5 seconds is a reasonable upper bound for a public IP-geolocation service.

**Test to write.** A test that patches `urlopen` to verify it was called
with `timeout=5` (or whatever value is chosen). Catches future regressions.

---

## Bug 2 — `/callback` crashes when `code` query param is missing

**Severity:** low

**File:** `web/oauth.py`

**What's wrong.** The `/callback` route calls
`await oauth.cognito.authorize_access_token(req)` which raises if the
expected `code` query param is missing or malformed. The exception is
unhandled, so the user sees a 500 instead of a structured 400 or a
redirect back to `/login`.

**Fix.** Wrap the token-exchange call in `try/except` and either return a
400 with a small explanation or redirect to `/login`:

```python
@fastapi_app.get("/callback")
async def callback(req: Request):
    try:
        tokens = await oauth.cognito.authorize_access_token(req)
    except Exception:
        # Bad/missing code, expired authorization, or replay attempt.
        # Send the user back to /login rather than 500.
        return RedirectResponse(url="/login")
    # ...rest unchanged
```

**Test to write.** A test that GETs `/callback` with no `code` and
asserts a 302 redirect to `/login`, not a 500.

---

## Bug 3 — `chat()` crashes on outbound `httpx` timeout

**Severity:** low-medium

**File:** `web/app.py`

**What's wrong.** `chat()` calls `httpx.post(...)` with `timeout=30`.
If the agent Lambda exceeds the timeout (cold start + slow Bedrock = real
risk), `httpx.TimeoutException` propagates as an unhandled exception
through Gradio. The user sees a generic Gradio error overlay, not a
graceful "the agent is slow, try again" message.

**Fix.** Wrap the `httpx.post` in `try/except` for `httpx.TimeoutException`
and `httpx.ConnectError`, returning a graceful string:

```python
import httpx as _httpx_mod  # for the exception classes

def chat(message, history, request: gr.Request):
    # ... existing setup ...
    try:
        agent_response = httpx.post(...)
    except _httpx_mod.TimeoutException:
        return "The agent took longer than expected to respond. Try again in a moment."
    except _httpx_mod.ConnectError:
        return "Could not reach the agent right now. Try again in a moment."
    # ... existing status_code branches ...
```

**Test to write.** Two tests that patch `httpx.post` to raise
`httpx.TimeoutException` and `httpx.ConnectError` respectively, asserting
the graceful strings are returned (not raised).
