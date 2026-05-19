# Trip Tracker — Design Considerations by Component

Why each component is built the way it is: the constraints, the
alternatives rejected, the tradeoffs accepted. Companion to
[`architecture.md`](./architecture.md) (the authoritative deployed
architecture) and its icon view [`architecture-v2.drawio`](./architecture-v2.drawio),
[`SYSTEM.md`](./SYSTEM.md) (flows + sequence diagrams),
[`adr/README.md`](./adr/README.md) (the binding decisions), and
[`threat-model.md`](./threat-model.md). Where a decision is
recorded in an ADR, that ADR is authoritative — this doc summarizes and
connects.

---

## Cross-cutting principles

These shape every component; read them once.

- **Cost-safe by default.** `mcpMode` defaults to fixture; the test
  suite always runs fixture/stub; Bedrock and SES have stub modes; a
  $10/mo AWS Budget alarm is the hard backstop. You can deploy and
  exercise the whole system for $0. ([ADR 0002](./adr/0002-fixture-replay-mode.md), [ADR 0004](./adr/0004-bedrock-decision.md))
- **Identity is never inferred from the model.** The user is a verified
  JWT claim end to end. The LLM tool schema never carries `user_id`, so
  prompt injection cannot retarget another user. ([ADR 0001](./adr/0001-user-scoped-tools-via-closure-factory.md))
- **Per-component credential isolation.** The chat agent and the poller
  sign MCP calls with *separate* Secrets Manager secrets; a leaked
  secret cannot impersonate the other component. ([ADR 0006](./adr/0006-per-component-jwt-secrets.md))
- **Least-privilege IAM, never wildcards.** Bedrock grants are scoped to
  specific model/inference-profile ARNs; SES to the sender identity;
  DynamoDB to the table (+ its index). No `bedrock:*`, no `Resource:'*'`.
- **Fail-soft, idempotent, sequential.** One watch at a time; a per-watch
  error is logged and skipped, not fatal; alert delivery is at-least-once
  with a bounded duplicate window. ([ADR 0003](./adr/0003-sequential-poll-loop.md), [ADR 0005](./adr/0005-after-ses-idempotency.md))
- **Honest personal-scale ceilings.** Where a design is "good enough for
  one user" rather than production-correct (the status GSI partition,
  single recipient), the ADR says so explicitly rather than implying
  more.
- **DynamoDB numerics are `Decimal(str(x))`** everywhere — never
  `Decimal(float)` — so `1500.0` stays `1500`. Consistent across agent,
  poller, notifier.
- **Durable artefacts carry no roadmap jargon.** Comments, commits,
  ADRs, docs avoid scaffold labels; a ripgrep gate enforces it. Risky
  changes pass a sequential multi-model reviewer gate (code → security →
  test → comments).

---

## 1. Cognito (`lib/cognito.js`)

**Purpose.** User identity for the chat path; issues the user JWT and
the JWKS the agent authorizer validates against.

**Design considerations.**
- `selfSignUpEnabled: false` — single-user/demo system; users (`Alice`,
  `Bob`) are provisioned by `prep-web.sh`, not open registration.
- Authorization-code grant with a client secret; 8-hour token validity
  — long enough for a chat session, short enough to bound a leaked token.
- `RemovalPolicy.DESTROY` — personal/dev posture; a real deployment
  holding user data would switch to `RETAIN`.
- Domain prefix is suffixed with a stack-derived id so two deploys in
  one account don't collide on the global Cognito domain namespace.

**Tradeoffs.** No MFA, no hosted-UI theming, no user migration — all
out of scope for a single intended user; the scaffold supports more if
the project ever needs it.

---

## 2. Agent authorizer (`lambdas/agent-authorizer`)

**Purpose.** API Gateway request authorizer that validates the Cognito
user JWT before the Travel Agent runs.

**Design considerations.**
- Validates against the Cognito **JWKS** URL (public, no secret needed
  in the Lambda) — asymmetric verification, nothing to leak here.
- It is the *only* place the Cognito token is trusted; downstream code
  receives an already-verified `user_id` and never re-parses raw tokens.
- Kept deliberately thin: authorize or deny, no business logic, so the
  trust boundary is small and auditable.

---

## 3. Travel Agent (`lib/agent.js` + `lambdas/travel-agent`)

**Purpose.** The Strands chat agent: turns natural language into watch
operations and answers live-price / status questions.

**Design considerations.**
- **User-scoped tools via a closure factory ([ADR 0001](./adr/0001-user-scoped-tools-via-closure-factory.md)).**
  `make_watch_tools(user_id)` builds the seven CRUD tools *after* JWT
  verification, each closing over the verified `user_id`. The LLM's tool
  schema never exposes `user_id`, so the model cannot be tricked into
  acting on another user's watches. Ownership is also enforced at the
  data layer (a foreign `watchId` returns no row) — defence in depth,
  no special-case code.
- **Two tool sources, one surface.** Local `@tool` watch-CRUD functions
  plus MCP tools discovered from the flights/hotels servers, merged into
  one tool list. Per-endpoint failures are caught so one MCP server
  being down degrades gracefully rather than failing the chat.
- **The agent calls MCP under its OWN per-component JWT ([ADR 0006](./adr/0006-per-component-jwt-secrets.md)),**
  signed with the agent secret fetched lazily from Secrets Manager. This
  is why the architecture shows a separate agent→MCP trust edge distinct
  from the poller's.
- **Bedrock IAM is ARN-scoped.** The grant is built from the same
  context value that selects the invoked model, so the grant ARNs and
  the runtime model cannot drift ([ADR 0006](./adr/0006-per-component-jwt-secrets.md)). Never `bedrock:*`, never
  `Resource:'*'`.
- **S3 session store.** Strands `S3SessionManager` externalizes
  multi-turn state so the Lambda stays stateless; the bucket is
  `grantReadWrite`-scoped to the agent only.
- **Model choice is explicit and overridable.** Currently Claude 3.5
  Haiku (scaffold default) via `AGENT_BEDROCK_MODEL_ID`; the design spec
  calls for a stronger chat model and that upgrade is tracked
  separately. Note this is a *different* model from the poller's
  decision model (§8) — they are chosen independently.
- **System prompt enforces discipline:** ask for missing fields one at a
  time, echo the full structured watch in plain English before any
  write, headline summaries on status, never invent a price not returned
  by a tool.

**Tradeoffs.** The chat model is the weaker scaffold default until the
upgrade lands; watch creation leans on prompt discipline rather than a
form (intentional — the differentiator is "no form").

---

## 4. MCP servers — flights / hotels (`lib/*-mcp-server.js` + `lambdas/flights-mcp`, `lambdas/hotels-mcp`)

**Purpose.** One MCP server per external integration: `flights-mcp`
wraps Duffel, `hotels-mcp` wraps LiteAPI.

**Design considerations.**
- **One server per domain.** Independent deploy, blast radius, and
  rate-limit surface per upstream API. New tools on either server are
  discovered by the agent's endpoint loop without agent code changes.
- **Fixture replay ([ADR 0002](./adr/0002-fixture-replay-mode.md)).** `MCP_MODE` selects `client-live.js`
  vs `client-fixture.js` at cold start; both implement the same
  interface. Fixture mode needs no API key, makes no paid call, and is
  deterministic — it is the default and what tests use. One flag flips
  the whole external-API surface intentionally.
- **Each server has its own authorizer Lambda** in front (per-component
  JWT, §5) — the trust boundary is enforced at the edge, and a direct
  call to the server still needs a validly signed token.
- Built as a Lambda from a dependencies layer; ARM64 by default for cost.

**Tradeoffs.** Two servers is more infra than one combined server, but
the domain isolation and independent fixture sets are worth it; fixture
data can drift from the live API shape — the fixture/live interface
parity is the guard.

---

## 5. MCP authorizer (`lambdas/mcp-authorizer`)

**Purpose.** Validates the per-component JWT (agent's or poller's) on
every MCP request.

**Design considerations.**
- **Per-component verification ([ADR 0006](./adr/0006-per-component-jwt-secrets.md)).** HS256, algorithm pinned
  (no `alg` confusion), `exp` enforced at the verifier (not minter
  trust), the two-secret/sub coupling checked so an agent-signed token
  cannot claim `sub: poller` or vice versa.
- **The verifier is triplicated** across mcp-authorizer + flights-mcp +
  hotels-mcp index by deliberate decision: each enforces the invariant
  independently so a direct-to-handler call is still checked. Drift risk
  is bounded by per-package tests pinning the identical invariant — edit
  all copies together.
- Infra-failure (e.g. Secrets fetch fails) **fails closed** with a
  distinct alarm rather than failing open.

---

## 6. Secrets Manager (`lib/secrets.js`)

**Purpose.** Holds the two HS256 signing secrets — one for the agent,
one for the poller.

**Design considerations ([ADR 0006](./adr/0006-per-component-jwt-secrets.md)).**
- **No signing material in the repo.** Repo access yields zero
  token-forging capability — the prior hard-coded shared literal is
  closed.
- **Least-privilege reads.** Each minter reads only its own secret; the
  verifiers read both; no Secrets Manager grant is wildcarded.
- **Lazy, cached fetch** (`boto3 _secrets=None` lazy-create) so unit
  tests don't hit AWS and cold-start cost is one `GetSecretValue`.
- **Rotation is a manual console + redeploy step** until a rotation
  Lambda is justified — explicitly accepted scope boundary.

**Tradeoffs.** Two secrets cost ~$0.40/mo each and add a cold-start
fetch; accepted for the isolation gained.

---

## 7. Data stores (`lib/data-stores.js`)

**Purpose.** `Watches` (one row per tracked trip) and `FareHistory`
(price snapshots over time).

**Design considerations.**
- **`Watches` keyed PK `userId` / SK `watchId`** — ideal for per-user
  chat CRUD. The poller needs all active watches across users, so a
  **`status-index` GSI** (PK `status`, Projection ALL) lets it `Query`
  instead of `Scan` — cost O(active), not O(total) ([ADR 0007](./adr/0007-watches-status-gsi.md)).
- **Sparse-index invariant is load-bearing.** A row without `status` is
  invisible to the poller and fails *silently*. Every writer sets
  `status`; `update_watch` is `SET`-only so it can never drop it. This
  is documented as an invariant, not guarded with untestable code.
- **`FareHistory` 90-day TTL** keeps the table self-pruning and storage
  trivial; 90 days comfortably covers the 30-day-median anomaly window.
- **PAY_PER_REQUEST billing** — no capacity planning at personal scale;
  cheapest below sustained ~10 RCU/WCU.
- **`RemovalPolicy.DESTROY`** matches the personal/dev posture; flip to
  `RETAIN` before holding real user data.
- **Honest ceiling.** A `status`-only GSI partition key is low
  cardinality (one hot partition at scale). It is *less wrong* than
  Scan-all, not the production answer (a sharded/composite PK is) — the
  ADR scopes the decision to personal scale and says so.

---

## 8. Poller (`lib/poller-server.js` + `lambdas/poller`)

**Purpose.** The scheduled engine: enumerate active watches, fetch
prices, snapshot, gate, decide, alert.

**Design considerations.**
- **Sequential per-watch loop ([ADR 0003](./adr/0003-sequential-poll-loop.md)).** Plain
  `for watch in iter_active_watches()`, one at a time;
  `reservedConcurrentExecutions = 1` so an EventBridge tick cannot fan
  out concurrent pollers. Per-watch `McpCallError`/`ValueError`/`KeyError`
  is caught, logged `watch_errored`, loop continues. Simplicity and
  predictable cost over throughput — correct at personal scale.
- **GSI Query enumeration ([ADR 0007](./adr/0007-watches-status-gsi.md)).** Reads only active rows,
  full row in one read (Projection ALL); pagination preserved; no
  `ConsistentRead` (unsupported on a GSI; one-tick latency for a
  brand-new watch is accepted).
- **Two gates before the model.** A threshold gate and a 30-day anomaly
  gate run first; Bedrock is only consulted when a gate passes — the
  model spend is bounded and only spent where it adds judgment.
- **Bedrock decision ([ADR 0004](./adr/0004-bedrock-decision.md)).** Claude Haiku 4.5, pinned to
  `claude-haiku-4-5-20251001` so an upstream point release can't change
  behaviour silently. Returns `{alert, reason}`; the **reason string is
  the product** — templated verbatim into the email, the value a static
  "fare dropped 30%" cannot match. `BEDROCK_MODE=stub` keeps tests
  cost-free. (Distinct from the chat model in §3.)
- **Alert delivery is decoupled.** On `alert==true` the poller
  async-invokes the notifier (`InvocationType=Event`); Lambda's async
  runtime owns retry, no SNS/EventBridge added until fan-out matters.
- **IAM** scoped to the poller secret, the Watches table + index, the
  FareHistory table, the specific Bedrock model ARNs, and
  `lambda:InvokeFunction` on the notifier only.

---

## 9. Notifier (`lib/notifier-server.js` + `lambdas/notifier`)

**Purpose.** Send the alert email and record that it was sent.

**Design considerations ([ADR 0005](./adr/0005-after-ses-idempotency.md)).**
- **After-SES conditional writeback.** `ses.send_email` first, then a
  conditional `lastAlertedAt` write. SES failure raises → no writeback →
  next poll retries (at-least-once). DDB failure after a successful send
  → WARN + 200; the next poll's 5% price-proximity dedup band catches
  the duplicate. The conditional protects against out-of-order retries
  backdating dedup state.
- **Plain text only.** `reason` is interpolated verbatim with no HTML
  body — plain text *is* the escape, defence-in-depth atop upstream
  parser hardening and the subject CR/LF strip. No HTML-injection path
  by construction.
- **Single verified sender + single recipient, both synth-validated.**
  `ses:SendEmail` is resource-scoped to the sender *identity* ARN
  (email, not domain — a domain grant would allow sending as any address
  in it). Multi-user recipient lookup via Cognito is the documented
  upgrade path.
- **DynamoDB grant is `UpdateItem` on Watches only** — no put, no
  delete, no scan, no FareHistory.

**Tradeoffs / deferred.** DLQ on the async path, SNS bounce/complaint
handling, and an HTML template are explicit post-v1 deferrals — a full
SES outage after retry exhaustion loses an alert, acceptable for v1.

---

## 10. Observability dashboard (`lib/observability-dashboard.js`)

**Purpose.** One CloudWatch dashboard over 8 Lambdas + 3 API Gateways +
the poller's EMF counters.

**Design considerations.**
- **Metric-shape asserted, not eyeballed.** Tests pin each widget's
  metric dimensions (a label carrying a function name while the
  dimension is wrong would otherwise pass) and assert two synth passes
  are byte-identical (cdk-diff-clean).
- **Cross-language constant sync.** The poller emits EMF metric names
  from Python; a sync test pins the JS dashboard constants against the
  Python source so the two cannot drift.
- Instantiated last in the stack so every Lambda/API ref exists.

---

## 11. Budget alarm (`lib/budget-alarm.js`)

**Purpose.** Account-level $10/mo cost budget — cheap insurance against
a runaway poll/Bedrock loop.

**Design considerations.**
- **80% ACTUAL + 100% FORECASTED** notifications: an early "you've spent
  $8" warning *and* a trajectory warning that fires before month-end,
  better than a single 100%-ACTUAL alert that only fires after the money
  is gone.
- **Email subscriber only — no SNS, no Budgets action.** Design intent
  is "email notification"; auto-remediation on a personal stack risks
  locking the owner out. The alarm is a signal; the operator decides.
- **No mode flag.** Unlike SES/Bedrock, a Budget is free and harmless to
  deploy, so there is no live/stub distinction to gate.
- **`EMAIL_PATTERN` copied verbatim from the notifier, not shared.** A
  shared util is a larger refactor than this checklist item; the regex
  is stable and this construct's own tests pin the same accept/reject
  shapes, so a divergence fails here.
- Fixed `budgetName` so the budget is findable and idempotent across
  redeploys.

---

## Where to go next

- The binding decisions: [`adr/README.md`](./adr/README.md) (ADR 0001–0007).
- End-to-end flows and sequence diagrams: [`SYSTEM.md`](./SYSTEM.md).
- Trust boundaries and attacker view: [`threat-model.md`](./threat-model.md).
- The original problem framing and scope: the design spec under
  [`superpowers/specs/`](./superpowers/specs/).
