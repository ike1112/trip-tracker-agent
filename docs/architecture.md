# Trip Tracker — Complete Architecture

Authoritative architecture of the **deployed** system, verified against a
live deploy (account resources, request traces, source). Supersedes the
scaffold sketch in [`../img/arch.png`](../img/arch.png), which describes the
upstream AcmeCorp bookings sample, not this product.

Companion docs: user flows + sequence diagrams in [`SYSTEM.md`](./SYSTEM.md);
decisions in [`adr/`](./adr/README.md); trust analysis in
[`threat-model.md`](./threat-model.md).

> **What changed vs `img/arch.png` (the initial scaffold sketch):**
> | Scaffold sketch | This system |
> |---|---|
> | HCP Vault issues JWKS | **Amazon Cognito** (user pool, hosted UI, JWKS) |
> | Deployed with Terraform | **AWS CDK** (CloudFormation) |
> | One "Bookings MCP" | **Two domain MCP servers**: flights (Duffel), hotels (LiteAPI) |
> | One shared secret | **Two per-component Secrets Manager secrets** (agent / poller), ADR 0006 |
> | Booking tools (book-car/hotel) | **Watch-CRUD + price-search tools** (search + alert only, no booking) |
> | Chat path only | **+ scheduled poll → gate → Bedrock decision → SES alert** path |
> | No data/observability/cost layer | **DynamoDB, S3, CloudWatch, X-Ray, EventBridge, SES, Budgets, SNS** |

---

## 1. Full AWS service inventory

| Service | Role in this system |
|---|---|
| **Amazon Cognito** | User identity. User pool + app client (with secret) + hosted login UI + JWKS. Issues the RS256 user JWT. Demo users `Alice`/`Bob`. |
| **Amazon API Gateway** | **Three** REST APIs, each with a `/prod` stage and a Lambda authorizer: Agent API, Flights-MCP API, Hotels-MCP API. |
| **AWS Lambda** | 8 functional Lambdas (see §2) + CDK helper functions (log-retention, S3 auto-delete, bucket-notifications) which are infra plumbing only. |
| **Amazon Bedrock** | LLM inference. Chat agent → Claude Sonnet 4.5 (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`; default 3.5 Haiku). Poller decision → Claude Haiku 4.5 (stubbable). |
| **Amazon DynamoDB** | `Watches` (PK `userId`, SK `watchId`; `status-index` GSI, ADR 0007) and `FareHistory` (PK `watchId`, SK `timestamp`; 90-day TTL). On-demand billing. |
| **Amazon S3** | Strands session store (multi-turn chat state) + CDK asset bucket. |
| **AWS Secrets Manager** | Two HS256 signing secrets, ADR 0006: `trip-tracker-agent-jwt-signer`, `trip-tracker-poller-jwt-signer`. Per-component isolation. |
| **Amazon EventBridge** | Scheduled rule `rate(4 hours)` → invokes the poller. |
| **Amazon SES** | Sends the trip-alert email (sandbox: verified sender + recipient). Stubbable. |
| **AWS Budgets** | Account-level $10/month COST budget; 80% actual + 100% forecast notifications. |
| **Amazon SNS** | Delivery channel for the Budgets notification (email subscription). |
| **Amazon CloudWatch** | Structured JSON logs (Lambda Powertools / pino), poller EMF metrics, 1 dashboard (`trip-tracker-StrandsAgentOnLambdaStack`), notifier-error alarm. |
| **AWS X-Ray** | `tracing: ACTIVE` on every Lambda — cross-service trace `web → API GW → agent → MCP`. |
| **AWS IAM** | Per-component roles, least privilege. Agent Bedrock grant scoped to the 3 US-region foundation-model ARNs + the inference-profile ARN (ADR 0006). |
| **AWS CloudFormation / CDK** | Infrastructure as code. One stack, file separation by construct. |
| **External (non-AWS)** | Duffel API (flights), LiteAPI (hotels) — called by the MCP servers in `live` mode; fixture-replayable (ADR 0002). |
| **Web UI (not AWS-hosted)** | Local FastAPI + Gradio app; Authlib OAuth2 code flow against Cognito. Runs on the operator's machine. |

---

## 2. The 8 functional Lambdas and their tools/role

```
travel-agent-on-lambda           Strands agent. Bedrock reasoning + tool loop.
  local tools (in-process):        add_watch, list_watches, update_watch,
                                   pause_watch, resume_watch, remove_watch,
                                   get_fare_history, get_user_location,
                                   get_todays_date
  remote tools (via MCP):          flights-mcp + hotels-mcp tool lists, merged
  binds:                           Bedrock, DynamoDB (Watches/FareHistory),
                                   S3 (session), Secrets Manager (agent secret)

travel-agent-authorizer          API GW authorizer. Validates the Cognito
                                   user JWT against Cognito JWKS (RS256).

flights-mcp-server               MCP server. Tools: search_offers,
                                   get_offer_details. client-live=Duffel,
                                   client-fixture=recorded JSON (MCP_MODE).
flights-mcp-server-authorizer    Validates the per-component JWT (HS256).

hotels-mcp-server                MCP server. Tools: search_offers,
                                   get_hotel_details. client-live=LiteAPI,
                                   client-fixture=recorded JSON.
hotels-mcp-server-authorizer     Validates the per-component JWT (HS256).

trip-tracker-poller              Scheduled pipeline (NOT tool-shaped):
                                   enumerator → mcp_client → snapshot →
                                   history_window → gates (dedup/threshold/
                                   anomaly) → bedrock_decide → writer →
                                   metrics. Mints its own per-component JWT.

trip-tracker-notifier            Sends the SES email, then idempotent
                                   writeback of lastAlertedAt/Price (ADR 0005).
```

---

## 3. Deployment / infrastructure view (all services)

```
                                   AWS Cloud (us-east-1) — one CDK stack
┌──────────────────────────────────────────────────────────────────────────────────────┐
│                                                                                        │
│  IDENTITY            CHAT PLANE                              DATA PLANE                 │
│  ┌─────────┐         ┌──────────────────────────────┐       ┌──────────────────────┐   │
│  │ Cognito │  JWKS   │ API GW (Agent) ─▶ agent-      │       │ DynamoDB Watches     │   │
│  │ userpool│────────▶│   authorizer λ (RS256)        │       │  PK userId/SK watchId│   │
│  │ +client │         │        │ allow                │       │  + status-index GSI  │   │
│  │ hostedUI│         │        ▼                       │◀─────▶│ DynamoDB FareHistory │   │
│  └─────────┘         │  travel-agent λ (Strands)     │ CRUD  │  PK watchId/SK ts    │   │
│       ▲              │   ├─ Bedrock (Sonnet 4.5)     │       │  90-day TTL          │   │
│       │ OAuth2       │   ├─ local watch-CRUD tools ──┼──────▶└──────────────────────┘   │
│       │ code         │   ├─ S3 session store ────────┼────▶ S3 (Strands sessions)       │
│       │              │   └─ MCP client (mints JWT) ──┼──┐  Secrets Mgr: agent-jwt-signer│
│  ┌─────────┐         └──────────────────────────────┘  │                                │
│  │ Web UI  │  user JWT                                  │ per-component JWT (HS256)      │
│  │ FastAPI │──(Bearer)──────────────────────────────────┤                                │
│  │ +Gradio │                                            ▼                                │
│  │ (local) │         TOOL PLANE  ┌───────────────────────────────────────────────┐      │
│  └─────────┘                     │ API GW (Flights-MCP) ─▶ flights-mcp-authorizer │      │
│       ▲                          │      │ allow              (HS256, 2-secret)     │      │
│   Alice/Bob                      │      ▼                                           │      │
│                                  │  flights-mcp-server λ ──▶ Duffel API | fixtures │      │
│                                  │ API GW (Hotels-MCP)  ─▶ hotels-mcp-authorizer  │      │
│                                  │      ▼                                           │      │
│                                  │  hotels-mcp-server λ  ──▶ LiteAPI    | fixtures │      │
│                                  └───────────────────────────────────────────────┘      │
│                                                                                        │
│  SCHEDULED PLANE                                         NOTIFY PLANE                   │
│  ┌────────────┐   ┌───────────────────────────────┐     ┌───────────────────────────┐  │
│  │ EventBridge│──▶│ trip-tracker-poller λ          │     │ trip-tracker-notifier λ   │  │
│  │ rate(4h)   │   │  Query status-index (active)   │     │  SES send → lastAlerted   │  │
│  └────────────┘   │  Secrets Mgr: poller-jwt-signer│     │  writeback (idempotent)   │  │
│                   │  → MCP tool plane (price)      │     └─────────────┬─────────────┘  │
│                   │  → FareHistory write           │ async invoke      ▼                │
│                   │  → gates → Bedrock (Haiku)─────▶│─────────────▶  Amazon SES ──▶ 📧   │
│                   └───────────────────────────────┘                                    │
│                                                                                        │
│  CROSS-CUTTING:  CloudWatch (logs+EMF metrics+1 dashboard+alarm) · X-Ray (all λ) ·      │
│                  AWS Budgets $10/mo ─▶ SNS ─▶ 📧 · IAM least-privilege · CDK IaC        │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Flow A — chat path (interactive)

```
Alice ─▶ Web UI (Gradio, local) ─▶ Cognito hosted login (OAuth2 authcode)
      ◀── user JWT (RS256, in server-side session) ───────────────────────┐
Web UI ──POST /chat {text} + Bearer user JWT──▶ API GW (Agent)            │
   API GW ──▶ travel-agent-authorizer λ ──validate vs Cognito JWKS──▶ allow│
   API GW ──▶ travel-agent λ:                                              │
        ├─ load S3 session (history)                                       │
        ├─ Bedrock Sonnet 4.5: reason over prompt + tool catalog           │
        ├─ LOCAL tool? add_watch/list/update/... ─▶ DynamoDB Watches/Fare  │
        │     (user_id bound via closure factory, ADR 0001 — LLM           │
        │      never supplies it; numbers Decimal-coerced for DDB)         │
        ├─ MCP tool? mint per-component JWT with AGENT secret (HS256,       │
        │     sub=travel-agent, 5-min exp) ─▶ API GW (Flights/Hotels MCP)  │
        │     ─▶ mcp-authorizer λ (2-secret verify) ─▶ mcp-server λ         │
        │     ─▶ Duffel/LiteAPI (live) | recorded fixtures (fixture)        │
        ├─ Bedrock Sonnet 4.5: synthesize natural-language answer          │
        └─ persist S3 session ─▶ response ─────────────────────────────────┘
```

Shadow paths: nil/expired user JWT → authorizer denies (401, "re-login");
MCP miss/empty → agent reports "no data", never fabricates a price
(design-spec rule); MCP server error → agent degrades that tool surface,
turn continues.

---

## 5. Flow B — scheduled path (autonomous, no user)

```
EventBridge rate(4h) ─▶ trip-tracker-poller λ
  ├─ DynamoDB: Query status-index for status="active"  (ADR 0007, not Scan)
  ├─ Secrets Manager: fetch POLLER signing secret  (distinct from agent's)
  ├─ per active watch (sequential, ADR 0003):
  │    ├─ mint per-component JWT (sub=travel-agent, poller secret)
  │    ├─ flights-mcp + hotels-mcp search ─▶ combined total
  │    ├─ write FareHistory snapshot (Decimal-coerced; 90-day TTL)
  │    ├─ pull 30-day window BEFORE the new row (no self-poisoning)
  │    ├─ gates: dedup(≥5% < lastAlertedPrice) → threshold(< maxTotal)
  │    │         OR anomaly(≤85% median OR new 30-day low)
  │    └─ if a gate passes ─▶ Bedrock Haiku 4.5 {alert, reason}
  │                            (stub returns {alert:true,"stub"} in dry run)
  │         └─ if alert ─▶ async invoke trip-tracker-notifier λ
  │                          ├─ SES send (reason templated in)
  │                          └─ AFTER send: writeback lastAlertedAt/Price
  │                             (idempotent, ADR 0005)
  └─ flush EMF metrics: watches_polled, watches_errored, alerts_sent,
                        bedrock_decisions_made  ─▶ CloudWatch
```

---

## 6. Trust & identity boundaries

```
[1] Browser ↔ Web UI          server-side session holds the user JWT
[2] Web UI  ↔ API GW (Agent)  Cognito user JWT (RS256), verified vs JWKS
[3] Agent   ↔ MCP servers     per-component JWT (HS256) signed with the
                              AGENT secret; sub=travel-agent; 5-min exp
[4] Poller  ↔ MCP servers     per-component JWT (HS256) signed with the
                              POLLER secret — a different Secrets Manager
                              secret. A leaked agent secret cannot mint
                              poller-valid tokens or vice versa; the MCP
                              authorizers reject the wrong signer.
[5] LLM authority             user identity is NEVER read from a model
                              response — it rides a JWT claim end to end;
                              watch-CRUD tools are closures bound to the
                              verified user_id (ADR 0001), so prompt
                              injection cannot retarget another user.
[6] MCP ↔ Duffel/LiteAPI      provider API keys (live mode only); fixture
                              mode needs no keys (forkable, ADR 0002).
```

---

## 7. Cross-cutting / operational

- **Observability:** every Lambda emits structured JSON logs with an
  `xray_trace_id`; the poller emits 4 EMF metrics; one CloudWatch dashboard
  covers the 8 Lambdas + 3 APIs; a notifier-error alarm fires on send
  failures; X-Ray gives the cross-service waterfall.
- **Cost control:** AWS Budgets $10/mo → SNS → email at 80%/100%. Idle
  fixed cost is the two Secrets Manager secrets (~$0.80/mo); everything
  else is free-tier / on-demand near-zero at rest.
- **Modes (ADR 0002):** `mcpMode` fixture (default, no provider keys) vs
  live; `bedrockMode`/`sesMode` stub vs live. The chat agent's Bedrock is
  always live (no stub) and needs model access in-region.
- **IaC:** AWS CDK, one stack, construct-per-file. Not Terraform.
