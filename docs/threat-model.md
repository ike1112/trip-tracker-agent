# Threat model — trip-tracker

**Date:** 2026-05-10
**Status:** Living document. Updated each slice that crosses a new trust boundary.
**Scope:** v1 personal-use deployment. The "what changes for production" notes
under each section call out the gap.

## Trust boundaries

The system has four distinct trust boundaries. Each crossing point is where
this document focuses — what flows across, what verifies it, what happens
on failure.

```
   ┌──── Browser (Cognito-authed user) ────┐
   │                                       │
[1]│ HTTPS + JWT (RS256)                   │
   ▼                                       │
[ API Gateway ] ── token authorizer ─→ [ travel-agent Lambda ]
   │                                       │
[2]│ HTTPS + JWT (HS256, internal)         │
   ▼                                       │
[ MCP API GW ] ── token authorizer ─→ [ flights-mcp Lambda ]
                                           │
                                       [3] │ HTTPS + Bearer key
                                           ▼
                                       [ Duffel API ]
                                           │
                                       [4] │
                                           ▼
                                       AWS account
                                       (S3, DDB, Bedrock, SES, CloudWatch)
```

[1] Browser ↔ API Gateway. Cognito-issued RS256 JWT.
[2] travel-agent ↔ MCP server. Internally-signed HS256 JWT.
[3] MCP server ↔ Duffel. Provider-issued Bearer key.
[4] Lambda ↔ AWS services. IAM roles, scoped per Lambda.

## JWT chain

```
   Cognito  ──issues──▶  RS256 JWT  ──verified by──▶  travel-agent (jwks)
                                                          │
                                              signs new HS256 JWT
                                                          ▼
                                  internal token  ──verified by──▶  mcp-authorizer (HS256)
                                                          │
                                              re-verified inside
                                                          ▼
                                                     flights-mcp Lambda
```

The user's identity is propagated as a `user_id` claim inside the HS256
internal token, so the MCP server knows *which* end user the agent is
acting on behalf of. The MCP server cannot trust the agent blindly —
even though they're in the same AWS account, it re-verifies the JWT in
its own handler as defense in depth (slice 3, `lambdas/flights-mcp/index.js`).

## Secrets

| Secret | Where it lives | Lifetime | Notes |
|---|---|---|---|
| Agent JWT signer | Secrets Manager (`trip-tracker-agent-jwt-signer`); read by the agent minter + both MCP verifier sites | Manual rotation (console + redeploy) | Per-component secret, `sub: travel-agent`. No value in the repo (ADR 0006). |
| Poller JWT signer | Secrets Manager (`trip-tracker-poller-jwt-signer`); read by the poller minter + both MCP verifier sites | Manual rotation (console + redeploy) | Per-component secret, `sub: trip-tracker-poller`. Replaces the shared hard-coded literal (ADR 0006). |
| Cognito signing keys | Cognito-managed (JWKS endpoint) | Rotated by Cognito | Public keys only on our side. |
| `DUFFEL_API_KEY` | Lambda env var, flights-mcp only | Rotated by hand at the provider | Only present if `MCP_MODE=live`. Fixture deploys leave it empty. |
| `LITEAPI_API_KEY` | Same pattern (slice 4) | | |

**JWT signing secrets** are now per-component values in AWS Secrets
Manager (ADR 0006), read lazily at first sign/verify and scoped per
least-privilege `grantRead`. The two **provider API keys**
(`DUFFEL_API_KEY`, `LITEAPI_API_KEY`) remain Lambda env vars:
- Single-tenant personal use; the radius of a provider-key leak is one
  developer, not customers.
- They are only populated on `MCP_MODE=live` deploys; fixture deploys
  leave them empty.

**What would change for production:** the provider API keys also move to
Secrets Manager with automatic rotation, and the JWT signing secrets
gain an automatic rotation Lambda (manual console + redeploy rotation
today — ADR 0006).

## Boundary-by-boundary threats

### [1] Browser → API Gateway

| Threat | Mitigation |
|---|---|
| Forged / unsigned JWT | API Gateway Token Authorizer rejects with 401 before the agent Lambda is invoked. |
| Expired JWT | Authorizer rejects. Cognito refresh flow on the client. |
| JWT replay across users | Each request re-validates; tokens carry `sub` and `exp`. |
| MITM | TLS 1.2+, API Gateway managed certs, HSTS on the web origin. |
| Cross-user data access via the LLM | **ADR 0001** — watch CRUD tools are user-scoped closures; `user_id` is never an LLM-visible parameter. Re-enforced at the DDB key. Tested in `tests/test_watches.py`. |

### [2] travel-agent → MCP servers (internal JWT)

| Threat | Mitigation |
|---|---|
| Calling MCP from outside the agent | Per-component HS256 JWT required (ADR 0006). The verifier couples each secret to its allowed `sub` — `travel-agent` under the agent secret, `trip-tracker-poller` under the poller secret — and denies any cross-sub or foreign-secret token. Enforced identically at the API-GW authorizer AND in-handler at both MCP servers. |
| Tampered / forged internal JWT | Signature verification pinned to `algorithms: ['HS256']` (blocks `alg:none` and RS/HS confusion explicitly, not by library default); tokens without `exp` are rejected (expiry enforced at the boundary, not assumed from the minter). Applied in all three verifier sites. |
| Token leak via logs | The agent never logs the full token; the MCP Lambda logs `user_id_prefix` (first 8 chars) only. |
| MCP server impersonation | The agent calls a CDK-output URL, not user-supplied. Endpoint env vars are stack-injected. |

### [3] MCP server → Duffel API

| Threat | Mitigation |
|---|---|
| `DUFFEL_API_KEY` leak via logs | The live client never logs the key. Lambda env vars don't appear in CloudWatch logs unless explicitly printed. Spot-check before each deploy. |
| `DUFFEL_API_KEY` leak via responses | The live client only reads the response body; the key is in the request header. The Lambda's response body to the agent contains *no* Duffel credentials. |
| Duffel rate-limit exhaustion | Provider-side rate limits act as a circuit breaker. Slice 5 poller adds local rate limiting (sequential per-watch loop, no parallel fan-out). |
| Duffel response contains prompt-injection content | **Tool results never go back into a system prompt.** They are passed to the LLM as tool-result content blocks, which the model treats as data, not instructions. Note this is *not* perfect — a sufficiently persuasive payload can still influence the model. The closure-factory pattern (ADR 0001) means the worst it can do is misinform the user, not exfiltrate cross-user data. |
| Duffel key revoked / billing failure | The live client throws a clear error; the agent surfaces "flight search is currently unavailable." Fixture mode is the fallback path for demos. |
| Untrusted upstream change (Duffel response shape) | Fixture tests catch breakage in the agent path; the live client's normaliser is the seam. |

**What would change for production:** add structured request/response
sampling to a private S3 bucket for replay debugging; add a token-bucket
rate limiter inside the live client; rotate Duffel keys quarterly.

### [3b] MCP server → LiteAPI

Same shape as the Duffel boundary; threats below are the deltas worth
calling out, not a re-listing of [3].

| Threat | Mitigation |
|---|---|
| `LITEAPI_API_KEY` leak via logs / responses | Same as Duffel: header-only, never logged, never echoed back to the agent. |
| Slow upstream blocks the Lambda's whole budget | **20s `AbortSignal.timeout` on every LiteAPI fetch** inside the live client. Lambda timeout is 30s, so the fetch failing fast leaves 10s for serialization + X-Ray flush. Hard failure beats tail-latency death. |
| Silent currency conversion | **Live client throws if LiteAPI returns a non-USD currency.** The watch system tracks USD totals; silent unit conversion would corrupt the FareHistory time series in a way that's invisible to the user *and* permanent. Failing loud preserves data integrity. |
| Hotel listing contains prompt-injection content (name, address, amenities) | Same as Duffel — tool results are content blocks, never system-prompt. Closure-factory caps the worst case at "misinform the user," not data exfiltration. Hotel descriptions are a larger attack surface than flight metadata, so this matters more here. |
| LiteAPI returns hundreds of listings, blowing the LLM context | **Top-N (5) cap inside the live client**, sorted by total price before truncation. The fixture client mirrors this implicitly via fixture-file size. |
| LiteAPI rate-limit exhaustion | Provider-side rate limits + the poller's sequential per-watch loop (slice 5, ADR 0003). At personal scale (≤10 watches, 4h cadence) the effective rate is < 1 call/min. |

**What would change for production:**
- Token-bucket rate limiter inside the live client (independent of the
  poller-level limiter, so chat usage and polling can't add up to a
  burst).
- Currency conversion at a single, audited point if multi-currency
  tracking is ever needed — never as a silent fallback in the live client.
- Per-hotel sanitisation pass on returned text (length cap, strip control
  characters) as a second-layer defense against prompt-injection in user-
  generated property descriptions.

### [5] Poller → AWS services + MCPs

The trip-tracker poller (slice 5) is a second internal caller of the MCP
boundary [2]. EventBridge invokes it on a 4-hour cron; per invocation it
walks every active `Watches` row, signs an HS256 JWT scoped to that
watch's owner, and calls flights-mcp + hotels-mcp. The poller writes
`FareHistory` snapshots to DDB and (slice 6+) will invoke Bedrock for
the alert decision.

| Threat | Mitigation |
|---|---|
| Compromised poller mints tokens that a verifier accepts as the agent | **Closed (ADR 0006).** The agent and poller now sign with separate Secrets Manager secrets and present distinct `sub` claims (`travel-agent` vs `trip-tracker-poller`). Every verifier — the MCP authorizer Lambda AND both MCP server handlers — couples each secret to its allowed `sub`, so a poller-secret-signed `travel-agent` token (or the reverse) is rejected. Pinned by the cross-sub-forgery cases in the authorizer (Group D) and both server handlers (Group F). |
| One bad watch's MCP failure starves all the others | **Sequential per-watch loop with per-watch try/except (ADR 0003).** `McpCallError`, `ValueError`, `KeyError` are categorised and skipped; `watches_errored` metric increments; the loop continues. Verified by `tests/test_handler_with_mcp.py::test_one_failing_mcp_does_not_block_other_watches`. |
| Cron-triggered code parses untrusted user input from the EventBridge event | The handler does not read any field from the `event` payload — schedule envelopes are ignored. The only inputs are the DDB rows themselves and the MCP responses, both of which are validated downstream. |
| MCP-response prompt injection / payload bombs | T2's `_NoRedirectHandler` blocks SSRF via redirect; `MAX_RESPONSE_BYTES = 2MB` cap on the body; T3's `_validate_deep_link` rejects non-`https://` and >2KB strings before they land in DDB. |
| Currency drift silently corrupts the FareHistory price series | T3's snapshot composer raises `ValueError` on any non-USD `currency` field in any offer/hotel — caught as `watch_errored`. Mirrors the LiteAPI live-client posture in [3b]. |
| New row poisoning its own anomaly baseline | T4's history window is fetched **before** writing the new snapshot; the exclusive `>` boundary in `get_window` then naturally excludes anything written at exactly `now`. No fragile equality filter needed. |
| Anomaly history truncated by DDB pagination | T4's `get_window` follows `LastEvaluatedKey` so a watch with months of history doesn't silently see only the first 1MB page. |
| Reflected MCP error body leaks reflected request fragments to logs | The `watch_errored` log explicitly omits `e.body`; only the categorised `reason` + HTTP status are surfaced. Verified by `test_handler_with_mcp.py::test_watch_errored_log_does_not_carry_response_body`. |
| Concurrent EventBridge ticks fan out parallel pollers | Lambda `reservedConcurrentExecutions = 1` queues a second invocation rather than running it. Free defence against accidental cron-config drift. |

**Done (ADR 0006):** per-component JWT signing secrets + per-component
`sub`, so a compromised poller cannot mint tokens that look like the
agent and vice versa.

**What would still change for production:** an automatic secret-rotation
Lambda (rotation is manual console + redeploy today); per-watch IAM
scoping if multi-tenant work ever lands; a poller-specific X-Ray
service-map node so the cron trace is independently visible from the
chat path.

### [6] Poller → Bedrock InvokeModel

The poller (slice 5 onward) calls `bedrock-runtime.invoke_model` once
per watch when the gate cascade reaches the decision layer. The call
goes out under the Lambda's IAM role over the AWS-internal control
plane — same blast radius as boundary [4] but with provider-controlled
strings (hotel names, airline codes from Duffel/LiteAPI responses)
flowing inside the prompt body. ADR 0004 documents the model choice
and the strict-JSON contract.

| Threat | Mitigation |
|---|---|
| Prompt injection via `bestOfferBlob.hotelName` / `airline` (provider-controlled) | **System prompt contains only rubric text; provider strings interpolated into the user message only.** A sentinel-based test in `lambdas/poller/tests/test_bedrock_decide.py` group E asserts the system message never includes any provider string. The model treats the user message as data, not instructions; per ADR 0001 the closure-factory pattern caps the worst case at "misinform the user," not cross-user data exfiltration. |
| Cost runaway via abuse of `bedrock:InvokeModel` | **IAM grant resource-scoped to the model ARN** in `lib/poller-server.js` — not `bedrock:*` and not `Resource: '*'`. The poller cannot invoke any model other than the pinned `BEDROCK_MODEL_ID`. Reserved-concurrency = 1 + clamped poll cadence (15-1440 min, see ADR 0003) cap the rate at which the grant can fire. Dedup gate short-circuits ~80% of poll cycles before any Bedrock call. AWS Budget alarm at $10/mo (`BudgetAlarmConstruct`, `lib/budget-alarm.js`; design-spec §300) is the safety net. |
| Bedrock returns malformed / fence-wrapped / extra-keyed JSON | **Strict JSON-only parser in `bedrock_decide._parse_response`** — first char must be `{`, last `}`, top-level keys exactly `{alert, reason}`, types pinned (`bool` not `int`, non-empty string ≤200 chars). Any deviation routes to defensive fallback `{alert: False, reason: "model_response_invalid", bedrock_called: True}`. Six malformation cases covered in `test_bedrock_decide.py` group F. |
| Bedrock outage triggers a flood of "decision failed" emails | **Defensive fallback is no-alert.** Network / IAM / throttle / parse failures all collapse to `alert: False`; the metric `bedrock_decisions_made` still increments (we tried) but no user-visible alert is sent. The user sees zero alerts during an outage, not bad ones. |
| Model drift causes silent alert-quality regression | **Pinned model ID** (`BEDROCK_MODEL_ID` env var, defaults to `claude-haiku-4-5-20251001`) — Anthropic point releases don't change behaviour unless we deploy. **Eval framework** (`evals/`) — 33+ hand-labelled cases + Sonnet 4.6 judge surface drift the next time a developer runs `python evals/run_evals.py`. A future CI `workflow_dispatch` trigger will run the corpus on demand. |
| `ANTHROPIC_API_KEY` leak via report or logs (eval framework, not production) | Eval runner's `RunMetadata` carries only model ID, mode, and stub flag — never the API key. The judge client constructs `anthropic.Anthropic()` which reads `ANTHROPIC_API_KEY` from env automatically; the key never enters Python code that gets logged or written to a file. |
| Judge model manipulated by malicious `reason` from under-test model | **Acknowledged residual risk.** The judge sees the under-test model's `reason` string as user-message data. A persuasive injection by the under-test model could in theory flip a judge verdict. Local-only eval runs cap the blast radius at "developer sees a wrong pass/fail rating," not user-facing impact. Captured in `evals/judge_client.py` docstring. |
| `bedrockInferenceProfileArn` context override grants inference-profile access | The CDK construct synth-time validates the ARN format. The grant adds the inference-profile ARN as an additional resource (not a replacement) so the direct-model ARN remains scoped. |

**What would change for production:**
- AWS Budget alarm at $10/mo with a direct email subscriber — **built**
  (`BudgetAlarmConstruct`, `lib/budget-alarm.js`); not yet `cdk deploy`-ed.
  SNS / multi-channel fan-out is a deliberate non-goal (design-spec §300
  specifies email; SNS adds infra for no value at personal scale).
- Eval framework wired to CI on workflow_dispatch (slice 9) — manual trigger only to keep cost discipline.
- Periodic golden-set expansion as production traffic surfaces edge
  cases the hand-labelled corpus didn't cover.
- Cloudwatch alarm on `bedrock_decisions_made` spiking above the
  steady-state rate (would indicate a misbehaving cron, dedup
  regression, or stuck loop).

### [7] Notifier → SES + Watches DDB UpdateItem

The notifier Lambda is invoked async by the poller when
`decision.alert` is True. It composes a plain-text email from the
`(snapshot, watch, decision)` payload, sends via SES, and writes
`lastAlertedAt` + `lastAlertedPrice` back to the Watches row so the
dedup gate can do its job on the next poll. ADR 0005 documents the
at-least-once / price-proximity-dedup semantics.

| Threat | Mitigation |
|---|---|
| Notifier sends AS an unverified or attacker-controlled identity | **`ses:SendEmail` IAM grant resource-scoped to the sender identity ARN** assembled from the synth-time-validated `notifierSenderEmail` context. The notifier cannot send as any other identity in the account. The verified-identity setup itself is a manual out-of-band step (AWS console / `VerifyEmailIdentity` API); README documents it. |
| Recipient enumeration / spam | Single-recipient v1 deploys with the recipient pinned in CDK context at synth time. The notifier has no input that can redirect delivery. Multi-user lookup is an upgrade path (ADR 0005); when it lands, the Cognito-lookup path becomes a second injection surface to audit. |
| Email-body content controlled by the model output | **Plain-text email only.** The template's `reason` interpolation has no HTML escape (none needed — plain text IS the escape). Defence-in-depth: the upstream `bedrock_decide` parser already rejects HTML chars, C0 controls, bidi codepoints, and non-UTF-8 strings in `reason` (boundary [6]); the template strips CR/LF/controls from the subject line to defeat header injection even if the destination string is somehow polluted. |
| Notifier writes outside the Watches row | **`dynamodb:UpdateItem` IAM grant scoped to the Watches table only** — no put, no delete, no scan, no access to FareHistory. The `UpdateExpression` only sets `lastAlertedAt` + `lastAlertedPrice`; other Watches fields are untouched (test_writer Group D). |
| Out-of-order async retry backdates the dedup state | **Conditional update** with `attribute_not_exists(lastAlertedAt) OR lastAlertedAt < :now`. A retry whose clock lands before the first attempt's writeback fails the condition and raises `WritebackConflictError`; the alert was delivered, the handler logs and returns 200. |
| Duplicate alerts during retry | **Acknowledged residual risk.** Lambda async retry on SES failure may resend; a DDB write-after-SES-success failure leaves dedup state stale until the next poll. **Bound:** the price-proximity dedup band (5%) catches identical-price duplicates at the next poll. ADR 0005's "Cost" section names this trade-off explicitly. |
| Recipient email / model `reason` leak into structured logs | **Notifier side:** the `notification_sent` log carries `watch_id`, `user_id_prefix` (first 8 chars of the Cognito sub only), and `message_id`. Never the full recipient email, never the full `user_id`, never the `reason` string. Failure-path logs (`ses_send_failed`, `notifier_invoke_failed`) carry only the exception class name — the AWS error message body is dropped to defeat future error-class leaks (e.g. `MessageRejected: Email address is not verified: <recipient>`). Asserted by `test_handler.py` group F + `test_C4`. **Poller side** (separate audit point): the `decision_made` log carries the full `reason` string for debugging the model output; this is the audit channel for what the model decided. `reason` is already stripped of HTML/control/bidi chars by `bedrock_decide` (boundary [6]) so the log carries no exploitable content. |
| SES throttling / outage stalls alerts | Lambda async retry (default 2 retries) handles transient SES errors. After retry exhaustion the invocation is lost — acceptable for v1; a production deploy adds a DLQ + CloudWatch alarm on async-invoke failures. |

**What would change for production:**
- DLQ on the notifier's async-invoke configuration + CloudWatch
  alarm on DLQ depth.
- Bounce / complaint feedback via SNS topic, parsed by a small
  Lambda that flags problematic recipients in the Watches table.
- Multi-user recipient lookup via Cognito (replaces the
  CDK-context single-recipient pattern) — adds an authn-context
  injection surface to audit.
- HTML email template — requires autoescape at every
  interpolation point; not added in v1 because plain text is
  cheaper to keep safe.

### [4] Lambda → AWS services

| Threat | Mitigation |
|---|---|
| Over-broad IAM | Each Lambda gets a least-privilege role: travel-agent has read/write on Watches, read on FareHistory, S3 on a single bucket, Bedrock-invoke. flights-mcp has zero AWS-resource permissions beyond CloudWatch Logs + X-Ray. The poller adds `bedrock:InvokeModel` resource-scoped to the model ARN (ADR 0004 / boundary [6]) and `lambda:InvokeFunction` on the notifier ARN. The notifier adds `ses:SendEmail` resource-scoped to the sender identity (ADR 0005 / boundary [7]) and `dynamodb:UpdateItem` on the Watches table. |
| Account-wide blast radius from a compromised Lambda | Resource-scoped policies (table ARNs, bucket ARNs, model ARN, SES identity ARN, function ARN) prevent lateral movement. |
| Cost runaway | AWS Budget alarm at $10/mo (built — `BudgetAlarmConstruct`, `lib/budget-alarm.js`; pending `cdk deploy`). Poller-specific defences in boundary [6] cap the Bedrock surface independently. SES sandbox limits + the single-recipient pin cap the email surface. |

## Out of scope (explicit)

These are real risks the codebase does not address, by design for v1:

- DDoS / large-scale abuse. Single-user system; no public endpoint
  beyond Cognito-gated chat.
- AWS account takeover, root-credential compromise. Standard AWS
  account hygiene assumed.
- Supply-chain attack via npm / pypi packages. `package-lock.json` and
  pinned versions in `requirements.txt` are the only mitigation; a
  proper SBOM / signed-builds setup is a production concern.
- Insider threat — same.
- Mobile app / browser-extension surface — not built.

## Change log

- **2026-05-10** — initial draft alongside slice 3 (flights-mcp). Boundaries
  [1] [2] [3] documented. [4] sketched; will fill in as slices land.
- **2026-05-10** — slice 4: appended [3b] for the LiteAPI boundary
  (latency budget, currency strictness, top-N cap, prompt-injection in
  hotel descriptions).
- **2026-05-10** — slice 5: appended [5] for the poller as a second
  internal JWT minter and second crosser of boundary [2]. ADR 0003
  documents the sequential-loop isolation guarantee called out in this
  section. The shared-secret risk is now triple-tracked: original
  Secrets row (line 64), this section's first row, and a `TODO(slice-9)`
  comment in `lib/strands-agent-on-lambda-stack.js` — ADR 0006 will
  resolve it.
- **2026-05-13** — slice 6: appended [6] for the poller's Bedrock
  InvokeModel boundary (prompt injection via provider strings, cost
  runaway via the IAM grant, strict-JSON contract as the
  bad-output defence, model drift surfaced by the new `evals/` framework).
  ADR 0004 documents the model choice. [4]'s IAM row updated to
  reference the new resource-scoped Bedrock grant.
- **2026-05-13** — alert notifier: appended [7] for the
  notifier-to-SES + Watches DDB UpdateItem boundary (identity
  spoofing via the resource-scoped SES grant; email-body content
  control via plain-text + parser-hardened `reason`; out-of-order
  retry handled by the writer's conditional update; duplicate-alert
  window bounded by the dedup gate's price-proximity band). ADR
  0005 documents the at-least-once trade-off. [4]'s IAM row updated
  to reference the new SES + Lambda invoke grants.
- **2026-05-15** — ADR 0006: the shared-secret risk is **closed**. The
  hard-coded literal and its `TODO` comment are removed from
  `lib/strands-agent-on-lambda-stack.js`; the agent and poller now sign
  with separate Secrets Manager secrets and distinct `sub` claims, and
  every verifier (MCP authorizer + both MCP server handlers) couples
  each secret to its allowed `sub`. Supersedes the 2026-05-10
  change-log entry's "triple-tracked … will resolve it" note. The agent's
  `bedrock:InvokeModel*` `Resource: '*'` grant is also closed —
  scoped to the 3 US geographic-profile destination foundation-model
  ARNs + the inference-profile ARN. ADR 0006 documents the design.
