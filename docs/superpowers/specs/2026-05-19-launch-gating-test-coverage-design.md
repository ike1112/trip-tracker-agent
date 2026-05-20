# Launch-gating test coverage — design

**Date:** 2026-05-19
**Status:** draft, awaiting implementation plan

## Goal

Add launch-gating test coverage to the five untested boundaries where a defect
would block production launch:

1. JWT authentication of agent calls (Node TOKEN authorizer)
2. JWT re-validation inside the Python Lambda handler
3. User identity threading from JWT claims to agent invocation
4. Custom tools exposed to the agent
5. Web OAuth flow and agent proxy

The work follows **hybrid TDD**: characterization tests lock current behavior on
already-working paths; net-new tests for risk surfaces follow strict
red → green → refactor. Where a test reveals a real bug, the same change
adds the failing test and the source fix.

## In scope

| # | Component | Runtime | Why it gates launch |
|---|---|---|---|
| 1 | `lambdas/agent-authorizer/index.js` | Node | API Gateway TOKEN authorizer that protects the agent endpoint. If broken, anyone with a forged token can reach the agent. |
| 2 | `lambdas/travel-agent/app.py` | Python | Lambda entrypoint. Re-validates the JWT (defense in depth), builds the prompt, calls `agent.prompt`. The user-facing 401/200 contract. |
| 3 | `lambdas/travel-agent/user.py` | Python | Identity data class. Locks the contract between `claims["sub"]` / `claims["username"]` and the rest of the agent stack. |
| 4 | `lambdas/travel-agent/tools.py` | Python | `get_user_location`, `get_todays_date`. The model-visible toolbox at the boundary. |
| 5 | `web/oauth.py` + `web/app.py` | Python | Cognito OAuth flow (`/login`, `/callback`, `/logout`), session auth dependency, agent proxy. The whole user-visible login + chat path. |

## Out of scope

Deliberate, with rationale. Each item below has been considered and excluded.

- **MCP servers (`flights-mcp`, `hotels-mcp`) live clients, transport shims,
  `get-*-details` tools.** Search tools already tested via fixture clients;
  these defer to a follow-up spec because they're not on the critical user
  path at launch.
- **CDK construct tests for `cognito.js`, `flights-mcp-server.js`,
  `hotels-mcp-server.js`, `poller-server.js`.** Synth-time correctness is
  verified at deploy + canary; not blocking for first users.
- **`lambdas/travel-agent/agent.py`, `agent_config.py`, `mcp_client_manager.py`.**
  The agent loop and MCP wiring. Real verification happens via evals (already
  covered) and live canaries, not unit tests against a hallucination engine.
- **Coverage percentages.** Tests target launch-gating boundaries, not lines.
- **End-to-end flow tests** across auth → agent → tools → DynamoDB. Those are
  runbook-driven (`docs/e2e-test-runbook.md`).
- **Performance / load tests.** Observability dashboard catches regressions
  in prod.

### Follow-up bugs to file separately (surfaced during this design)

Listed here so they aren't lost when this spec ships:

- `lambdas/travel-agent/tools.py` — `urllib.request.urlopen` to ip-api.com
  has no `timeout=` argument. A hung ip-api request can wedge the Lambda for
  the full 15-minute timeout. **Medium severity.** One-line fix.
- `web/oauth.py` — `/callback` crashes if the `code` query param is missing.
  Should return 400 or redirect to `/login`. Low severity.
- `web/app.py` — `chat()` propagates `httpx.TimeoutException` and
  `httpx.ConnectError` to the user as a crash instead of returning a
  graceful error string. Low-medium severity.

## Test conventions

Follow existing repo patterns. Do not invent new ones.

### Node — `node --test` (not jest)

All Lambda Node packages use the built-in Node test runner with this script:

```
"test": "node --import ./tests/setup.js --test --test-reporter=spec \"tests/*.test.js\""
```

`lambdas/agent-authorizer/` matches `lambdas/mcp-authorizer/` verbatim. Jest is
reserved for root-level CDK construct tests in `test/`.

### Authorizer test seam

`lambdas/mcp-authorizer/index.js` already exports `__seedSecretCacheForTests`
so tests can inject keys without touching AWS. Add the analogous
`__setSigningKeyForTests(publicKey)` export to `lambdas/agent-authorizer/index.js`.
Tests generate an RSA keypair with
`node:crypto.generateKeyPairSync('rsa', { modulusLength: 2048 })`, seed the
public key via the test seam, then sign tokens with the private key.

### Python — `pytest` per package

Run from each package directory: `cd lambdas/<pkg> && python -m pytest tests/ -q`.
Same pattern for the new `web/tests/`.

### Python `app.py` import-chain mitigation

`lambdas/travel-agent/app.py` imports `agent`, which imports `strands`,
constructs a `BedrockModel`, and reads `SESSION_STORE_BUCKET_NAME` from env.
Tests must not exercise the real Strands stack.

Pattern: in `conftest.py`, **before** importing `app`, inject a stub `agent`
module into `sys.modules` with `prompt = MagicMock()`. Then set
`COGNITO_JWKS_URL`, `WATCHES_TABLE_NAME`, `FARE_HISTORY_TABLE_NAME` env vars
and import `app` fresh inside the fixture. This mirrors the existing
`watches_module` import-reload pattern at
`lambdas/travel-agent/tests/conftest.py:35-66`.

Patch `app.jwks_client.get_signing_key_from_jwt` to return the test public
key.

### RSA keypair fixtures

Per language, not shared across runtimes.

- **Python:** pytest session-scoped fixture using
  `cryptography.hazmat.primitives.asymmetric.rsa`. Already in
  `requirements-test.txt` (`cryptography==45.0.4`).
- **Node:** generated once in `tests/setup.js`, exposed via
  `globalThis.__testKeys`. `jsonwebtoken` already pulled in by other
  Node authorizers.

### FastAPI tests

Use `starlette.testclient.TestClient` against the assembled `fastapi_app`.
Mock the Authlib seams `oauth.cognito.authorize_redirect` and
`oauth.cognito.authorize_access_token`. Mock the outbound `chat()` call
with `responses` (already pinned). After the session-secret bug fix,
tests use a `TestClient` with a pre-signed session cookie produced via
`itsdangerous` (transitive of `starlette`).

**Authlib testability refactor.** `web/oauth.py` currently constructs
`oauth = OAuth()` as a local inside `add_oauth_routes()`. That makes
`oauth.cognito` unreachable from tests. Hoist `oauth = OAuth()` and the
`oauth.register(name="cognito", ...)` call to module scope so tests can
patch `web.oauth.oauth.cognito.authorize_redirect` and
`web.oauth.oauth.cognito.authorize_access_token`. Behavior is identical
(the module is imported once per process); only the lexical scope of the
`oauth` variable moves.

### `web/` deployment

`web/` is a standalone Docker container (`web/Dockerfile`). It is **not**
CDK-deployed. Bug fixes are pure env-var-driven config changes in
`web/app.py` (`SESSION_SECRET_KEY`) and `web/oauth.py`
(`OAUTH_CALLBACK_URI`, `OAUTH_POST_LOGOUT_URL`). Add a `web/.env.example`
documenting the new env vars; no `lib/` changes.

### `requirements-test.txt`

Already complete. Pins `httpx`, `responses`, `PyJWT`, `cryptography`,
`fastapi`, `starlette`, `python-multipart`, `python-dotenv`, `moto`.
Add zero new pins.

## File layout after the spec lands

```
lambdas/agent-authorizer/
  package.json                    # new — type: module, test script matches mcp-authorizer
  tests/setup.js                  # new — generates RSA keypair, sets env
  tests/handler.test.js           # new — A1..A10 matrix
  index.js                        # +__setSigningKeyForTests export
lambdas/travel-agent/tests/
  conftest.py                     # +rsa_keypair fixture, +stub-agent helper
  test_app.py                     # new
  test_user.py                    # new
  test_tools.py                   # new
web/
  .env.example                    # new — SESSION_SECRET_KEY, OAUTH_*
  app.py                          # bug fix: secret_key from env
  oauth.py                        # bug fix: URLs from env
  tests/__init__.py               # new
  tests/conftest.py               # new — TestClient + Authlib seams
  tests/test_oauth.py             # new
  tests/test_app.py               # new
.github/workflows/ci.yml          # +2 steps: agent-authorizer (node --test),
                                  #          web (pytest, working-directory: web)
```

## Per-component test plans

Tables below list every assertion. `Today` column: green ✅ today, or red ❌
because a source change is required. Red rows are TDD red → green → refactor.

### 1. `lambdas/agent-authorizer/index.js`

| # | Assertion | Type | Today |
|---|---|---|---|
| A1 | Valid RS256 token (matching `kid`) → `Effect=Allow`, `principalId=${sub}\|${username}` | characterization | ✅ |
| A2 | Missing `event.authorizationToken` → `Deny` | characterization | ✅ |
| A3 | Authorization header lacking `Bearer ` prefix → `Deny` | characterization | ✅ |
| A4 | Expired token (`exp` in past) → `Deny` | TDD | ✅ |
| A5 | `alg: none` forged token → `Deny` | TDD | ✅ |
| A6 | HS256 token using public key as HMAC secret (algorithm-confusion attack) → `Deny` | TDD | ✅ |
| A7 | Token `kid` not in JWKS → `Deny` | TDD | ✅ |
| A8 | JWKS fetch throws (network error) → `Deny`, fail-closed | TDD | ✅ |
| A9 | Token signed by a different RSA keypair → `Deny` | TDD | ✅ |
| A10 | Deny response has no `principalId` field (don't leak attacker identity into IAM logs) | TDD | ✅ |

### 2. `lambdas/travel-agent/app.py`

| # | Assertion | Type | Today |
|---|---|---|---|
| B1 | Valid JWT + valid body → `agent.prompt(user, composite_prompt)` called once; returns `200, {"text": …}` | characterization | ✅ |
| B2 | Missing Authorization header → `401 Unauthorized` | characterization | ✅ |
| B3 | Malformed Authorization header (no Bearer) → `401` | characterization | ✅ |
| B4 | `composite_prompt` contains `User name:`, `User IP:`, `User prompt:` lines in exact order (lock the format) | characterization | ✅ |
| B5 | `User(id=…, name=…)` is built from `claims["sub"]` and `claims["username"]` — not other claims (multi-tenancy assertion: prompt injection cannot substitute identity by sending a different `username` claim) | characterization | ✅ |
| B6 | Expired token → `401` | TDD | ✅ |
| B7 | Token signed by wrong key → `401` | TDD | ✅ |
| B8 | HS256 token → `401` (PyJWT pin: `algorithms=["RS256"]`) | TDD | ✅ |
| B9 | Malformed JSON in `event["body"]` → `400 Bad Request` | TDD | ❌ fix |
| B10 | Missing `"text"` key in body → `400 Bad Request` | TDD | ❌ fix |
| B11 | Missing `requestContext.identity.sourceIp` → `400 Bad Request` | TDD | ❌ fix |

**Source change for B9–B11:** wrap the body-parsing + sourceIp-access block
in `try/except (json.JSONDecodeError, KeyError)`, return
`{"statusCode": 400, "body": "Bad Request"}`. Three lines.

### 3. `lambdas/travel-agent/user.py`

| # | Assertion | Type | Today |
|---|---|---|---|
| C1 | `User(id="abc", name="alice")` stores both attributes | characterization | ✅ |
| C2 | `id` and `name` accept any string (no validation) — locks current behavior so a future "add validation" change shows up as a test diff | characterization | ✅ |

### 4. `lambdas/travel-agent/tools.py`

| # | Assertion | Type | Today |
|---|---|---|---|
| D1 | `get_user_location("1.2.3.4")` with mocked `urlopen` returning `{"city":"X","region":"Y","country":"Z"}` → returns `"X Y, Z"` | characterization | ✅ |
| D2 | `get_user_location` propagates JSON-decode errors (lock current behavior) | characterization | ✅ |
| D3 | `get_user_location` propagates `KeyError` on missing city/region/country | characterization | ✅ |
| D4 | `get_todays_date()` returns ISO date matching frozen `datetime.today()` (monkeypatch) | characterization | ✅ |

### 5. `web/oauth.py`

| # | Assertion | Type | Today |
|---|---|---|---|
| E1 | `GET /login` → redirect to Cognito authorize URL (via mocked `authorize_redirect`) | characterization | ✅ |
| E2 | `GET /callback` with valid code → session has `access_token` + `username`, redirects to `/chat` | characterization | ✅ |
| E3 | `GET /logout` → session cleared, redirects to Cognito logout URL with `logout_uri` param | characterization | ✅ |
| E4 | `OAUTH_CALLBACK_URI` env var drives the callback URL — not hard-coded `localhost:8000/callback` | TDD | ❌ fix |
| E5 | `OAUTH_POST_LOGOUT_URL` env var drives the post-logout URL — not hard-coded `localhost:8000/chat` | TDD | ❌ fix |

**Source change for E4–E5:** replace the two hard-coded string literals in
`web/oauth.py` with `os.environ["OAUTH_CALLBACK_URI"]` and
`os.environ["OAUTH_POST_LOGOUT_URL"]`. Fail-fast on missing env. Document
in `web/.env.example`.

### 6. `web/app.py`

| # | Assertion | Type | Today |
|---|---|---|---|
| F1 | `check_auth` with missing session → raises `HTTPException(302, Location=/login)` | characterization | ✅ |
| F2 | `check_auth` with valid session → returns `username` | characterization | ✅ |
| F3 | `chat()` with agent 200 → returns `agent_response.json()["text"]` | characterization | ✅ |
| F4 | `chat()` with agent 401/403 → returns the "Try to re-login" string | characterization | ✅ |
| F5 | `chat()` with agent 5xx → returns the generic "Failed to communicate" string | characterization | ✅ |
| F6 | `chat()` forwards exactly `request.request.session["access_token"]` as the Bearer token — no cross-session leak (multi-tenancy assertion) | characterization | ✅ |
| F7 | `SessionMiddleware` key comes from `SESSION_SECRET_KEY` env var — not literal `"secret"` | TDD | ❌ fix |
| F8 | App import raises `RuntimeError` if `SESSION_SECRET_KEY` is unset, empty, or equal to the literal `"secret"` | TDD | ❌ fix |

**Source change for F7–F8:** replace `SessionMiddleware(secret_key="secret")`
with a guarded read:

```python
_session_key = os.environ.get("SESSION_SECRET_KEY", "").strip()
if not _session_key or _session_key == "secret":
    raise RuntimeError(
        "SESSION_SECRET_KEY env var is required and must not be the placeholder 'secret'"
    )
fastapi_app.add_middleware(SessionMiddleware, secret_key=_session_key)
```

The explicit guard catches all three failure modes (missing, empty, placeholder)
and produces a clear error at import time. Document `SESSION_SECRET_KEY` in
`web/.env.example`.

## CI integration

Two new steps in `.github/workflows/ci.yml`:

```yaml
# under node-tests:
- name: agent-authorizer tests
  run: npm install && npm test
  working-directory: lambdas/agent-authorizer

# under python-tests:
- name: web
  run: python -m pytest tests/ -q
  working-directory: web
```

No new pinned dependencies. The root `requirements-test.txt` already
covers `web/`'s test needs.

## Sequencing and PR shape

Five PRs, each independently mergeable, each green on merge. Each PR owns
one component plus any source fixes that component's tests demand.

| PR | Adds | Source changes | Fixes bugs |
|---|---|---|---|
| 1 | `agent-authorizer/tests/` + CI step | `__setSigningKeyForTests` test seam in `index.js` | none (A1–A10 all green today) |
| 2 | `travel-agent/tests/test_app.py` + JWT fixture in `conftest.py` | `app.py` handler robustness | B9, B10, B11 |
| 3 | `web/tests/test_oauth.py` + new CI step + `web/.env.example` | `oauth.py` reads `OAUTH_CALLBACK_URI` and `OAUTH_POST_LOGOUT_URL` from env | E4, E5 |
| 4 | `travel-agent/tests/test_user.py` + `test_tools.py` | none | none |
| 5 | `web/tests/test_app.py` | `app.py` reads `SESSION_SECRET_KEY` from env, fail-fast if missing | F7, F8 |

**Cross-PR invariant:** PR 1 and PR 2 both write JWT failure-matrix tests
against the same threat model (HS256 attack, expired, etc.). Divergence
between the Node authorizer and the Python handler is detected if PR 2's
matrix passes the same shape as PR 1's. Reviewers should diff the two test
files side-by-side.

## Risks

- **Test fragility — RSA keypair regeneration per session.** Deterministic
  per test run, non-deterministic across runs. No assertion depends on key
  bytes, only on the sign → verify round-trip. Safe.
- **Test fragility — Authlib internal API drift.** Tests mock
  `oauth.cognito.authorize_redirect` / `authorize_access_token`. If Authlib
  renames these in a future version, tests break. Acceptable; Authlib is
  pinned transitively.
- **`sys.modules` stub-injection pattern** requires careful ordering in
  `conftest.py`. The existing `watches_module` fixture already uses this
  pattern successfully — we follow it.

## What this spec deliberately does not promise

- Line-coverage percentages
- End-to-end flow tests across auth → agent → tools → DynamoDB
- Performance / load tests
- Coverage for components listed in "Out of scope" above

## Acceptance criteria

- All 40 assertions from the tables above are implemented and green in CI
  (A: 10, B: 11, C: 2, D: 4, E: 5, F: 8).
- `.github/workflows/ci.yml` runs `agent-authorizer` and `web` on every PR.
- The four pre-approved bug fixes (E4, E5, F7, F8) and the three
  handler-robustness fixes (B9, B10, B11) land in source alongside their
  failing tests.
- `web/.env.example` documents `SESSION_SECRET_KEY`, `OAUTH_CALLBACK_URI`,
  `OAUTH_POST_LOGOUT_URL`.
- The three deferred bugs (D5, E6, F9) are filed as follow-up issues
  before this spec is marked complete.
- No new pinned dependencies in `requirements-test.txt`.
