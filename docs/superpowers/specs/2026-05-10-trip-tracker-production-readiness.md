# Trip Tracker — Production-Readiness Companion Spec

**Date:** 2026-05-10
**Status:** Draft (awaiting review)
**Owner:** Isabel
**Companion to:** [`2026-05-08-trip-tracker-agent-design.md`](./2026-05-08-trip-tracker-agent-design.md)

---

## 1. Scope and relationship to the design spec

The 2026-05-08 design spec answers *what* we are building and *why*. It stays untouched.

This companion spec answers a different question:
**"How does this codebase signal production-grade engineering judgement to a reviewer who forks the repo and reads it without me in the room?"**

It covers cross-cutting architecture decisions added on top of the design (fixture replay mode, structured logging, X-Ray tracing, closure-factory for user-scoped tools), the repo artifacts that make production thinking visible (ADRs, threat model, evals report, CI workflow, `.env.example`, README pitch), and an updated 9-slice plan that bakes the production-readiness work into each feature slice plus a final polish slice.

**What it is not:**
- Not a rewrite of design-spec §1-§8.
- Not a deployment runbook (intentional — that's "full production-grade" depth, ruled out).
- Not a multi-tenant productization plan.

**Decisions inherited from the design spec but worth restating here:**
- Lambda env vars chosen for Duffel/LiteAPI keys (resolves design-spec §10 Q2 with a third option not originally listed; appropriate for personal scale, captured in ADR 0006).
- Sequential per-watch poll loop in v1 (design-spec §10 Q3; ADR 0003).
- Fail-fast on MCP errors, no retry inside poller (design-spec §10 Q5).
- No Bedrock cost cap in v1; AWS Budget alarm covers (design-spec §10 Q4).
- Single CDK stack, file separation by construct (design-spec §10 Q1).

---

## 2. Updated 9-slice plan

Approach A′ — production-readiness baked into each feature slice; one final slice 9 lands cross-cutting items.

| # | Slice | Feature work | Production-readiness additions |
|---|---|---|---|
| 1 | DDB tables (✅ committed `cf78209`) | `Watches` + `FareHistory` via DataStoresConstruct | On-demand billing, TTL on FareHistory, RemovalPolicy + future-GSI tradeoff inline comment, CfnOutputs |
| 2 | Watch CRUD tools | 7 `@tool` functions in `tools.py` + `watches.py` helpers + system-prompt update | Closure-factory pattern for user-scoped tools (ADR 0001); unit tests for `watches.py`; structured JSON logs via `aws-lambda-powertools`; ownership check on every `watchId`-keyed tool |
| 3 | `flights-mcp` Lambda | New Lambda dir, Duffel client, `search_offers` + `get_offer_details`, MCP server, API GW, custom JWT authorizer | Fixture replay mode (ADR 0002); Duffel-client unit tests using fixtures only; threat-model section for the Duffel boundary; X-Ray tracing on Lambda |
| 4 | `hotels-mcp` Lambda | Same shape as #3 but LiteAPI | Fixture replay mode; LiteAPI threat-model section; X-Ray |
| 5 | Poller Lambda | Reads active watches, calls both MCPs, writes FareHistory, gate logic (design-spec §5); Bedrock decision stubbed `{alert: True, reason: "stub"}` | Sequential loop (ADR 0003); per-watch structured logs with `watch_id`/`user_id` fields; X-Ray; CloudWatch metric emission (`watches_polled`, `watches_errored`, `alerts_sent`, `bedrock_decisions_made`) |
| 6 | Bedrock decision | Real Haiku 4.5 call returning `{alert, reason}` | Eval golden set v1 (decision-quality fixtures + judge rubric); eval runner script `evals/run_evals.py`; sample report committed; ADR 0004 (why route through Bedrock for the decision at all) |
| 7 | Notifier + SES | Templated email with reason; `lastAlertedAt`/`lastAlertedPrice` writeback after SES success | After-SES idempotency (ADR 0005); markdown-safe email template; CloudWatch alarm on Notifier errors |
| 8 | Cleanup | Delete the legacy stub MCP scaffold and its CDK construct | Migration commit comment; CloudWatch dashboard JSON in `infra/dashboards/` for the 4 metrics from slice 5 |
| 9 | Polish (new) | (no feature code) | CI workflow `.github/workflows/ci.yml` (lint, `cdk synth`, `cfn-lint`, unit tests in fixture mode); `.env.example`; README rewritten per §4.6; ADR index `docs/adr/README.md`; threat model `docs/threat-model.md`; AWS Budget alarm (CDK); architecture PNG export; Loom outline `docs/demo-script.md`; LICENSE; GitHub repo description + topics |

---

## 3. Cross-cutting architecture decisions

### 3.1 Fixture replay mode (slices 3 and 4)

The `flights-mcp` and `hotels-mcp` Lambdas pick a client implementation at cold start based on `MCP_MODE`:
- `MCP_MODE=live` (default) — real Duffel / LiteAPI calls.
- `MCP_MODE=fixture` — read pre-recorded JSON from `lambdas/{flights,hotels}-mcp/fixtures/`.

Layout (per MCP):
```
lambdas/flights-mcp/
  index.js
  client-live.js     # thin Duffel HTTP client
  client-fixture.js  # same interface, reads fixtures/*.json
  fixtures/
    SFO-NRT-2026-10-15.json
    LHR-CDG-2026-12-20.json
```

Both clients implement the same interface; the rest of the Lambda is mode-agnostic. Fixtures are recorded once via `tools/record-fixtures.py` (one-shot, run with real keys) and committed.

**Why this is the strongest single production-readiness signal:**
- Repo is forkable and end-to-end runnable for a reviewer with no Duffel/LiteAPI accounts.
- Unit tests run without network or external dependencies.
- Loom recording is reliable (no API outages mid-take).
- Forces a clean seam between "wraps an external API" and "delivers MCP tool semantics" — the same seam that makes the system mockable at all.

Documented as ADR 0002.

### 3.2 Structured JSON logs

All Python Lambdas use `aws-lambda-powertools`:
```python
from aws_lambda_powertools import Logger
logger = Logger(service="travel-agent")
```

Every log line is JSON: `timestamp`, `level`, `message`, `service`, `request_id`, `correlation_id`, plus event-specific fields (`watch_id`, `user_id` redacted to first-8 chars). Makes CloudWatch Logs Insights queries trivial — the README will include 2-3 sample queries.

JS Lambdas (the two authorizers) use `pino` for the same shape.

### 3.3 X-Ray tracing

Every Lambda gets `tracing: lambda.Tracing.ACTIVE`. Python Lambdas add `aws-xray-sdk-python` to the dependencies layer. The trace tells the cross-service story `web → API GW → travel-agent → MCPs → Duffel/LiteAPI` — exactly the screenshot to embed in the README.

Cost: pennies/month at personal scale; documented in the README cost section.

### 3.4 Closure-factory for user-scoped tools (slice 2)

The architectural call from earlier in this session, formalized.

`watches.py` exposes `make_watch_tools(user_id) -> list[Tool]` returning `@tool`-decorated closures bound to `user_id`. `agent.py` builds these per-request, *after* JWT verification, and passes them into `Agent(tools=[...])`. The LLM never sees `user_id` and can't be tricked into operating on a different user's watches via prompt injection.

Documented as ADR 0001. This is the keystone production-readiness decision because it shows the engineer thought about the LLM's authority surface, not just functionality.

---

## 4. Repo artifacts

These are the things a forking reviewer will see by browsing the repo.

### 4.1 ADR index — `docs/adr/`

Format: 1 page each, "Context / Decision / Consequences" (Michael Nygard style). Index lives at `docs/adr/README.md`.

| ID | Title | Slice |
|---|---|---|
| 0001 | User-scoped tools via closure factory | 2 |
| 0002 | Fixture replay mode for external-API MCP servers | 3 |
| 0003 | Sequential per-watch poll loop | 5 |
| 0004 | Bedrock decision call as alert-worthiness gate | 6 |
| 0005 | After-SES idempotency for `lastAlertedAt` writeback | 7 |
| 0006 | Lambda env vars (not Secrets Manager) for external API keys | written slice 9 |
| 0007 | Watches table without status GSI | written slice 9 (decision was made in slice 1; ADR backfilled) |

### 4.2 Threat model — `docs/threat-model.md`

Single page covering:
- Trust boundaries: web ↔ API GW; agent ↔ MCP servers; MCP servers ↔ Duffel/LiteAPI.
- JWT chain: Cognito (RS256) → agent verifies → agent signs internal HS256 → MCP authorizer verifies.
- Secrets handling: Lambda env var for v1, why not Secrets Manager (cost vs. personal scale; production would change), what changes for production.
- Failure modes that are security-relevant: LLM passing a wrong `userId` (mitigated by §3.4), expired/forged JWT, leaked Duffel key (provider-side rate limits + revocation), prompt-injection from MCP tool responses (mitigated by no tool result going back into a system prompt).
- Out-of-scope explicit list: DDoS, infra compromise, AWS account takeover.

### 4.3 Evals as repo artifacts — `evals/`

Per design-spec §6, three layers. Repo-visible additions:
- `evals/results/2026-05-XX-baseline.md` — sample run committed; markdown table per chat pattern with judge rationale snippets, plus pass/fail per decision-quality case.
- `evals/run_evals.py` — locally runnable: `make evals`. Reads judge rubrics from `evals/judge_prompts/`.
- CI integration: `workflow_dispatch`-only trigger (manual run). Uses GitHub Actions OIDC for AWS auth. Output posted as PR comment via `gh pr comment`. **Not** on every PR — cost discipline.

### 4.4 CI workflow — `.github/workflows/ci.yml`

Jobs (every push and PR):
- `lint` — `eslint` for JS; `ruff` for Python.
- `synth` — `npx cdk synth --quiet`. Catches CDK regressions.
- `cfn-lint` — `cfn-lint cdk.out/*.template.json`.
- `unit-tests` — `pytest` for Python Lambdas; `vitest`/`jest` for JS Lambdas. Runs in fixture mode (no AWS, no Duffel/LiteAPI; `MCP_MODE=fixture`).

Manual-only:
- `evals` (`workflow_dispatch`) — runs the eval suite; comments results on a triggered PR.

### 4.5 `.env.example`

```
# Required for live mode. Leave empty + set MCP_MODE=fixture to run without keys.
DUFFEL_API_KEY=
LITEAPI_API_KEY=

# SES verified email for trip alerts. Required from slice 7 onwards.
ALERT_TO_EMAIL=

# Internal HS256 secret used for agent → MCP-server JWTs.
# Any random 32+ char string; rotate independently of API keys.
JWT_SIGNATURE_SECRET=

# 'live' (default) or 'fixture'. Set to 'fixture' to run end-to-end without external API keys.
MCP_MODE=live
```

### 4.6 README structure (slice 9)

1. **30-second pitch** — one paragraph: what it does, why it's interesting, who built it.
2. **Architecture diagram** — Mermaid PNG (export of design-spec §2 diagram).
3. **Demo** — embedded 90-second Loom (chat-create a watch → live search → simulated alert email).
4. **Why this exists** — design-spec §1 distilled.
5. **Getting started** — 3 commands; fixture mode is the default so reviewer doesn't need keys.
6. **Production-readiness signals** — link to this companion spec; bullet summary of fixture mode, structured logs, X-Ray, evals, threat model, CI.
7. **What would change for actual production** — explicit hardening list: `RemovalPolicy.RETAIN`, secrets store, multi-tenancy guardrails, rate limiting on MCP endpoints, eval gate on PRs, blue-green deploy, status GSI on Watches, Bedrock cost cap.
8. **Cost** — $1-3/mo personal use (design-spec §7); link to AWS Budget alarm CDK construct.
9. **Links** — design spec, this spec, threat model, ADR index, evals report.

### 4.7 Demo script — `docs/demo-script.md`

Loom outline for slice 9:
- 0:00-0:15 — show the chat creating a watch (real flow, real DDB write).
- 0:15-0:35 — show "what's happening with my watches?" (`list_watches`).
- 0:35-1:00 — show live flight search via fixture mode; call out the `MCP_MODE` env var.
- 1:00-1:30 — show a sample alert email with model-generated reason; cut to CloudWatch dashboard showing the 4 metrics from slice 5.

---

## 5. Updated launch checklist

Extends design-spec §9. Completion targets each item to the slice that closes it.

Note: this checklist is a dated planning artifact. Several originally
slice-9-scoped items below are already complete on `main`.

- [x] (slice 1) DDB tables provisioned with on-demand billing, TTL on FareHistory
- [x] (slice 2) Closure-factory pattern + ADR 0001 + unit tests for `watches.py`
- [x] (slice 3) `flights-mcp` Lambda + fixture mode + ADR 0002 + threat-model Duffel section
- [x] (slice 4) `hotels-mcp` Lambda + fixture mode + threat-model LiteAPI section
- [x] (slice 5) Poller Lambda + EventBridge + structured logs + X-Ray on all Lambdas + CloudWatch metrics + ADR 0003
- [x] (slice 6) Bedrock decision + eval baseline report committed (`evals/results/`) + ADR 0004
- [x] (slice 7) Notifier + SES + ADR 0005 + markdown-safe email template
- [x] (slice 8) legacy stub MCP scaffold removed + CloudWatch dashboard committed
- [ ] (slice 9, remaining) CI green + Loom recorded
- [ ] (post-launch, design-spec §9) One real watch active for 7 days before declaring done

---

## 6. Out of scope for this companion (deferred — likely never)

- Dev/staging stack separation
- Blue-green Lambda alias deploys
- Integration tests against an ephemeral env in CI
- SLO/error-budget tracking
- Grafana / Datadog dashboards (CloudWatch is enough)
- OIDC roles for human deployers (only CI uses OIDC)

These were explicitly ruled out by choosing "selective hardening" depth. If audience or scope ever changes, revisit before adding them.
