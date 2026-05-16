# ADR 0006 — Per-component JWT signing secrets via Secrets Manager

**Date:** 2026-05-15
**Status:** Accepted

## Context

Two internal components mint HS256 JWTs for the MCP call path: the chat
agent (`lambdas/travel-agent/mcp_client_manager.py`) and the poller
(`lambdas/poller/jwt_signer.py`). Until now both signed with a single
shared secret AND both presented `sub: "travel-agent"`. Two defensive
properties the threat model claims were therefore not actually held:

1. **Secret-out-of-repo.** The shared secret was a hard-coded literal in
   `lib/strands-agent-on-lambda-stack.js`. Anyone with repo access could
   mint a token every verifier accepts (`docs/threat-model.md` Secrets
   row, line 64).
2. **Component-level identity at the auth boundary.** With one secret and
   one `sub`, a compromised poller could mint tokens indistinguishable
   from the agent's. The MCP authorizer could not tell them apart
   (`docs/threat-model.md` boundary [5] / [2] row).

The verifier is not only the API Gateway Token Authorizer Lambda
(`lambdas/mcp-authorizer/index.js`): both MCP server handlers
(`lambdas/flights-mcp/index.js`, `lambdas/hotels-mcp/index.js`)
re-verify the bearer JWT in-handler as documented defense in depth. Any
fix has to cover all three verifier sites, not just the authorizer.

## Decision

- **Two Secrets Manager secrets, one per minter.**
  `trip-tracker-agent-jwt-signer` and `trip-tracker-poller-jwt-signer`,
  created by CloudFormation at deploy time via `generateSecretString`
  (40 alphanumeric chars, punctuation excluded). No value ever in the
  repo. A new `SecretsConstruct` (`lib/secrets.js`) owns creation; each
  consuming construct scopes its own least-privilege
  `secretsmanager:GetSecretValue` via `grantRead`, never `Resource: '*'`.

- **Per-component `sub`.** The agent keeps `sub: "travel-agent"`; the
  poller switches to `sub: "trip-tracker-poller"`.

- **Secret-to-sub coupling inside every verifier.** A token must verify
  under exactly one secret AND carry that secret's allowed sub
  (`AGENT_JWT_SECRET_ARN → travel-agent`,
  `POLLER_JWT_SECRET_ARN → trip-tracker-poller`). Without the coupling a
  leaked agent token would still pass as a poller token. The agent + the
  two MCP server handlers read both secret ARNs; the minters read only
  their own.

- **HS256 with two secrets, not RS256.** Asymmetric keys would force
  every minter to ship a private-key library and every verifier to hold
  a public-key cache or fetch JWKS. Disproportionate at single-developer
  scale. Revisit only if a verifier ever lives in a less-trusted zone
  than the minters.

- **The two-secret verifier is triplicated, not shared.** The ~15-line
  verify block is copied verbatim into the authorizer and both MCP
  server handlers. A cross-Lambda-package shared module needs a Lambda
  layer or a monorepo symlink — both larger than this hardening change
  and a new failure surface. Per-package tests pin the identical
  cross-sub-forgery + foreign-secret invariant so a copy that drifts
  fails before production.

- **Secret fetch is lazy + cached on first use, not import-time.** An
  import-time fetch fires before any test can stub the Secrets Manager
  client. A `_get_secret()` / `getSecret()` helper memoizes per warm
  execution environment; fail-loud moves from import to first sign/verify.

- **The agent's Bedrock IAM grant is scoped to its geographic profile.**
  `us.anthropic.claude-3-5-haiku-20241022-v1:0` is a `us.`-prefixed
  geographic inference profile; Bedrock routes through it to one of
  us-east-1 / us-east-2 / us-west-2 and authorizes against the
  foundation-model ARN in the landing Region. The grant enumerates the
  foundation-model ARN in all three US Regions plus the inference-profile
  ARN (4 ARNs), replacing the previous `Resource: '*'`. The model id is
  injected via `AGENT_BEDROCK_MODEL_ID` from the same CDK context value
  that derives the ARNs, so the grant and the invoked model cannot drift.

- **Rotation is manual in v1.** No rotation Lambda. To rotate: put a new
  `SecretString` on the secret in the AWS console, then redeploy (or
  recycle the warm Lambdas) so the lazy cache refetches. A correct
  rotation Lambda must coordinate minter + verifier rollover and is out
  of scope.

## Consequences

**Good:**

- Repo access yields zero JWT-forging capability — no signing material
  in the tree.
- A compromised poller secret cannot mint agent-claiming tokens: every
  verifier rejects a poller-secret-signed `sub: travel-agent` token (and
  vice versa). Blast radius is scoped per minter.
- Least-privilege reads: each minter reads only its own secret; the
  verifiers read both; no Secrets Manager grant is wildcarded.
- The widest remaining IAM grant (the agent's `bedrock:InvokeModel*`
  `Resource: '*'`) is closed; no Bedrock action is wildcard-scoped
  anywhere in the stack.

**Costs / limits:**

- Two Secrets Manager secrets (~$0.40/mo each) plus per-call
  `GetSecretValue` on cold start. Acceptable for the security gained.
- The verify block exists in three files. Drift risk is real; it is
  bounded by the per-package test groups asserting the identical
  invariant. Edit all three copies together.
- The geographic-profile destination Region set (us-east-1, us-east-2,
  us-west-2) is hardcoded from the `us.` prefix. A non-US profile
  (`eu.`, `apac.`) needs a code change to that list — a deliberate scope
  boundary, not a prefix→regions lookup table.
- Rotation is a manual console + redeploy step until a rotation Lambda
  is justified.

## Closes

- `docs/threat-model.md` Secrets row (the hard-coded shared literal).
- `docs/threat-model.md` boundary [5] / [2] compromised-poller row
  (component identity at the auth boundary).
- The agent's `bedrock:InvokeModel*` `Resource: '*'` grant.
