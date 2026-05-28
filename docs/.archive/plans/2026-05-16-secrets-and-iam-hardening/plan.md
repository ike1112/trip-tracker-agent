# PRP: Per-component JWT secrets (ADR 0006) + agent Bedrock IAM tightening

**Confidence:** **8/10** for one-pass execution. Two work items both well-bounded against existing patterns — the poller's resource-scoped Bedrock grant is the direct mirror for the agent; the per-component JWT split is a small protocol change with a clean two-token-types verifier. Main uncertainty is the mcp-authorizer's currently-untested Lambda — this PRP adds the first test file for it, and the test matrix exercises both happy paths plus the cross-secret-rejection edge case.

---

## 0. Pre-implementation review response (Codex adversarial pass + independent Claude verification, 2026-05-15)

A Codex adversarial review (read the live repo, told not to trust the PRP's
file/line claims) surfaced 7 defects; an independent Claude reviewer
(`agent-skills:code-reviewer`) re-verified each against the codebase, downgraded
one to PARTIAL, and added an 8th. All 8 are verified true and the PRP body below
has been revised. The full evidence log lives at
`.claude/PRPs/reports/secrets-iam-codex-findings.md`.

| # | Sev | Finding (verified file:line) | Resolution | Sections touched |
|---|-----|------------------------------|------------|------------------|
| 1 | high | §9 Decision #8 AND §8 Task 5 GOTCHA falsely claim the flights/hotels MCP *server* Lambdas don't consume the JWT secret. They DO: `lambdas/flights-mcp/index.js:23,56` + `lambdas/hotels-mcp/index.js:22,55` `jwt.verify` in-handler as documented defense-in-depth. Dropping the env var per the original Task 5 → authorizer passes, handler 401s every request. | The two-secret + sub-coupling verifier applies to BOTH server handlers as well as the authorizer. Decision #8 reversed. Verifier is **triplicated** (copied, not shared) — §10's no-shared-module exclusion stays (a cross-package shared verifier needs a Lambda layer / monorepo symlink, larger than this fix). | §1; §3A; §8 Task 5; §9 (#8 rewritten); §10; §11 (Group F) |
| 2 | high (→ partial) | `lambdas/travel-agent/app.py:33` reads `os.environ['JWT_SIGNATURE_SECRET']` at import but app.py verifies via RS256/Cognito-JWKS (`app.py:41,51`) — the HS256 secret is a **dead read**. Original Task 8 never touches app.py → cold-start `KeyError`. | One-line delete of `app.py:33`. app.py is NOT a fourth verifier. (`mcp_client_manager.py:44` is the live agent minter; Task 8 already covers it.) | §6; §8 Task 8 |
| 3 | high | PRP §7 specifies import-time `get_secret_value()`; §8 Task 7 specifies a post-import `monkeypatch.setattr(jwt_signer, "_secrets", ...)`. The import-time fetch fires before any test patch (`test_jwt_signer.py:25-31`, `conftest.py:224,236`). Self-contradictory. | Switch both the Python and Node secret-fetch patterns from import-time to **lazy + cached on first use** (`_get_secret()` memo). Tests patch the client before the first `sign_for_user()` / first handler invocation. | §7 (both patterns); §8 Tasks 6,7,8; §9 (new decision); §11 Group E |
| 4 | high | Agent model `us.anthropic.claude-3-5-haiku-20241022-v1:0` (`agent_config.py:10`) is a **geographic** inference profile (routes us-east-1/2, us-west-2). PRP §3B grants only a single-`${region}` foundation-model ARN. Poller's single-region pattern is correct for the poller (`poller-server.js:10` model is not `us.`-prefixed) but does not transfer. Runtime `AccessDeniedException` on cross-region routing. | Enumerate the 3 US destination-region foundation-model ARNs (us-east-1, us-east-2, us-west-2) + the inference-profile ARN, derived from the `us.` prefix. New locked decision; a non-US profile needs a code change (documented). | §3B; §7; §8 Task 3; §9 (new decision); §12 Gate 5 |
| 5 | med | Poller integration mock authorizers pin `sub == "travel-agent"` (`test_e2e_poll.py:51`, `test_handler_with_mcp.py:62,192,305-335`). The sub flip breaks them. Original Task 7 names only `test_jwt_signer.py`. | Task 7 + Group E + Gate 2 expand: update every poller mock authorizer + assertion to expect `trip-tracker-poller` under the poller secret. | §8 Task 7; §11 Group E; §12 Gate 2 |
| 6 | med | flights/hotels packages run `node --import ./tests/setup.js --test` (`package.json:8`), NOT jest; mcp-authorizer has no test script. Gate 3's `npx jest lambdas/flights-mcp/tests/ ...` won't apply setup and won't run the authorizer tests. | Split Gate 3: root CDK tests via jest; flights/hotels via `npm test` (node --test) in-package; mcp-authorizer gets its own `node --test` script + a `tests/setup.js`. | §8 (Task 6, Task 9); §11 Group D/F; §12 Gate 3 |
| 7 | low | Gate 5 asserts only "2 secrets / no old env var / no Bedrock wildcard" — cannot catch #1 (server Lambda missing the new secret ARNs) or #4 (incomplete Bedrock ARN set). | Extend Gate 5: assert flights/hotels server Lambdas carry both new secret-ARN env vars, and the agent Bedrock policy carries all 3 foundation-model region ARNs + the profile ARN. | §12 Gate 5 |
| 8 | high | Distinct from #1's framing: even after Task 5 adds the secret ARNs to the *authorizer*, the server *handler* code (`flights/hotels index.js:23`) still reads `process.env.JWT_SIGNATURE_SECRET` at module scope and passes `undefined` to `jwt.verify` (`:56`). Original Task 5/9 never update the handler verify logic. | Task 5 explicitly updates each server `index.js` to the two-secret + sub-coupling verifier (the triplicated copy) reading the two new ARN env vars; Task 9 updates `setup.js` + `handler.test.js` accordingly. | §8 Task 5, Task 9; §11 Group F |

Net scope change: server handlers join the verifier set (verifier triplicated
across mcp-authorizer + flights/hotels `index.js`); secret fetch is lazy-cached
not import-time; agent Bedrock grant carries 7 ARNs (3 foundation-model regions
× 1 + … see §3B) instead of 2; estimated atomic tasks 10 → 12 (Task 5 split into
authorizer-wiring + server-handler-verifier; new Task 11 for poller integration
mocks). Gate 3 split into three runners.

---

## 1. Summary

Two bundled production-readiness work items, both flagged as carryovers in `docs/threat-model.md`:

**A. Per-component JWT secrets + per-component `sub` claims (ADR 0006).** The agent + poller currently share a single HS256 secret AND both present `sub: "travel-agent"`, so a compromised poller can mint tokens the MCP authorizer is forced to accept as the agent (`docs/threat-model.md` row at line 153). The fix: two AWS Secrets Manager secrets, one per minter; the agent keeps `sub: "travel-agent"`; the poller switches to `sub: "trip-tracker-poller"`; every verifier checks against whichever secret matches and rejects sub/secret mismatches. The verifier runs in **three places** — the shared MCP authorizer Lambda AND each MCP server handler (`lambdas/flights-mcp/index.js`, `lambdas/hotels-mcp/index.js`), which re-verify the bearer JWT in-handler as documented defense-in-depth. The ~15-line verifier is copied into all three (not factored into a shared module — see §10).

**B. Tighten the agent's Bedrock IAM grant.** The chat agent's `travelAgentFn` currently holds `resources: ['*']` for `bedrock:InvokeModel*` (`lib/agent.js:131`). The poller already mirrors the desired pattern — resource-scoped to a specific foundation-model + optional cross-region inference-profile ARN (`lib/poller-server.js:140-159`). Apply the same shape to the agent's grant. The agent uses cross-region inference profile `us.anthropic.claude-3-5-haiku-20241022-v1:0` (`lambdas/travel-agent/agent_config.py:10`), so both ARN forms are required.

## 2. Problem statement

The hard-coded `JWT_SIGNATURE_SECRET = 'jwt-signature-secret'` literal in `lib/strands-agent-on-lambda-stack.js:20` and the unified `sub: "travel-agent"` claim used by every minter together violate two defensive properties the threat model claims:
1. **Component-level identity at the auth boundary.** Today the MCP authorizer cannot tell agent-minted tokens from poller-minted tokens; this is documented in threat-model row at line 153 and tagged "ADR 0006 fix."
2. **Secret-out-of-repo invariant.** Anyone with repo access can mint a valid token against the current literal. Threat-model row at line 64 also tags this for ADR 0006.

Separately, the agent's `Resource: '*'` Bedrock grant in `lib/agent.js:131` is the widest IAM grant left after the poller's `resource-scoped pattern` landed. Threat-model row implicitly accepts this because the poller's grant tightening covered the read-most-often path, but the chat path still has the broad grant. A grant tightening here is a clean win in the same direction — same scope-down pattern, different consumer.

## 3. Solution shape

**A. Per-component JWT secrets — HS256-with-two-secrets + per-component sub.**

Why HS256-with-two-secrets and not RS256: asymmetric keys would force the MCP authorizer Lambda to hold (or re-fetch) public keys per verifier, and require key-format/library changes in every minter and verifier. The shared-symmetric model already in flight stays in place; we just split one secret + one sub into two.

Implementation shape:
1. **Two Secrets Manager secrets** created in a new `SecretsConstruct` (`lib/secrets.js`). Each is a Random-value secret of fixed length (40 chars, alphanumeric), generated by CloudFormation at deploy time. Names: `trip-tracker-agent-jwt-signer` and `trip-tracker-poller-jwt-signer`. Both secrets are auto-rotated only on manual rotation request — automatic rotation is out of scope (no rotation Lambda).
2. **Grant patterns.** Agent + poller each get `secretsmanager:GetSecretValue` on their OWN secret only. The MCP authorizer Lambda AND both MCP server Lambdas (flights + hotels) each get `secretsmanager:GetSecretValue` on BOTH secrets — the server handlers re-verify the JWT in-handler (`lambdas/flights-mcp/index.js:56`, `lambdas/hotels-mcp/index.js:55`) as documented defense-in-depth, so they are verifiers, not just env-var carriers. Resource-scoped, never `Resource: '*'`.
3. **Env-var injection** changes from one var to two for every verifier and stays one var for the minters:
   - Agent Lambda: `AGENT_JWT_SECRET_ARN`
   - Poller Lambda: `POLLER_JWT_SECRET_ARN`
   - Flights/hotels MCP authorizer Lambda AND flights/hotels MCP server Lambdas: `AGENT_JWT_SECRET_ARN` + `POLLER_JWT_SECRET_ARN`
4. **Secret-value retrieval is lazy + cached on first use** (NOT import-time). Each minter/verifier exposes a `_get_secret()` (Python) / `getSecret()` (Node) helper that fetches once on the first call and memoizes for the warm-Lambda lifetime. Import-time fetch is rejected because it fires before any test can stub the Secrets Manager client (see §0 finding #3); lazy-first-use lets tests patch the client before the first `sign_for_user()` / first handler invocation. Hot path stays in-memory after the first call.
5. **Verifier logic** (identical in the authorizer AND both server handlers — triplicated, see §10) verifies against the two-secret allowlist. For each candidate token: try `jwt.verify(token, agentSecret)` first; if it succeeds AND `claims.sub === 'travel-agent'`, allow. If that throws, try `jwt.verify(token, pollerSecret)`; if it succeeds AND `claims.sub === 'trip-tracker-poller'`, allow. Otherwise deny. The "secret must match its allowed sub" coupling is the load-bearing invariant — without it, a leaked agent token still works as a poller token. `jsonwebtoken`'s `jwt.verify` throws on any failure (bad signature, expired, malformed) AND throws if the secret arg is `undefined` — the per-secret `try/catch` must fall through to the next branch, never leak the wrong-secret diagnostic, and the secret values must be resolved (step 4) before the first verify.
6. **Poller sub change.** `lambdas/poller/jwt_signer.py:28` flips `SUBJECT = "travel-agent"` to `SUBJECT = "trip-tracker-poller"`. The agent's signer (in `lambdas/travel-agent/mcp_client_manager.py:134`) keeps `sub: "travel-agent"`.
7. **Stack literal goes away.** The `JWT_SIGNATURE_SECRET` constant in `lib/strands-agent-on-lambda-stack.js:20` is deleted. The `jwtSignatureSecret` prop on every construct is replaced with `agentJwtSecret` / `pollerJwtSecret` / both, depending on the construct.

**B. Agent Bedrock IAM grant tightening — mirror the poller.**

1. The agent's `model_id` (`us.anthropic.claude-3-5-haiku-20241022-v1:0`) is a **geographic cross-region inference profile** (the `us.` prefix). Bedrock routes an invocation through the profile to one of the US destination regions, and IAM authorizes against the foundation-model ARN *in the region the request lands in* — so a single-`${region}` foundation-model ARN gets `AccessDeniedException` on cross-region routing. The poller's single-region pattern (`lib/poller-server.js:141`) is correct for the poller only because the poller's default model (`claude-haiku-4-5-20251001`) is a plain foundation model, NOT a `us.`-prefixed profile; it does not transfer to the agent. Derive these ARNs at synth time from the `us.` prefix:
   - Inference-profile ARN: `arn:aws:bedrock:${region}:${account}:inference-profile/us.anthropic.claude-3-5-haiku-20241022-v1:0`.
   - Foundation-model ARN in EACH of the 3 US destination regions: `arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0`, `…:us-east-2::…`, `…:us-west-2::…` (the `us.` profile's known destination set; no `us.` prefix on the foundation-model id — foundation models are region-scoped, not profile-namespaced).
   - 7 ARNs total only if multiple actions were per-ARN; here it is 4 resource ARNs (1 profile + 3 foundation-model regions) shared by both actions.
2. CDK context override (`agentBedrockModelId`, defaulting to the constant above) lets a deploy pick a different model without code change — same pattern as the poller's `bedrockModelId` context (`lib/poller-server.js:66`). The 3-US-region destination set is derived from the `us.` prefix; a non-US geographic profile (`eu.`, `apac.`) would need a code change to its destination-region list — this is a locked decision (§9), not an oversight.
3. Replace `resources: ['*']` with the 4-ARN list. Keep both actions (`InvokeModel` + `InvokeModelWithResponseStream`) — the chat agent does streaming responses.
4. `lambdas/travel-agent/agent_config.py` reads the model ID from a new `AGENT_BEDROCK_MODEL_ID` env var, falling back to the literal if unset (so existing tests keep working). The stack injects the env var from the same context value.

## 4. Metadata

| Field | Value |
|---|---|
| Type | SECURITY_HARDENING (A) + IAM_TIGHTENING (B) |
| Complexity | MEDIUM |
| Systems Affected | CDK stack + 5 constructs; 5 Lambda packages (mcp-authorizer, flights-mcp, hotels-mcp, poller, travel-agent — the two MCP servers join because their handlers re-verify in-handler); new ADR; threat-model row updates |
| New deps | None (`@aws-sdk/client-secrets-manager` already shipped with Node 22 Lambda runtimes; Python boto3 is on every poller layer) |
| Estimated atomic tasks | 12 |

## 5. UX / operator-view transformation

### Before state

```
Repo HEAD              CDK synth                  Runtime
+----------------+    +----------------+    +-------------------+
| jwt-signature- | -> | env var on all | -> | every Lambda      |
| secret literal |    | 5 Lambdas      |    | shares one HS256  |
+----------------+    +----------------+    | key + one sub     |
                                            | "travel-agent"    |
                                            +-------------------+

Anyone with repo access mints a valid token. Authorizer cannot
distinguish agent-from-poller; threat-model row [153] documents
the gap.

travelAgentFn IAM: bedrock:InvokeModel resources:'*'
```

### After state

```
Repo HEAD          CDK synth (deploy time)        Runtime
+-------------+   +-----------------------+   +-------------------+
| no literal  |-->| 2 SecretsManager      |-->| agent reads its   |
| in repo     |   | secrets created at    |   | own secret ARN    |
|             |   | random:               |   | poller reads its  |
|             |   | trip-tracker-agent-   |   | own secret ARN    |
|             |   |   jwt-signer          |   | mcp-authorizers   |
|             |   | trip-tracker-poller-  |   | read BOTH; verify |
|             |   |   jwt-signer          |   | against the right |
|             |   +-----------------------+   | one based on sub  |
+-------------+                               +-------------------+

A compromised poller secret cannot mint agent-claiming tokens
(authorizer checks sub vs which-secret-verified the JWT). Repo
access yields zero JWT-forging capability.

travelAgentFn IAM: bedrock:InvokeModel{,WithResponseStream}
  resources: [
    arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-haiku-...,
    arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-3-5-haiku-...,
    arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-3-5-haiku-...,
    arn:aws:bedrock:${region}:${account}:inference-profile/us.anthropic...,
  ]

flights/hotels server Lambdas + the shared mcp-authorizer all read
BOTH secret ARNs and run the same two-secret + sub-coupling verify
(triplicated copy, see §10).
```

## 6. Mandatory reading

| Priority | File | Lines | Why |
|---|---|---|---|
| P0 | `lib/strands-agent-on-lambda-stack.js` | 1-110 | The literal at line 20 + the four `jwtSignatureSecret:` prop assignments at lines 40, 48, 60, 88. All four assignments change shape after this PRP. |
| P0 | `lambdas/mcp-authorizer/index.js` | 1-40 | The shared verifier whose logic gets the two-secret allowlist treatment. Note the existing single-sub enforcement at line 16; the new logic moves the sub check inside the per-secret verification branch. This same verifier is copied (not shared) into the two server handlers below. |
| P0 | `lambdas/flights-mcp/index.js` | 1-72 | **In-handler verifier.** Line 23 `const JWT_SIGNATURE_SECRET = process.env.JWT_SIGNATURE_SECRET;`, line 56 `jwt.verify(token, JWT_SIGNATURE_SECRET)` — documented defense-in-depth (comment at 49-50). This handler gets the same two-secret + sub-coupling verify as the authorizer (triplicated). The §0 finding #1/#8 file. |
| P0 | `lambdas/hotels-mcp/index.js` | 1-72 | Same as flights (lines 22, 55). |
| P0 | `lambdas/travel-agent/app.py` | 30-55 | Line 33 `JWT_SIGNATURE_SECRET = os.environ['JWT_SIGNATURE_SECRET']` is a **dead read** — actual inbound verify is RS256/Cognito-JWKS at lines 41,51. §0 finding #2: delete line 33 only; app.py is NOT a verifier. |
| P0 | `lib/poller-server.js` | 60-90, 140-159 | Pattern source — the synth-time allowlist validation (lines 70-81) and the resource-scoped Bedrock grant (lines 140-159). NOTE: the poller's single-`${region}` ARN is correct only because its model (`poller-server.js:10`) is a plain foundation model, not a `us.`-prefixed geographic profile. Do not copy the single-region shape to the agent (§0 finding #4). |
| P0 | `lambdas/poller/jwt_signer.py` | 1-62 | The Python signer. Line 28 `SUBJECT = "travel-agent"` flips to `"trip-tracker-poller"`. Line 27 `JWT_SIGNATURE_SECRET = os.environ.get(...)` flips to a **lazy, cached-on-first-use** Secrets Manager fetch (NOT import-time — §0 finding #3). The external contract (`sign_for_user(user_id)`) stays the same. |
| P0 | `lambdas/travel-agent/mcp_client_manager.py` | 40-50, 130-140 | The agent's live HS256 minter. Line 44 `jwt_signature_secret = os.environ['JWT_SIGNATURE_SECRET']`, line ~137 `jwt.encode(..., algorithm="HS256")` — switches to a lazy cached Secrets Manager fetch reading `AGENT_JWT_SECRET_ARN`. The `sub: "travel-agent"` claim is unchanged. |
| P0 | `lambdas/travel-agent/agent_config.py` | 1-12 | The agent's model literal at line 10 (`us.`-prefixed → geographic profile). The model-id source flips to env var with the literal as fallback so existing tests don't need fixture updates. |
| P0 | `lib/agent.js` | 100-135 | The Bedrock IAM grant block at lines 129-132 → 4-ARN resource-scoped grant (3 US foundation-model regions + profile). Also adds `AGENT_JWT_SECRET_ARN` env var + `agentJwtSecret.grantRead`. |
| P0 | `lib/flights-mcp-server.js` | 55-72 | BOTH the server Lambda env block AND the authorizer Lambda env block: `JWT_SIGNATURE_SECRET` becomes the two ARN env vars; `grantRead` BOTH secrets to BOTH Lambdas (server handler is a verifier — §0 finding #1/#8). |
| P0 | `lib/hotels-mcp-server.js` | 45-62 | Same as flights for hotels. |
| P0 | `docs/threat-model.md` | 60-70, 145-160 | Rows that ADR 0006 closes — update the "fix" column in both rows to reference this PRP's commit + ADR 0006. |
| P1 | `lambdas/poller/tests/test_jwt_signer.py` | all | Existing Python signer tests. The env-var module-load fixture must move to patching the boto3 client before first `sign_for_user()` (lazy fetch). |
| P1 | `lambdas/poller/tests/test_e2e_poll.py`, `lambdas/poller/tests/test_handler_with_mcp.py` | sub checks | Mock authorizers pin `sub == "travel-agent"` (`test_e2e_poll.py:51`, `test_handler_with_mcp.py:62,192,305-335`). The poller sub flip breaks them — they must accept `trip-tracker-poller` under the poller secret (§0 finding #5). |
| P1 | `lambdas/flights-mcp/tests/setup.js`, `lambdas/hotels-mcp/tests/setup.js`, `lambdas/flights-mcp/package.json`, `lambdas/hotels-mcp/package.json` | all | Setup sets `JWT_SIGNATURE_SECRET` for the `node --test` runner (NOT jest — `package.json:8`). Split into the two ARN env vars + a stubbed secret-fetch; handler tests gain cross-sub cases (§0 finding #6/#8). |

**External documentation:**

| Source | Section | Why |
|---|---|---|
| [aws-cdk-lib v2.196 aws-secretsmanager Secret](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_secretsmanager.Secret.html) | constructor + `generateSecretString` | Construct shape; how to set the secret name, exclude characters, length |
| [aws-cdk-lib v2.196 IGrantable + Secret.grantRead](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_secretsmanager.Secret.html#grantwbrreadgrantee-versionstages) | grantRead | Idiomatic IAM grant — auto-scopes to the secret ARN, no PolicyStatement boilerplate |
| [AWS SDK v3 SecretsManagerClient](https://docs.aws.amazon.com/AWSJavaScriptSDK/v3/latest/client/secrets-manager/) | GetSecretValueCommand | Node-side fetch in the authorizer Lambda |
| [boto3 SecretsManager docs](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/secretsmanager/client/get_secret_value.html) | get_secret_value | Python-side fetch in the poller |

## 7. Patterns to mirror

### RESOURCE-SCOPED BEDROCK GRANT (from `lib/poller-server.js:140-159`)

```js
const region = Stack.of(this).region;
const bedrockResources = [`arn:aws:bedrock:${region}::foundation-model/${bedrockModelId}`];
const inferenceProfileArn = scope.node.tryGetContext('bedrockInferenceProfileArn');
if (inferenceProfileArn) {
    const inferenceProfilePattern = /^arn:aws:bedrock:[a-z0-9-]+:\d{12}:inference-profile\/[\w.-]+$/;
    if (!inferenceProfilePattern.test(inferenceProfileArn)) {
        throw new Error(
            `bedrockInferenceProfileArn must match arn:aws:bedrock:<region>:<account>:inference-profile/<id>; got: ${inferenceProfileArn}`
        );
    }
    bedrockResources.push(inferenceProfileArn);
}
pollerFn.addToRolePolicy(new iam.PolicyStatement({
    actions: ['bedrock:InvokeModel'],
    resources: bedrockResources,
}));
```

The agent version does NOT mirror this single-region shape. The agent's model is a `us.`-prefixed **geographic** inference profile that routes across us-east-1, us-east-2, us-west-2; IAM authorizes against the foundation-model ARN in whichever region the request lands in. The agent grant therefore enumerates the foundation-model ARN in all three US regions PLUS the inference-profile ARN — 4 resource ARNs, derived from the `us.` prefix:

```js
const region = Stack.of(this).region;
const account = Stack.of(this).account;
// agentBedrockModelId default: 'us.anthropic.claude-3-5-haiku-20241022-v1:0'
const profileId = agentBedrockModelId;                    // keeps the us. prefix
const fmId = profileId.replace(/^us\./, '');              // foundation-model id, no prefix
const US_PROFILE_REGIONS = ['us-east-1', 'us-east-2', 'us-west-2'];
const bedrockResources = [
    ...US_PROFILE_REGIONS.map(r => `arn:aws:bedrock:${r}::foundation-model/${fmId}`),
    `arn:aws:bedrock:${region}:${account}:inference-profile/${profileId}`,
];
```

A non-US geographic profile (`eu.`, `apac.`) would need its destination-region list changed in code — a locked decision (§9 #10), not an oversight. A blank/whitespace `agentBedrockModelId` must throw at synth (mirror `lib/poller-server.js:79`).

### SYNTH-TIME ALLOWLIST (from `lib/poller-server.js:70-81`)

```js
const ALLOWED_BEDROCK_MODES = ['live', 'stub'];
if (!ALLOWED_BEDROCK_MODES.includes(bedrockMode)) {
    throw new Error(
        `bedrockMode context value must be one of ${ALLOWED_BEDROCK_MODES.join(', ')}; got: ${bedrockMode}`
    );
}
```

The new secrets construct uses this pattern for any synth-time input it accepts (none, in v1 — secrets are generated at deploy time with fixed parameters).

### SECRETSMANAGER GRANT (canonical CDK pattern; new in this PRP)

```js
// SOURCE: aws-cdk-lib v2.196 docs (no existing precedent in this repo)
const agentSecret = new secretsmanager.Secret(this, 'AgentJwtSigner', {
    secretName: 'trip-tracker-agent-jwt-signer',
    generateSecretString: {
        passwordLength: 40,
        excludePunctuation: true,
        excludeCharacters: ' ',  // keep alphanumeric only
        includeSpace: false,
    },
});
agentSecret.grantRead(agentLambda);
```

`grantRead` is the IGrantable shortcut — emits `secretsmanager:GetSecretValue` scoped to the secret's ARN. Use this instead of hand-rolling a PolicyStatement.

### LAZY CACHED SECRET FETCH (Python, new in this PRP)

```python
# SOURCE: new pattern. Boto3 client + GetSecretValue on FIRST USE,
# memoized for the warm-Lambda lifetime. NOT import-time (§0 finding #3:
# import-time fetch fires before any test can stub the client).
import os, boto3
_secrets = boto3.client("secretsmanager")
_cached_secret = None

def _get_secret():
    global _cached_secret
    if _cached_secret is None:
        arn = os.environ.get("POLLER_JWT_SECRET_ARN")
        if not arn:
            raise EnvironmentError("POLLER_JWT_SECRET_ARN env var is required")
        _cached_secret = _secrets.get_secret_value(SecretId=arn)["SecretString"]
    return _cached_secret
```

`sign_for_user()` calls `_get_secret()` instead of reading a module global. Failure (missing env var or GetSecretValue raises) is fail-loud on first sign, not at import. Tests patch BEFORE the first call: `monkeypatch.setattr(jwt_signer, "_secrets", FakeSecretsClient(...))` then reset `jwt_signer._cached_secret = None`.

### LAZY CACHED SECRET FETCH (Node, new in this PRP — used in all 3 verifiers)

```js
import { SecretsManagerClient, GetSecretValueCommand } from '@aws-sdk/client-secrets-manager';

let secretsClient = new SecretsManagerClient();
const _cache = {};

// Lazy: the fetch fires on the FIRST handler invocation, not at module
// load. Module-load fetch starts before a test can replace the client
// (§0 finding #3). Memoized per warm execution environment.
async function getSecret(envVar) {
    const arn = process.env[envVar];
    if (!arn) throw new Error(`${envVar} env var is required`);
    if (!_cache[arn]) {
        _cache[arn] = (await secretsClient.send(
            new GetSecretValueCommand({ SecretId: arn }))).SecretString;
    }
    return _cache[arn];
}
```

Handlers `await getSecret('AGENT_JWT_SECRET_ARN')` / `'POLLER_JWT_SECRET_ARN'` on first use. Tests stub by replacing `secretsClient` (or the whole `getSecret`) before the first invocation. **This exact helper + the two-secret/sub-coupling verify is copied verbatim into `lambdas/mcp-authorizer/index.js`, `lambdas/flights-mcp/index.js`, and `lambdas/hotels-mcp/index.js`** — triplicated, not shared (see §10). Drift is bounded by Group D + Group F asserting the identical invariant in each location.

## 8. Files to change

| File | Action | Justification |
|---|---|---|
| `lib/secrets.js` | CREATE | New `SecretsConstruct` exporting `this.agentJwtSecret` + `this.pollerJwtSecret`. Synth-time creation of the two Secrets Manager secrets with `generateSecretString`. |
| `lib/strands-agent-on-lambda-stack.js` | UPDATE | Delete the hard-coded literal (line 20). Instantiate `SecretsConstruct` early in the constructor. Replace `jwtSignatureSecret: JWT_SIGNATURE_SECRET` prop with `agentJwtSecret` / `pollerJwtSecret` on each consumer construct. |
| `lib/agent.js` | UPDATE | (a) Drop `JWT_SIGNATURE_SECRET` env var; add `AGENT_JWT_SECRET_ARN` env var. (b) Replace `resources: ['*']` Bedrock grant with the **4-ARN** resource-scoped grant (3 US foundation-model regions + inference-profile), per §7. (c) Add `agentBedrockModelId` context override (default `'us.anthropic.claude-3-5-haiku-20241022-v1:0'`); throw at synth if blank. (d) Add `AGENT_BEDROCK_MODEL_ID` env var from the same context value. (e) Call `props.agentJwtSecret.grantRead(travelAgentFn)`. |
| `lib/poller-server.js` | UPDATE | (a) Drop `JWT_SIGNATURE_SECRET` env var; add `POLLER_JWT_SECRET_ARN` env var. (b) Call `props.pollerJwtSecret.grantRead(pollerFn)`. (Bedrock grant unchanged — poller model is not a geographic profile.) |
| `lib/flights-mcp-server.js` | UPDATE | (a) Drop `JWT_SIGNATURE_SECRET` from BOTH the server Lambda AND the authorizer Lambda env blocks. (b) Add `AGENT_JWT_SECRET_ARN` + `POLLER_JWT_SECRET_ARN` to BOTH the server Lambda AND the authorizer Lambda env — the server handler re-verifies in-handler (§0 finding #1/#8), it is a verifier. (c) Call `props.agentJwtSecret.grantRead(...)` + `props.pollerJwtSecret.grantRead(...)` for BOTH `flightsServerFn` AND `flightsAuthorizerFn`. |
| `lib/hotels-mcp-server.js` | UPDATE | Same as flights (server + authorizer both get both secret ARNs + grantRead). |
| `lambdas/mcp-authorizer/index.js` | UPDATE | Replace the single-secret verify path with the two-secret allowlist + lazy cached fetch (§7 Node pattern). Invariant: a token must verify under exactly one secret AND its `sub` must match the allowed-sub for that secret. |
| `lambdas/flights-mcp/index.js` | UPDATE | **§0 finding #1/#8.** Replace `const JWT_SIGNATURE_SECRET = process.env.JWT_SIGNATURE_SECRET` (line 23) + the single `jwt.verify(token, JWT_SIGNATURE_SECRET)` (line 56) with the triplicated two-secret + sub-coupling verifier reading the two new ARN env vars via the §7 lazy `getSecret` helper. Keep the in-handler verify (defense-in-depth, comment 49-50). |
| `lambdas/hotels-mcp/index.js` | UPDATE | Same as flights (lines 22, 55). |
| `lambdas/flights-mcp/package.json` + `lambdas/hotels-mcp/package.json` | UPDATE | Add `@aws-sdk/client-secrets-manager` to deps (the handler now fetches secrets). `jsonwebtoken` already present. |
| `lambdas/mcp-authorizer/package.json` | UPDATE | Add `@aws-sdk/client-secrets-manager` dependency. Add `jsonwebtoken` if not already there. Add a `"test": "node --import ./tests/setup.js --test \"tests/*.test.js\""` script + a `tests/setup.js` (the package has no runner today — §0 finding #6). |
| `lambdas/poller/jwt_signer.py` | UPDATE | (a) `SUBJECT = "trip-tracker-poller"`. (b) Replace `os.environ.get("JWT_SIGNATURE_SECRET")` with the §7 lazy `_get_secret()` reading `POLLER_JWT_SECRET_ARN` (NOT import-time). (c) Failure message names the new env var. |
| `lambdas/poller/tests/test_jwt_signer.py` | UPDATE | Patch the boto3 client + reset `_cached_secret` before the first `sign_for_user()` (lazy fetch). Add a case asserting `sub` is `trip-tracker-poller`, NOT `travel-agent`. |
| `lambdas/poller/tests/test_e2e_poll.py` + `lambdas/poller/tests/test_handler_with_mcp.py` | UPDATE | **§0 finding #5.** Mock authorizers (`test_e2e_poll.py:51`, `test_handler_with_mcp.py:62`) + assertions (`:192`) + the dedicated reject test (`:305-335`) flip to expect `sub == "trip-tracker-poller"` under the poller secret. |
| `lambdas/travel-agent/mcp_client_manager.py` | UPDATE | Replace `jwt_signature_secret = os.environ['JWT_SIGNATURE_SECRET']` (line 44) with the §7 lazy `_get_secret()` reading `AGENT_JWT_SECRET_ARN`. The `sub: "travel-agent"` claim is unchanged. |
| `lambdas/travel-agent/app.py` | UPDATE | **§0 finding #2.** Delete the dead line 33 `JWT_SIGNATURE_SECRET = os.environ['JWT_SIGNATURE_SECRET']` (never used — app.py verifies via RS256/Cognito-JWKS). One-line delete, no migration. |
| `lambdas/travel-agent/agent_config.py` | UPDATE | Replace the hardcoded `model_id="us.anthropic.claude-3-5-haiku-20241022-v1:0"` with `os.getenv("AGENT_BEDROCK_MODEL_ID", "us.anthropic.claude-3-5-haiku-20241022-v1:0")`. |
| `lambdas/mcp-authorizer/tests/handler.test.js` | CREATE | First test for the authorizer (uses the new `node --test` script). Pin: (a) agent-secret + `sub:travel-agent` ⇒ Allow. (b) poller-secret + `sub:trip-tracker-poller` ⇒ Allow. (c) agent-secret + `sub:trip-tracker-poller` ⇒ Deny. (d) poller-secret + `sub:travel-agent` ⇒ Deny. (e) foreign-secret ⇒ Deny. (f) malformed bearer ⇒ Deny. (g) missing header ⇒ Deny. (h) expired ⇒ Deny. |
| `lambdas/flights-mcp/tests/setup.js` + `lambdas/hotels-mcp/tests/setup.js` | UPDATE | Replace `JWT_SIGNATURE_SECRET ??= 'test-secret'` with `AGENT_JWT_SECRET_ARN` + `POLLER_JWT_SECRET_ARN` env stubs AND a stubbed `getSecret` (the handler now fetches). Update each `handler.test.js`: existing happy path uses agent-secret + `sub:travel-agent`; ADD the 4 cross-sub/foreign-secret deny cases (the verifier is now in-handler, §0 finding #8). |
| `test/secrets-construct.test.js` | CREATE | Jest tests for the new SecretsConstruct: (a) synthesises two `AWS::SecretsManager::Secret` resources with the expected names. (b) `generateSecretString` config has `passwordLength: 40`, `excludePunctuation: true`. (c) The secrets construct exposes `this.agentJwtSecret` + `this.pollerJwtSecret` as `Secret` instances. |
| `test/agent-bedrock-iam.test.js` | CREATE | Jest tests for the tightened Bedrock IAM grant: (a) the agent's IAM role has a `bedrock:InvokeModel` + `bedrock:InvokeModelWithResponseStream` policy with a `Resource` array of exactly the **4 expected ARNs** (foundation-model in us-east-1/us-east-2/us-west-2 + the inference-profile ARN). (b) no `'*'` resource on Bedrock actions anywhere in the agent's role. (c) the `agentBedrockModelId` context override propagates to both the IAM grant ARNs AND the `AGENT_BEDROCK_MODEL_ID` env var. (d) a blank `agentBedrockModelId` throws at synth. |
| `docs/adr/0006-per-component-jwt-secrets.md` | CREATE | New ADR. Status: Accepted. Context, Decision, Consequences. Documents the HS256-with-two-secrets choice, rejects RS256 as over-engineering for personal scale, names the two secrets, names the sub allowlist contract at the authorizer, lists the threat-model rows it closes. |
| `docs/threat-model.md` | UPDATE | (a) Row at line 64 (`JWT_SIGNATURE_SECRET`) — flip the "Fix" column from `Hard-coded literal; ADR 0006 will revisit` to `Per-component secrets in Secrets Manager (ADR 0006, commit <sha>); rotation manual.` (b) Row at line 153 — same flip; the "fix" column changes from a forward reference to a backward one. (c) Append a change-log entry. |
| `docs/adr/README.md` | UPDATE | Add the new ADR 0006 row to the index. |

## 9. Locked decisions

1. **HS256-with-two-secrets, not RS256.** Asymmetric keys would force every minter to ship a private-key library + cache, and every verifier to hold a public-key cache or fetch from JWKS. Disproportionate at the project's scale. Revisit if the verifier ever needs to live in a less-trusted zone than the minters.
2. **Two secrets, not N.** One per minter. The MCP authorizer is the only verifier today; if the architecture later adds a non-MCP verifier, that verifier gets the secret it needs and only that one.
3. **Sub allowlist coupling is inside the verifier.** `secret -> allowed-sub` is a fixed map in `lambdas/mcp-authorizer/index.js`, not a wildcard. A future minter requires both a new secret AND adding its sub to the map.
4. **Secret rotation is manual in v1.** No automatic rotation Lambda. Document the rotation procedure in the ADR's Consequences section but don't implement it.
5. **Secrets are generated by CloudFormation at deploy time** (`generateSecretString`), not pre-provisioned. This is a different pattern from the SES verified identity (which is manual via the AWS console because identity verification requires receiving an email). Random generation is the right shape for HMAC secrets.
6. **Agent Bedrock grant enumerates 3 US foundation-model regions + the inference-profile ARN (4 ARNs).** The model is a `us.`-prefixed geographic inference profile; Bedrock authorizes against the foundation-model ARN in whichever destination region (us-east-1/us-east-2/us-west-2) it routes to. A single-region ARN gets `AccessDeniedException` on cross-region routing (§0 finding #4). The poller's single-region pattern does NOT transfer (its model is a plain foundation model).
7. **Agent model id is overridable but unchanged in default value.** Design-spec §4 calls for a Sonnet 4.6 upgrade for the chat agent — that's tracked separately and not in this PRP's scope.
8. **The MCP server Lambdas (flights, hotels) DO verify the JWT in-handler and DO read both secrets.** Reversed from the original PRP (§0 finding #1 — the "verified by grep" claim was false). `lambdas/flights-mcp/index.js:56` + `hotels-mcp/index.js:55` `jwt.verify` as documented defense-in-depth. They get both secret ARNs + `grantRead` + the triplicated two-secret verifier. The in-handler check is NOT removed (removing it would be a security regression).
9. **Secret fetch is lazy + cached on first use, not import-time** (§0 finding #3). Import-time fetch fires before any test can stub the Secrets Manager client. A `_get_secret()` / `getSecret()` helper memoizes per warm execution environment; fail-loud moves from import to first sign/verify.
10. **The agent's geographic-profile destination-region set is hardcoded to the 3 US regions** (us-east-1, us-east-2, us-west-2), derived from the `us.` prefix. A non-US profile (`eu.`, `apac.`) requires a code change to that list. This is a deliberate scope boundary — a prefix→regions lookup table is out of scope for this PRP (§10).
11. **The two-secret + sub-coupling verifier is triplicated (copied) across `mcp-authorizer/index.js`, `flights-mcp/index.js`, `hotels-mcp/index.js`, not factored into a shared module.** A cross-Lambda-package shared verifier needs a Lambda layer or monorepo symlink — larger than this security fix and a new failure surface. Drift risk is bounded: ~15 lines of stateless JWT logic with the identical invariant pinned by Group D (authorizer) + Group F (both servers). Honors the §10 exclusion.

## 10. NOT building (explicit)

- **Automatic secret rotation.** Rotation Lambda is non-trivial to write correctly (must coordinate minter + verifier rollover). Manual rotation via the AWS console + redeploy is the v1 plan.
- **RS256 / asymmetric keys.** Out of scope; see Locked Decision #1.
- **Replacing the agent's model with Sonnet 4.6.** Tracked separately per design-spec §4.
- **A new central JWT-config module shared across Lambda packages.** The two-secret verifier is intentionally **triplicated** (copied verbatim into the authorizer + both server handlers — §9 #11), not factored into a shared module/layer. A cross-package shared verifier needs a Lambda layer or monorepo symlink, both larger than this security fix. Per-location Group D/F tests pin the identical invariant so a copy that drifts fails before production.
- **A profile-prefix → destination-regions lookup table.** The agent's `us.` profile maps to a hardcoded 3-region list (§9 #10). Generalising to `eu.`/`apac.` via a table is out of scope.
- **Touching the agent-authorizer Lambda (`lambdas/agent-authorizer/`).** That Lambda validates Cognito JWTs via JWKS — different protocol, different secret model, not affected by this PRP.
- **Re-deploying `mcp-authorizer` to a fresh package name.** The current `lambdas/mcp-authorizer/` source asset is shared between flights + hotels (per the cleanup work just done). The shape of that sharing is unchanged.
- **Automatic secret rotation Lambda.** (Already listed above; rotation stays manual.) Note Gate 5 DOES now assert that both server Lambdas carry the two secret-ARN env vars + the full 4-ARN Bedrock set — that synth-time check is in scope (§0 finding #7), it is the per-construct jest tests that remain the primary env-var coverage.

## 11. Test matrix

### Group A — SecretsConstruct (jest, new `test/secrets-construct.test.js`)
- `test_A1_construct_synthesises_two_secret_resources`
- `test_A2_agent_secret_name_is_trip_tracker_agent_jwt_signer`
- `test_A3_poller_secret_name_is_trip_tracker_poller_jwt_signer`
- `test_A4_generateSecretString_pins_length_40_excludePunctuation`
- `test_A5_construct_exposes_agentJwtSecret_and_pollerJwtSecret_as_Secret_instances`

### Group B — Stack wiring (jest, fold into existing or new `test/stack-secrets-wiring.test.js`)
- `test_B1_agent_lambda_has_AGENT_JWT_SECRET_ARN_env_var_referencing_agent_secret`
- `test_B2_poller_lambda_has_POLLER_JWT_SECRET_ARN_env_var_referencing_poller_secret`
- `test_B3_flights_authorizer_AND_server_have_both_secret_arn_env_vars`
- `test_B4_hotels_authorizer_AND_server_have_both_secret_arn_env_vars`
- `test_B5_no_lambda_in_the_stack_carries_the_old_JWT_SIGNATURE_SECRET_env_var`
- `test_B6_no_secrets_manager_grant_uses_resource_wildcard`
- `test_B7_flights_and_hotels_server_lambdas_grantRead_BOTH_secrets`
- `test_B8_agent_lambda_has_AGENT_BEDROCK_MODEL_ID_env_var`

### Group C — Agent Bedrock IAM grant (jest, new `test/agent-bedrock-iam.test.js`)
- `test_C1_agent_role_has_no_resource_wildcard_bedrock_grant`
- `test_C2_agent_role_grants_invoke_model_on_foundation_model_arn_in_all_3_us_regions`
- `test_C3_agent_role_grants_invoke_model_on_inference_profile_arn`
- `test_C4_agent_role_grants_invoke_model_with_response_stream`
- `test_C5_agentBedrockModelId_context_override_propagates_to_env_var_AND_iam_arns`
- `test_C6_agent_role_bedrock_resource_array_has_exactly_4_arns`
- `test_C7_blank_agentBedrockModelId_throws_at_synth`

### Group D — mcp-authorizer verifier (`node --test`, new `lambdas/mcp-authorizer/tests/handler.test.js` + `tests/setup.js`)
(NOT jest — the package gains its own `node --test` script per §0 finding #6. The test stubs `getSecret` so no real Secrets Manager call fires.)
- `test_D1_agent_secret_signed_with_sub_travel_agent_returns_allow`
- `test_D2_poller_secret_signed_with_sub_trip_tracker_poller_returns_allow`
- `test_D3_agent_secret_signed_with_sub_trip_tracker_poller_returns_deny` (cross-sub forgery)
- `test_D4_poller_secret_signed_with_sub_travel_agent_returns_deny` (cross-sub forgery)
- `test_D5_token_signed_with_foreign_secret_returns_deny`
- `test_D6_malformed_authorization_header_returns_deny`
- `test_D7_missing_authorization_header_returns_deny`
- `test_D8_token_with_expired_iat_exp_returns_deny`

### Group E — Python poller signer + integration mocks (pytest)
`lambdas/poller/tests/test_jwt_signer.py`:
- `test_E1_signed_token_has_sub_trip_tracker_poller` (asserts the SUBJECT constant flip)
- `test_E2_signer_fetches_secret_lazily_on_first_sign_not_at_import`
- `test_E3_signer_raises_if_POLLER_JWT_SECRET_ARN_env_var_missing` (on first sign, not import)
- `test_E4_signer_raises_if_secrets_manager_get_secret_value_fails`
- `test_E5_existing_token_lifecycle_tests_continue_to_pass` (catch-all regression)

`lambdas/poller/tests/test_e2e_poll.py` + `test_handler_with_mcp.py` (§0 finding #5):
- `test_E6_mock_authorizer_accepts_sub_trip_tracker_poller_under_poller_secret`
- `test_E7_handler_with_mcp_call_assertions_expect_trip_tracker_poller`
- `test_E8_dedicated_wrong_sub_reject_test_updated_for_new_sub` (the `:305-335` test)

### Group F — flights / hotels in-handler verifier (`node --test`, update setup + handler tests)

Runner is the package's own `node --test` (NOT jest — §0 finding #6). `tests/setup.js` in each package stubs `AGENT_JWT_SECRET_ARN` + `POLLER_JWT_SECRET_ARN` AND replaces `getSecret` so no real Secrets Manager call fires; the handler-test helper signs with the matching fake secret.

Per package (`flights-mcp`, `hotels-mcp`) `handler.test.js`:
- `test_F1_agent_secret_signed_sub_travel_agent_passes_handler` (existing happy path, rewired)
- `test_F2_poller_secret_signed_sub_trip_tracker_poller_passes_handler`
- `test_F3_agent_secret_signed_sub_trip_tracker_poller_rejected_401` (cross-sub forgery, in-handler)
- `test_F4_poller_secret_signed_sub_travel_agent_rejected_401` (cross-sub forgery, in-handler)
- `test_F5_foreign_secret_signed_token_rejected_401`
- `test_F6_handler_still_processes_rpc_after_successful_verify` (regression: verify change didn't break the MCP path)

## 12. Validation gates

### Gate 1 — Notifier suite (regression check)

```
cd C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/notifier && \
  "C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/python.exe" -m pytest tests/ -q
```
EXPECT: 126 passing. The notifier doesn't touch JWT_SIGNATURE_SECRET, so this is a pure regression check.

### Gate 2 — Poller + evals suite

```
"C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/python.exe" \
  -m pytest C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/poller/tests/ \
            C:/Users/isabe/Downloads/trip-tracker-agent/evals/tests/ -q
```
EXPECT: 310 prior + ~5 new jwt_signer (Group E E1-E5) + the poller integration mocks updated for the new sub (E6-E8, same test count, modified assertions) = ~315 passing. A green run here proves §0 finding #5 is closed (poller integration tests accept `trip-tracker-poller`).

### Gate 3 — Jest (CDK) + node --test (Lambda packages), three runners

flights/hotels use `node --test`, NOT jest, and mcp-authorizer gains its own `node --test` script (§0 finding #6). Run all three:

```
cd C:/Users/isabe/Downloads/trip-tracker-agent && npx jest test/
cd C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/mcp-authorizer && npm test
cd C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/flights-mcp && npm test
cd C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/hotels-mcp && npm test
```
EXPECT: jest `test/` = 76 prior + ~19 new (Group A/B/C); mcp-authorizer `node --test` = 8 new (Group D); flights + hotels `node --test` = prior + 6 each (Group F). All green. A jest invocation against `lambdas/flights-mcp/tests/` would NOT apply `tests/setup.js` and is the wrong runner — do not use it.

### Gate 4 — Comment-cleanliness ripgrep

```
rg -n --no-heading 'slice[ -_]?\d|\bT[1-9]\b|\bTask [1-9]\b|Checkpoint [A-Z]\b' \
  C:/Users/isabe/Downloads/trip-tracker-agent/lib/secrets.js \
  C:/Users/isabe/Downloads/trip-tracker-agent/test/secrets-construct.test.js \
  C:/Users/isabe/Downloads/trip-tracker-agent/test/agent-bedrock-iam.test.js \
  C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/mcp-authorizer/tests/handler.test.js \
  C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/mcp-authorizer/tests/setup.js \
  C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/flights-mcp/index.js \
  C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/hotels-mcp/index.js \
  C:/Users/isabe/Downloads/trip-tracker-agent/docs/adr/0006-per-component-jwt-secrets.md
rg -n --no-heading -w 'basically|simply|obviously|essentially|merely' \
  <same files>
```
EXPECT: zero matches in both.

### Gate 5 — Full-stack synth + IAM-shape assertions

Run the same Docker-skipping node-eval pattern this codebase uses (per `project_cdk_test_invocation_gotchas` memory):

```
DUFFEL_API_KEY=stub LITEAPI_API_KEY=stub node -e "
  const { App } = require('aws-cdk-lib');
  const { StrandsAgentOnLambdaStack } = require('./lib/strands-agent-on-lambda-stack');
  const app = new App({ context: {
    'aws:cdk:bundling-stacks': [],
    mcpMode: 'fixture',
    bedrockModelId: 'claude-haiku-4-5-20251001',
    bedrockMode: 'stub',
    notifierSenderEmail: 'test@example.com',
    notifierRecipientEmail: 'me@example.com',
    sesMode: 'stub',
  }});
  const stack = new StrandsAgentOnLambdaStack(app, 'TestStack', {});
  const tmpl = app.synth().getStackByName('TestStack').template;
  const resources = tmpl.Resources || {};
  // 1. Two Secrets Manager resources exist with the expected names.
  const secrets = Object.values(resources).filter(r => r.Type === 'AWS::SecretsManager::Secret');
  if (secrets.length !== 2) { console.error('expected 2 secrets, got', secrets.length); process.exit(1); }
  // 2. No Lambda has the old JWT_SIGNATURE_SECRET env var.
  for (const r of Object.values(resources)) {
    if (r.Type !== 'AWS::Lambda::Function') continue;
    const env = r.Properties?.Environment?.Variables || {};
    if ('JWT_SIGNATURE_SECRET' in env) { console.error('Lambda still carries JWT_SIGNATURE_SECRET'); process.exit(1); }
  }
  // 3. No bedrock:InvokeModel* statement anywhere has Resource '*'.
  for (const r of Object.values(resources)) {
    if (r.Type !== 'AWS::IAM::Policy') continue;
    const stmts = r.Properties?.PolicyDocument?.Statement || [];
    for (const s of stmts) {
      const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
      const isBedrockInvoke = actions.some(a => a && a.startsWith('bedrock:InvokeModel'));
      if (!isBedrockInvoke) continue;
      const resources_ = Array.isArray(s.Resource) ? s.Resource : [s.Resource];
      if (resources_.includes('*')) { console.error('bedrock:InvokeModel* grant uses Resource: *'); process.exit(1); }
    }
  }
  // 4. Every Lambda whose function name contains mcp (flights/hotels server
  //    AND authorizer) carries BOTH new secret-ARN env vars (catches §0 #1/#8:
  //    a server Lambda left without a usable verifier secret).
  for (const r of Object.values(resources)) {
    if (r.Type !== 'AWS::Lambda::Function') continue;
    const fn = JSON.stringify(r.Properties?.FunctionName || '');
    if (!/mcp/i.test(fn)) continue;
    const env = r.Properties?.Environment?.Variables || {};
    if (!('AGENT_JWT_SECRET_ARN' in env) || !('POLLER_JWT_SECRET_ARN' in env)) {
      console.error('MCP Lambda missing a secret-ARN env var:', fn); process.exit(1);
    }
  }
  // 5. The agent Bedrock grant carries the full 4-ARN set (3 us regions FM +
  //    1 inference-profile), not a single-region ARN (catches §0 #4).
  let agentBedrockOk = false;
  for (const r of Object.values(resources)) {
    if (r.Type !== 'AWS::IAM::Policy') continue;
    const doc = JSON.stringify(r.Properties?.PolicyDocument || {});
    if (!/bedrock:InvokeModel/.test(doc)) continue;
    const regions = ['us-east-1','us-east-2','us-west-2']
      .filter(reg => doc.includes('bedrock:'+reg) || doc.includes(':'+reg+'::foundation-model'));
    if (regions.length === 3 && doc.includes('inference-profile')) agentBedrockOk = true;
  }
  if (!agentBedrockOk) { console.error('agent Bedrock grant missing 3-region FM + profile ARN set'); process.exit(1); }
  console.log('ok: 2 secrets, no JWT_SIGNATURE_SECRET env, no Bedrock Resource:* grant, MCP Lambdas carry both secret ARNs, agent Bedrock 4-ARN set present');
"
```
EXPECT: `ok: 2 secrets, no JWT_SIGNATURE_SECRET env, no Bedrock Resource:* grant, MCP Lambdas carry both secret ARNs, agent Bedrock 4-ARN set present`.

### Gate 6 — Threat-model row state check

```
grep -n 'ADR 0006 will revisit\|slice-9 fix (ADR 0006)' docs/threat-model.md
```
EXPECT: zero matches (the forward-references to ADR 0006 have all been flipped to backward references citing the commit).

### Gate 7 — git grep cleanliness (post-secret-removal)

```
git grep -i 'JWT_SIGNATURE_SECRET\|jwt-signature-secret' -- \
  ':!docs/adr/' ':!docs/threat-model.md' ':!tasks/' ':!.claude/'
```
EXPECT: zero hits in `lib/` and `lambdas/`. Hits in ADR / threat-model / tasks / Ralph archives are allowed (durable historical context).

## 13. Constraints inherited

- **Zero `slice X` / `T#` / `Task N` / `Checkpoint A-Z` / `phase N`** references in any new file (global CLAUDE.md rule).
- **Zero nonsense filler** in any new file (`basically`, `simply`, `obviously`, `essentially`, `merely`, `kind of`).
- **Multi-reviewer gate** at the end: code-reviewer five-axis → security-auditor → test-engineer → code-reviewer comments-focused. Sequential per memory `feedback_subagents_sequential`.
- All Python tests run via `.venv-tests/Scripts/python.exe`.
- All construct tests synthesise with `'aws:cdk:bundling-stacks': []` context to skip Docker bundling (per memory `project_cdk_test_invocation_gotchas`).
- Pytest invocation for `lambdas/notifier/tests/` runs from the package root (`cd lambdas/notifier && pytest tests/`); same gotcha may apply to other lambda packages with `tests/conftest.py`.

## 14. Step-by-step

### Task 1: CREATE `lib/secrets.js` — SecretsConstruct

- **ACTION**: New CDK construct exporting `this.agentJwtSecret` + `this.pollerJwtSecret`. Both are `secretsmanager.Secret` with `generateSecretString: { passwordLength: 40, excludePunctuation: true }`. Names: `trip-tracker-agent-jwt-signer` + `trip-tracker-poller-jwt-signer`.
- **MIRROR**: JSDoc header + design-choices block from `lib/poller-server.js:12-40`.
- **VALIDATE**: `node -e "const S = require('./lib/secrets'); console.log(typeof S);"` prints `function`.

### Task 2: UPDATE `lib/strands-agent-on-lambda-stack.js`

- **ACTION**: Delete the literal at line 20. Instantiate `SecretsConstruct` early, before the constructs that consume the secrets. Replace every `jwtSignatureSecret: JWT_SIGNATURE_SECRET` prop assignment (4 places: agent, flights, hotels, poller) with the appropriate secret-instance prop:
  - Agent: `agentJwtSecret: secrets.agentJwtSecret`
  - Flights: `agentJwtSecret: secrets.agentJwtSecret, pollerJwtSecret: secrets.pollerJwtSecret`
  - Hotels: same as flights
  - Poller: `pollerJwtSecret: secrets.pollerJwtSecret`
- **VALIDATE**: `node -e "require('./lib/strands-agent-on-lambda-stack');"` exits 0.

### Task 3: UPDATE `lib/agent.js`

- **ACTION**: (a) Drop the `JWT_SIGNATURE_SECRET` env var from the agent Lambda's environment block. (b) Add `AGENT_JWT_SECRET_ARN: props.agentJwtSecret.secretArn`. (c) Add `AGENT_BEDROCK_MODEL_ID: agentBedrockModelId` env var; read `agentBedrockModelId` from CDK context with the default `'us.anthropic.claude-3-5-haiku-20241022-v1:0'`; throw at synth if blank/whitespace (mirror `lib/poller-server.js:79`). (d) Replace the `resources: ['*']` Bedrock grant with the **4-ARN** resource-scoped grant per the §7 pattern: foundation-model ARN in us-east-1/us-east-2/us-west-2 (strip the `us.` prefix for the FM id) + the inference-profile ARN. (e) Call `props.agentJwtSecret.grantRead(travelAgentFn)`.
- **MIRROR**: §7 "RESOURCE-SCOPED BEDROCK GRANT" agent block (NOT the poller's single-region shape); SecretsManager `grantRead` is the CDK idiom.
- **VALIDATE**: `node -e "require('./lib/agent');"` exits 0. Group C jest tests pass.

### Task 4: UPDATE `lib/poller-server.js`

- **ACTION**: (a) Drop the `JWT_SIGNATURE_SECRET` env var. (b) Add `POLLER_JWT_SECRET_ARN: props.pollerJwtSecret.secretArn`. (c) Call `props.pollerJwtSecret.grantRead(pollerFn)`.
- **VALIDATE**: `node -e "require('./lib/poller-server');"` exits 0.

### Task 5: UPDATE `lib/flights-mcp-server.js` + `lib/hotels-mcp-server.js` (CDK wiring)

- **ACTION** (both files): (a) Drop `JWT_SIGNATURE_SECRET` from BOTH Lambdas' env blocks (server Lambda + authorizer Lambda). (b) On BOTH the server Lambda AND the authorizer Lambda, add `AGENT_JWT_SECRET_ARN: props.agentJwtSecret.secretArn` + `POLLER_JWT_SECRET_ARN: props.pollerJwtSecret.secretArn`. (c) Call `props.agentJwtSecret.grantRead(...)` + `props.pollerJwtSecret.grantRead(...)` for BOTH `serverFn` AND `authorizerFn`.
- **GOTCHA (§0 finding #1/#8 — the original GOTCHA was wrong)**: The server Lambda handler (`lambdas/flights-mcp/index.js:56`, `hotels-mcp/index.js:55`) DOES `jwt.verify` in-handler as documented defense-in-depth. It IS a verifier. The server Lambda must get both secret ARNs + grantRead, not just the authorizer. Confirmed by reading both `index.js` files (lines 23/56, 22/55).
- **VALIDATE**: `node -e "require('./lib/flights-mcp-server'); require('./lib/hotels-mcp-server');"` exits 0.

### Task 5b: UPDATE `lambdas/flights-mcp/index.js` + `lambdas/hotels-mcp/index.js` (handler verifier)

- **ACTION** (both files): Replace `const JWT_SIGNATURE_SECRET = process.env.JWT_SIGNATURE_SECRET` + the single `claims = jwt.verify(token, JWT_SIGNATURE_SECRET)` with the triplicated copy of the §7 Node lazy `getSecret` helper + the two-secret/sub-coupling verify (try agent secret → require `sub==='travel-agent'`; else try poller secret → require `sub==='trip-tracker-poller'`; else 401). Keep the in-handler verify and its defense-in-depth comment. Add `@aws-sdk/client-secrets-manager` to each package.json.
- **GOTCHA**: `jwt.verify(token, undefined)` throws synchronously — `getSecret` MUST resolve before the first verify. The per-secret try/catch falls through without leaking which secret failed.
- **VALIDATE**: Group F `node --test` cases pass in both packages.

### Task 6: UPDATE `lambdas/mcp-authorizer/index.js` + `package.json` + add a test runner

- **ACTION**: (a) Add `@aws-sdk/client-secrets-manager` + `jsonwebtoken` to `package.json` deps if absent; add `"test": "node --import ./tests/setup.js --test \"tests/*.test.js\""` + create `tests/setup.js` (the package has no runner today — §0 finding #6). (b) Rewrite the handler with the §7 lazy `getSecret` helper (NOT module-load) + the two-secret/sub-coupling verify: try agent secret → require `sub==='travel-agent'`; else try poller secret → require `sub==='trip-tracker-poller'`; any other path Deny.
- **GOTCHA**: `jwt.verify` throws on any failure (bad sig, expired, malformed) AND if the secret arg is `undefined`. The try/catch around each verify must fall through without leaking the wrong-secret diagnostic, and `getSecret` must resolve first.
- **VALIDATE**: Group D `node --test` cases pass (`cd lambdas/mcp-authorizer && npm test`).

### Task 7: UPDATE `lambdas/poller/jwt_signer.py` + tests

- **ACTION**: (a) `SUBJECT = "trip-tracker-poller"` (was `"travel-agent"`). (b) Replace the env-var read at line 27 with the §7 lazy `_get_secret()` helper reading `POLLER_JWT_SECRET_ARN` (fetch on first `sign_for_user()`, NOT at import — §0 finding #3). (c) Failure-mode message names the new env var.
- **GOTCHA**: `test_jwt_signer.py:25-31` (`_fresh_signer`) and `conftest.py:224,236` import the module after only setting env. With lazy fetch, the test patches `monkeypatch.setattr(jwt_signer, "_secrets", FakeSecretsClient(...))` AND resets `jwt_signer._cached_secret = None` BEFORE the first `sign_for_user()` — import order no longer matters.
- **VALIDATE**: Group E E1-E5 pass; Gate 2 stays green.

### Task 8: UPDATE `lambdas/travel-agent/mcp_client_manager.py` + `agent_config.py` + `app.py`

- **ACTION (mcp_client_manager.py)**: Replace `jwt_signature_secret = os.environ['JWT_SIGNATURE_SECRET']` (line 44) with the §7 lazy `_get_secret()` reading `AGENT_JWT_SECRET_ARN`. The `sub: "travel-agent"` claim is unchanged.
- **ACTION (app.py — §0 finding #2)**: Delete the dead line 33 `JWT_SIGNATURE_SECRET = os.environ['JWT_SIGNATURE_SECRET']`. It is never referenced (app.py verifies via RS256/Cognito-JWKS at lines 41,51). One-line delete, no replacement — app.py is NOT an HS256 verifier.
- **ACTION (agent_config.py)**: Change `model_id="us.anthropic.claude-3-5-haiku-20241022-v1:0"` to `model_id=os.getenv("AGENT_BEDROCK_MODEL_ID", "us.anthropic.claude-3-5-haiku-20241022-v1:0")`.
- **VALIDATE**: `python -c "import app"` (with the OTHER required env vars set, WITHOUT `JWT_SIGNATURE_SECRET`) exits 0 — proves the dead read is gone; existing travel-agent tests still pass.

### Task 9: UPDATE flights/hotels MCP test setup + handler tests (`node --test`)

- **ACTION**: In `lambdas/flights-mcp/tests/setup.js` + `lambdas/hotels-mcp/tests/setup.js`, replace `process.env.JWT_SIGNATURE_SECRET ??= 'test-secret'` with `AGENT_JWT_SECRET_ARN` + `POLLER_JWT_SECRET_ARN` env stubs AND a stub of the §7 `getSecret` helper that returns fixed fake secrets (the handler now fetches — §0 finding #8). Rewrite each `handler.test.js`: existing happy path = agent secret + `sub:travel-agent`; ADD Group F F2-F6 (poller-sub allow, both cross-sub forgeries denied 401, foreign-secret denied, post-verify RPC regression).
- **GOTCHA**: Runner is `node --test` via `npm test` in each package — NOT jest (§0 finding #6).
- **VALIDATE**: `cd lambdas/flights-mcp && npm test` and `cd lambdas/hotels-mcp && npm test` both green.

### Task 10: CREATE ADR 0006 + UPDATE threat-model + ADR index

- **ACTION (ADR)**: `docs/adr/0006-per-component-jwt-secrets.md` with Status: Accepted (date 2026-05-15), Context (threat-model rows at lines 64 + 153), Decision (HS256-with-two-secrets + per-component sub; reject RS256; triplicated verifier + why not shared; 3-US-region Bedrock grant), Consequences (manual rotation, secret-value-not-in-repo, blast radius scoped per minter, triplication-drift bounded by tests, non-US profile needs code change).
- **ACTION (threat-model.md)**: Update rows at lines 64 + 153 — flip the "Fix" column from forward-reference to backward-reference citing this PRP's commit + ADR 0006. Append a change-log entry.
- **ACTION (ADR README)**: Add row for ADR 0006.
- **VALIDATE**: Gate 6 passes (no remaining `ADR 0006 will revisit` strings).

### Task 11: UPDATE poller integration mock authorizers (§0 finding #5)

- **ACTION**: In `lambdas/poller/tests/test_e2e_poll.py:51` and `test_handler_with_mcp.py:62`, flip the mock-authorizer sub check to accept `sub == "trip-tracker-poller"` (the poller's new sub) under the poller secret. Update the call assertion at `test_handler_with_mcp.py:192` (`all(c["sub"] == ...)`) and the dedicated wrong-sub reject test at `:305-335` to the new sub.
- **GOTCHA**: These mocks mirror the production authorizer's contract; keep them faithful to the new two-secret/sub-coupling rule, not just a string swap — a poller-secret-signed `travel-agent` token must still be rejected by the mock.
- **VALIDATE**: Gate 2 green (Group E E6-E8).

## 15. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Authorizer's two-secret verifier accepts a token signed with the wrong secret under one of the sub branches | LOW | HIGH | Group D tests D3 + D4 explicitly assert cross-sub forgery is rejected. Code review at the multi-reviewer gate covers the verifier's branch logic specifically. |
| Lazy Secrets Manager fetch fails on first sign/verify (network blip, IAM misconfig) | MED | HIGH | Failure raises on first sign/verify — fail-loud, surfaces in CloudWatch with a clear error on the first real request. Lambda async retry handles the transient case. The lazy shape (vs import-time) is required so tests can stub the client (§0 finding #3). Acceptable. |
| Secret-value rotation breaks running invocations | LOW | MED | Manual rotation procedure documented in ADR 0006. CloudWatch alarm on authorizer 4xx-after-success would catch a half-rotated state; alarm is out of scope per §10 but the failure mode is documented. |
| `agentBedrockModelId` context default drifts from the literal in `agent_config.py` | LOW | LOW | The Group C C5 test explicitly asserts that the CDK context value propagates to both the IAM grant AND the env var; the env var is read by `agent_config.py` via the new `os.getenv(...)` line. A test fails before deploy if the two sources disagree. |
| Existing flights/hotels handler tests break because the test secret no longer matches the runtime env-var name | MED | LOW | Task 9 explicitly migrates `setup.js` in both packages and updates `handler.test.js` to use the new secret name + sub. Gate 3 catches a missed update. |
| Dropping `JWT_SIGNATURE_SECRET` from the flights/hotels SERVER Lambdas breaks the in-handler verify | **CONFIRMED, not a risk — designed for** | HIGH | §0 finding #1/#8: the server handlers DO `jwt.verify` in-handler (`flights-mcp/index.js:56`). Task 5 + 5b give the server Lambdas both new secret ARNs + the triplicated verifier. Gate 5 assertion #4 fails the build if any MCP Lambda lacks the secret-ARN env vars. The original "verified safe to drop" claim was false and is corrected. |
| Migration to per-component sub breaks the agent's own MCP-client minting code | MED | HIGH | The agent's `mcp_client_manager.py` mints `sub: "travel-agent"` and the agent's secret stays paired with that sub. The change is "poller flips sub, agent keeps sub." Easy to verify in the multi-reviewer gate: grep for `sub` in both minters and confirm exactly one is `travel-agent` and exactly one is `trip-tracker-poller`. |

---

## What "done" looks like

- 1 new CDK construct (`lib/secrets.js`).
- CDK/stack files updated: stack, agent.js, poller-server.js, flights-mcp-server.js, hotels-mcp-server.js.
- Lambda source updated: mcp-authorizer (index.js + package.json + new tests/setup.js + test script), flights-mcp & hotels-mcp (index.js + package.json), jwt_signer.py, mcp_client_manager.py, agent_config.py, app.py (one-line delete).
- New/updated tests: Group A/B/C (jest), Group D (mcp-authorizer `node --test`), Group E (poller pytest, incl. integration mocks), Group F (flights/hotels `node --test`).
- 1 new ADR (`docs/adr/0006-per-component-jwt-secrets.md`); 2 threat-model rows flipped forward→backward + change-log entry; 1 ADR index row added.
- All 7 validation gates green (Gate 3 now runs 4 commands: jest + 3× `node --test`).
- Hard-coded `JWT_SIGNATURE_SECRET` literal removed; `git grep` in `lib/` + `lambdas/` returns zero hits; no dead `os.environ['JWT_SIGNATURE_SECRET']` in app.py.
- Agent Bedrock grant carries exactly 4 resource ARNs (3 US foundation-model regions + inference-profile); no `Resource: '*'` for `bedrock:InvokeModel*` anywhere; both MCP server Lambdas carry both secret-ARN env vars and run the triplicated two-secret verifier.

## Confidence

**8/10 → 7/10 after the adversarial pass.** The two work items are still well-bounded, but the §0 review widened scope materially: the verifier is triplicated across 3 Lambda packages (drift risk, bounded by Group D + Group F), the secret fetch had to move to lazy-cached, and Gate 3 split into 4 runners. The single largest residual risk is a triplicated-verifier copy drifting between the authorizer and the two server handlers — the per-location test groups assert the identical cross-sub-forgery + foreign-secret invariants, and the sequential multi-reviewer gate (security-auditor in particular) inspects all three copies for divergence. The 4-ARN Bedrock grant and the lazy-fetch test seam are pinned by Group C and Group E respectively.
