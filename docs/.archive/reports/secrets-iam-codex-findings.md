# Codex adversarial review — `tasks/secrets-and-iam-hardening.prp.md`

Source: Codex CLI 0.130.0, adversarial plan-review mode, run 2026-05-15 against
commit `93777c1` (PRP add). 530k tokens. Codex read the live repo files and was
told NOT to trust the PRP's file/line claims.

Each finding below carries Codex's claim plus a first-pass verification note
(checked by the orchestrating session). A separate Claude reviewer must
independently re-verify each one against the codebase and classify it
VALID / PARTIAL / INVALID before any PRP revision is implemented.

User decisions already taken:
- Bedrock geographic-profile ARN scope (finding #4): **enumerate the 3 US
  destination regions** (us-east-1, us-east-2, us-west-2) + the inference-profile
  ARN, derived from the `us.` prefix.
- Verifier-structure question (finding #1): deferred into this verify-then-
  implement loop — the reviewer should assess whether a shared verifier module
  vs triplication vs authorizer-only is the right call and recommend one.

---

## Finding 1 — HIGH — PRP Locked Decision #8 is false (server Lambdas ARE verifiers)

**Codex claim:** PRP §9 Locked Decision #8 and §10 state the flights/hotels MCP
*server* Lambdas do not consume `JWT_SIGNATURE_SECRET` ("verified by grep").
Both server handlers independently re-verify the Bearer JWT in-handler, so
dropping the env var per Task 5 makes the authorizer pass a request the handler
then 401s.

**First-pass verification (CONFIRMED):**
- `lambdas/flights-mcp/index.js:23` — `const JWT_SIGNATURE_SECRET = process.env.JWT_SIGNATURE_SECRET;`
- `lambdas/flights-mcp/index.js:56` — `claims = jwt.verify(token, JWT_SIGNATURE_SECRET);`
- `lambdas/hotels-mcp/index.js:22,55` — identical shape.
- `lambdas/flights-mcp/index.js:50` carries the comment "Same pattern as travel-agent/app.py" — the in-handler verify is intentional defense-in-depth, not dead code.
- PRP §9 Locked Decision #8's "verified by grep" claim is factually wrong.

**Impact:** Stack-breaking. Every flights/hotels MCP call would 401 after deploy.

**Resolution direction:** The two-secret + sub-coupling verifier must also apply
to both server handlers, not just `lambdas/mcp-authorizer/index.js`. This
reverses Locked Decision #8 and the §10 "no shared JWT module" exclusion is now
in tension with DRY-ing security-critical logic across 3 files. Reviewer to
recommend: shared verifier module vs triplicate vs authorizer-only (the last is
a security regression and disfavored).

PRP sections to change: §3, §8 (Task 5), §9 (Decision #8), §10, §11 (Group F).

---

## Finding 2 — HIGH — `lambdas/travel-agent/app.py` left with a dangling env read

**Codex claim:** PRP drops `JWT_SIGNATURE_SECRET` from the agent Lambda but never
touches `app.py`, which reads it at import → cold-start `KeyError`.

**First-pass verification (CONFIRMED, with nuance):**
- `lambdas/travel-agent/app.py:33` — `JWT_SIGNATURE_SECRET = os.environ['JWT_SIGNATURE_SECRET']` at module scope.
- `app.py:41,51` — actual inbound verification is `jwt.PyJWKClient` + `jwt.decode(..., algorithms=["RS256"])` (Cognito JWKS). The HS256 `JWT_SIGNATURE_SECRET` is **never used** in app.py — it is a dead import-time read.
- So the fix is simpler than Codex framed: **delete line 33**, no migration. app.py is not a 4th HS256 verifier.

PRP sections to change: §6 (mandatory reading), §8 (Task 8 — add app.py: delete the dead line).

---

## Finding 3 — HIGH — Python test-mock strategy contradicts import-time fetch

**Codex claim:** PRP §7/§8 specifies a module-load `get_secret_value()` in
`jwt_signer.py`, but the test strategy `monkeypatch.setattr(jwt_signer,
"_secrets", FakeSecretsClient(...))` runs *after* import — the fetch already
fired. `lambdas/poller/tests/test_jwt_signer.py:25-31` (`_fresh_signer`) and
`lambdas/poller/tests/conftest.py:224,236` only set env before importing.

**First-pass verification (CONFIRMED as a design contradiction in the PRP):**
The PRP's own §7 "MODULE-LOAD SECRET FETCH (Python)" block runs the fetch at
import; §8 Task 7's mocking note patches the client attribute post-import. These
cannot both hold.

**Resolution direction:** Switch the secret-fetch pattern from import-time to
lazy + cached-on-first-use (a `_get_secret()` helper memoizing the value), so a
test can patch the boto3 client before the first `sign_for_user()` call. Same
change applies to the Node pattern (§7 "MODULE-LOAD SECRET FETCH (Node)").

PRP sections to change: §7 (both patterns), §8 (Tasks 6, 7, 8), §11 (Group E).

---

## Finding 4 — HIGH — Agent Bedrock IAM grant insufficient for a geographic profile

**Codex claim:** Agent model `us.anthropic.claude-3-5-haiku-20241022-v1:0`
(`agent_config.py:10`) is a *geographic* cross-region inference profile. AWS
requires the inference-profile ARN PLUS the foundation-model ARN in every
destination region. PRP grants only one foundation-model ARN in `${region}`.
The poller's pattern (`lib/poller-server.js:141`) is single-region but the
poller's model (`claude-haiku-4-5-20251001`, `poller-server.js:10`) is NOT a
`us.`-prefixed profile, so the poller pattern does not transfer to the agent.

**First-pass verification (CONFIRMED):**
- `agent_config.py:10` model id is `us.`-prefixed (geographic profile).
- `lib/poller-server.js:10` default is `claude-haiku-4-5-20251001` (plain
  foundation model, not geographic). The poller's single-ARN grant is correct
  *for the poller* but copying it to the agent under-scopes the agent grant.

**Resolution (user-decided):** Enumerate the 3 US destination regions
(us-east-1, us-east-2, us-west-2). The grant becomes: inference-profile ARN +
foundation-model ARN in each of the 3 US regions, all derived from the `us.`
prefix on the model id. A non-US profile would need a code change (acceptable;
documented as a locked decision).

PRP sections to change: §3B, §7, §8 (Task 3), §9 (new locked decision), §12 (Gate 5).

---

## Finding 5 — MED — Poller integration mocks pin `sub == "travel-agent"`

**Codex claim:** Flipping the poller `sub` to `trip-tracker-poller` breaks
poller integration tests whose mock authorizers reject any other sub. PRP only
calls out `test_jwt_signer.py`.

**First-pass verification (CONFIRMED):**
- `lambdas/poller/tests/test_e2e_poll.py:51` — `if claims.get("sub") != "travel-agent":`
- `lambdas/poller/tests/test_handler_with_mcp.py:62` — same check; `:192` asserts `all(c["sub"] == "travel-agent" ...)`; `:306,331` a dedicated reject-non-travel-agent test.

**Resolution direction:** PRP §8 Task 7 + §11 Group E + Gate 2 expand to update
every poller mock authorizer + assertion to expect `trip-tracker-poller` under
the poller secret.

PRP sections to change: §8 (Task 7), §11 (Group E), §12 (Gate 2).

---

## Finding 6 — MED — Gate 3 uses the wrong test runner for flights/hotels

**Codex claim:** flights/hotels packages declare
`"test": "node --import ./tests/setup.js --test ..."` (Node built-in runner).
PRP Gate 3 runs `npx jest lambdas/flights-mcp/tests/ ...`, which won't apply the
`--import ./tests/setup.js` env setup.

**First-pass verification (CONFIRMED):**
- `lambdas/flights-mcp/package.json:8` and `lambdas/hotels-mcp/package.json:8`
  both: `node --import ./tests/setup.js --test --test-reporter=spec "tests/*.test.js"`.

**Resolution direction:** Split Gate 3 — root CDK tests via jest; flights/hotels
via `npm test` (node --test) run inside each package; mcp-authorizer gets its
own explicit test script + setup with the two-secret mocks.

PRP sections to change: §12 (Gate 3).

---

## Finding 7 — LOW — Gate 5 cannot catch findings #1 or #4

**Codex claim:** Gate 5 asserts only "2 secrets / no old env var / no Bedrock
wildcard" — it would not catch a server Lambda left without a usable verifier
secret, nor an incomplete cross-region Bedrock ARN set.

**First-pass verification (CONFIRMED, follows from #1 + #4).**

**Resolution direction:** Extend Gate 5 to (a) assert the flights/hotels server
Lambdas carry the two secret-ARN env vars (post-#1 fix) and (b) assert the agent
Bedrock policy contains the full expected ARN set (3 foundation-model regions +
profile).

PRP sections to change: §12 (Gate 5), §12 (Gate 7 unaffected).

---

## Independent Claude verification verdict (agent-skills:code-reviewer, Sonnet, 2026-05-15)

| # | Verdict | Note |
|---|---------|------|
| 1 | VALID | Both §9 Decision #8 AND §8 Task 5 GOTCHA falsely claim server Lambdas don't consume the secret. Stack-breaking. |
| 2 | PARTIAL | `app.py:33` dead read → cold-start `KeyError`; fix is a one-line delete. `mcp_client_manager.py:44` is the live HS256 minter (PRP §8 Task 8 already covers it). |
| 3 | VALID | Import-time fetch vs post-import `monkeypatch` is a genuine contradiction. Node form softer but same root issue. |
| 4 | VALID | `agent_config.py:10` is `us.`-prefixed (geographic); poller's `claude-haiku-4-5-20251001` is not. Single-region ARN insufficient. |
| 5 | VALID | `test_e2e_poll.py:51`, `test_handler_with_mcp.py:62,192,305-335` pin `travel-agent`. PRP names only `test_jwt_signer.py`. |
| 6 | VALID | flights/hotels use `node --import ./tests/setup.js --test`; mcp-authorizer has no test script. Gate 3 jest invocation wrong. |
| 7 | VALID | Gate 5 cannot catch #1 or #4. |
| 8 | NEW | Even after Task 5 adds secret ARNs to the *authorizer*, server *handler* code (`flights/hotels index.js:23,56`) reads `process.env.JWT_SIGNATURE_SECRET` at module scope and passes `undefined` to `jwt.verify` → 401s all requests. Task 5 + Task 9 never update the server handler verify logic. |

**Design decision (reviewer-recommended, adopted): triplicate the ~15-line
verifier** across mcp-authorizer + flights/hotels handlers. Rationale: §10's
"no shared JWT module" exclusion is correctly scoped — a cross-package shared
verifier needs a Lambda layer or monorepo symlink (larger than the security fix,
new failure surface). Authorizer-only is a deliberate security regression
(in-handler verify is documented defense-in-depth). Triplication's drift risk is
bounded by ~15 lines of stateless JWT logic with per-location Group D/F test
coverage that fails before production.

**Bedrock ARN scope (user-decided): enumerate us-east-1, us-east-2, us-west-2**
foundation-model ARNs + the inference-profile ARN, derived from the `us.` prefix.

## Net assessment

All 8 findings valid (one PARTIAL, narrower scope). The PRP needs a §0 review-
response table and body revisions across §1, §3, §6, §7, §8, §9, §10, §11, §12.
Decisions locked: triplicate verifier, 3-US-region Bedrock ARNs, lazy-cached
secret fetch (not import-time).
