# Implementation Report — Per-component JWT secrets + agent Bedrock IAM tightening

**Plan:** `tasks/secrets-and-iam-hardening.prp.md`
**Completed:** 2026-05-16
**Iterations:** 1 (Ralph, max 10)

## Summary

Implemented ADR 0006: the agent and poller now sign MCP JWTs with separate
AWS Secrets Manager secrets and distinct `sub` claims (`travel-agent` vs
`trip-tracker-poller`). Every verifier — the MCP authorizer Lambda AND both
MCP server handlers (defense-in-depth, surfaced by the adversarial review) —
runs a triplicated two-secret + sub-coupling verifier. The hard-coded
`JWT_SIGNATURE_SECRET` literal is gone from the repo. Separately, the chat
agent's `bedrock:InvokeModel*` `Resource: '*'` grant is scoped to the four
ARNs its `us.` geographic inference profile actually needs (foundation-model
in us-east-1/us-east-2/us-west-2 + the inference-profile ARN).

## Tasks completed (12)

1. `lib/secrets.js` — SecretsConstruct (two `generateSecretString` secrets).
2. Stack — literal deleted, SecretsConstruct wired, props swapped.
3. `lib/agent.js` — AGENT_JWT_SECRET_ARN + AGENT_BEDROCK_MODEL_ID env,
   4-ARN Bedrock grant, blank-model synth guard, grantRead.
4. `lib/poller-server.js` — POLLER_JWT_SECRET_ARN env + grantRead.
5. `lib/flights-mcp-server.js` + `lib/hotels-mcp-server.js` — both server
   AND authorizer Lambdas get both secret ARNs + grantRead.
5b. `lambdas/flights-mcp/index.js` + `lambdas/hotels-mcp/index.js` —
    triplicated lazy two-secret verifier (the §0 finding #1/#8 fix).
6. `lambdas/mcp-authorizer/index.js` — same verifier; new `node --test`
   script + `tests/setup.js`.
7. `lambdas/poller/jwt_signer.py` — lazy `_get_secret()`, SUBJECT flip.
8. `mcp_client_manager.py` lazy agent-secret fetch; `app.py` dead read
   deleted; `agent_config.py` reads AGENT_BEDROCK_MODEL_ID.
9. flights/hotels test setup + handler tests (Group F, cross-sub cases).
10. ADR 0006 written; threat-model 2 rows flipped + changelog; ADR README.
11. Poller integration mock authorizers + conftest updated for new sub.

## Validation results (all 7 gates)

| Gate | Result |
|------|--------|
| 1 — notifier regression | 126 pass |
| 2 — poller + evals | 312 pass (206 + 106) |
| 3 — jest test/ (96) + mcp-authorizer 8 + flights 19 + hotels 18 | all pass |
| 4 — cleanliness | filler clean; `slice\d` only matched `String(...).slice(0,8)` (JS method, pre-existing untouched code — regex `[ -_]` range includes `(`; not a roadmap label) |
| 5 — full-stack synth IAM | pass |
| 6 — threat-model forward-refs | clean |
| 7 — git grep old secret in lib/+lambdas | clean |

## Codebase patterns discovered

- Secret fetch must be lazy + cached on first use, never import-time, so
  tests can stub the client before the first call (ADR 0006 finding #3).
- `_secrets = None` + create-on-first-fetch avoids boto3 `NoRegionError`
  in unit tests that never touch AWS.
- flights-mcp / hotels-mcp / mcp-authorizer run `node --test` via
  `npm test` (NOT jest); CDK construct tests use jest from repo root.
- The two-secret verifier is triplicated verbatim across three Lambda
  packages by design (ADR 0006 #11) — edit all three together; Group D +
  Group F pin the identical invariant.

## Deviations from plan

- The `@aws-sdk/client-secrets-manager` dependency was `npm install`-ed
  into all three Node packages (lockfiles updated). The PRP assumed the
  Lambda Node 22 runtime ships it; that is true at runtime but the local
  `node --test` runs need it resolvable, so it is now an explicit dep.
- Gate 4's ripgrep pattern `slice[ -_]?\d` over-matches the JS
  `String.prototype.slice(0, 8)` call in pre-existing `flights-mcp` /
  `hotels-mcp` index.js logging (the `[ -_]` ASCII range includes `(`).
  This is a false positive, not a roadmap label; the untouched legit
  code was deliberately not mangled to satisfy an over-broad regex.

## Outstanding

PRP §13 mandates a sequential 4-reviewer gate (code-reviewer five-axis →
security-auditor → test-engineer → code-reviewer comments) before this is
shippable. That is a separate phase, not a Ralph iteration.
