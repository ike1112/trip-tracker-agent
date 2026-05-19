# AWS Architecture & Patterns

Source tools:
- **AWS Knowledge MCP server** (`aws___search_documentation`, `aws___read_documentation`)
- **AWS Pricing MCP server** (`get_bedrock_patterns`)
- Manual reading of `lib/`, `lambdas/`, `web/`

This file maps each architectural choice in the project to the official AWS pattern that justifies (or contradicts) it. Use it as a reading guide: each section gives you the AWS doc URL plus what's specific to *this* code.

---

## 1. Architecture overview

```
┌────────────────────┐         OIDC / Authorization Code         ┌──────────────────┐
│  Browser (Alice)   │ ──────────────────────────────────────────▶│   Cognito Hosted │
└────────────────────┘                                             │  UI + Token EP   │
         │                                                         └──────────────────┘
         │ Bearer access_token
         ▼
┌────────────────────┐    Lambda Authorizer (RS256/JWKS)        ┌──────────────────┐
│  Web App (Gradio   │ ──▶  travel-agent-api  ──▶  travel-agent ─▶│  Bedrock         │
│   FastAPI on PC)   │       (REST API GW)        Lambda(py)     │  Claude 3.5 Haiku│
└────────────────────┘                              │             └──────────────────┘
                                                    │  S3SessionManager  ┌──────────┐
                                                    ├───────────────────▶│   S3     │
                                                    │                    └──────────┘
                                                    │  mints HS256 JWT
                                                    ▼
                          ┌──────────────────┐  Bearer agent JWT  ┌──────────────────┐
                          │   mcp-authorizer │ ─────────────────▶ │ flights/hotels   │
                          │      Lambda      │                    │ MCP Lambdas       │
                          └──────────────────┘                    └──────────────────┘
                                                                       (direct handlers)
```

This is a textbook **serverless multi-tier AI agent**: identity at the edge, stateless app layer, externalized session state, model layer via managed inference, and tool-call boundary via MCP. Every block is pay-per-use.

---

## 2. Pattern: OIDC Authorization Code grant with Cognito

**What this project does:**
- `lib/cognito.js:21-30` configures the User Pool Client with `authorizationCodeGrant: true`, scopes `openid email profile`.
- `web/oauth.py` uses Authlib's `authorize_redirect` / `authorize_access_token` against Cognito's hosted-UI endpoints.

**AWS guidance:**
- [Cognito Hosted UI](https://docs.aws.amazon.com/help-panel/cognito/latest/console/hp-hosted-ui.html) — managed login pages, OAuth 2.0 endpoints, customizable callbacks.
- [callbackUrls reference](https://docs.aws.amazon.com/sdk-for-kotlin/api/latest/cognitoidentityprovider/aws.sdk.kotlin.services.cognitoidentityprovider.model/-user-pool-client-type/callback-urls.html) — *"Amazon Cognito requires HTTPS over HTTP except for localhost addresses (used for testing)."*

**Verdict:** correct pattern. Caveats are in `analysis/aws-security.md` (`ALLOW_USER_PASSWORD_AUTH` shouldn't be enabled, and the `localhost` HTTP callback must become HTTPS for production).

---

## 3. Pattern: API Gateway Lambda Token Authorizer with JWKS

**What this project does:**
- `lib/agent.js` creates the agent API authorizer (Cognito RS256/JWKS).
- `lib/flights-mcp-server.js` and `lib/hotels-mcp-server.js` create the MCP API token authorizers.
- `lambdas/agent-authorizer/index.js` verifies a Cognito-issued RS256 JWT against the JWKS endpoint (`COGNITO_JWKS_URL`).
- `lambdas/mcp-authorizer/index.js` verifies an HS256 JWT signed with a shared secret.

**AWS guidance** (sources: AWS docs MCP search results):
- [`RequestAuthorizerProps`](https://docs.aws.amazon.com/cdk/api/v1/python/aws_cdk.aws_apigateway/RequestAuthorizerProps.html) — *"a cache TTL (defaulting to 5 minutes, max 1 hour)"*
- [`LambdaTokenAuthorizationIdentity`](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/sam-property-api-lambdatokenauthorizationidentity.html) — *"`ReauthorizeEvery` (TTL in seconds for caching authorizer results, defaulting to 300)."*
- The synthesized template confirms this: both authorizers have `AuthorizerResultTtlInSeconds: 300`.

**Verdict:** Two-tier token validation (RS256 from external IdP, HS256 for service-to-service) is a clean pattern. The 5-minute cache is the AWS default; if you tighten it, you trade revocation latency for slightly higher Lambda authorizer cost (which is rounding error here).

**Key insight (token exchange):** the agent does NOT forward the user's Cognito JWT to the MCP server. Instead it mints a new HS256 JWT with `sub=travel-agent` and the user identity as side claims (`user_id`, `user_name`). The MCP authorizer **only allows `sub=travel-agent`**. This is the *RFC 8693 Token Exchange* pattern simplified for an internal trust domain — it means the LLM never sees a token it could leak, and an attacker who steals the JWT can only impersonate "the agent talking to MCP", not a user.

See `analysis/identity-flow.md` for the full sequence.

---

## 4. Pattern: MCP Servers on AWS Lambda (direct handler)

**What this project does:**
- `lambdas/flights-mcp/index.js` and `lambdas/hotels-mcp/index.js` are direct Lambda handlers (no Express, no Lambda Web Adapter).
- Each handler parses JSON-RPC requests and dispatches via an in-memory transport (`lambda-transport.js`) to the MCP server implementation.
- API Gateway fronts each service separately (`flights-mcp-api`, `hotels-mcp-api`) with token authorizers.
- The servers are stateless by design; each invocation can land on a fresh container.

**AWS guidance:**
- [AWS MCP Server marketplace integration](https://docs.aws.amazon.com/marketplace/latest/userguide/integrating-mcp.html) — *"providers implement an MCP server exposing capabilities (tools, resources, and prompts) via JSON-RPC 2.0"*
- Lambda Web Adapter project: [github.com/awslabs/aws-lambda-web-adapter](https://github.com/awslabs/aws-lambda-web-adapter) — official AWS Labs project for running web frameworks (Express, Flask, FastAPI) on Lambda without rewriting them as native handlers.

**Verdict:** sound pattern for low-latency, low-complexity MCP tool calls on Lambda. **Trade-offs to know:**
1. **Stateless MCP** — no long-lived server-side sessions/subscriptions; tools-only flows are the intended shape.
2. **No streaming responses** — API Gateway REST + direct handler returns JSON responses, not SSE.
3. **Scale-out duplicates warm caches** — each warm container keeps its own in-memory objects; correctness is fine but cache locality is best-effort.

For long-running agentic flows or very high sustained QPS, ECS/Fargate or App Runner is still the better fit.

---

## 5. Pattern: Strands `S3SessionManager` for stateless agents

**What this project does:**
- `lambdas/travel-agent/agent.py` creates `S3SessionManager(session_id=f"session_for_user_{user.id}", bucket=SESSION_STORE_BUCKET_NAME, prefix="agent_sessions")` for each invocation.
- The Lambda function itself is stateless (no in-memory cache survives between invocations on the same container — well, it does, but you can't rely on it).

**Strands guidance** (from MCP search):
- [User Guide — session-management](https://strandsagents.com/docs/user-guide/concepts/agents/session-management/index.md) — `FileSessionManager` for local, `S3SessionManager` for distributed.
- [API ref `strands.session.s3_session_manager`](https://strandsagents.com/docs/api/python/strands.session.s3_session_manager/index.md) — file structure:
    ```
    /<sessions_dir>/
    └── session_<session_id>/
        ├── session.json
        └── agents/
            └── agent_<agent_id>/
                ├── agent.json
                └── messages/
                    ├── message_<id1>.json
                    └── message_<id2>.json
    ```

**Verdict:** correct usage. The session ID embeds user identity, so each user's history is isolated by S3 prefix and the IAM policy on the agent's role only allows the agent to read/write within the session bucket.

**Subtle point: per-user MCP client cache.**

`lambdas/travel-agent/mcp_client_manager.py` keeps an **in-process** dict `mcp_clients` keyed by `user.id`. This is intentional — if the *same user* hits the same warm Lambda container twice, the SDK reuses the MCP client without re-running `initialize`/`tools/list`. But:
- The cache is per-container, not global, so concurrent invocations may each create their own.
- It's not invalidated on logout.
- It conflates concurrency control: if user A's request lands on a container last touched by user B, A still creates a fresh client — fine. But you cannot evict B's client without process restart.

In production this would matter for resource leaks if you have thousands of distinct users hitting the same container; here with two demo users it's invisible.

---

## 6. Pattern: Bedrock invocation via cross-region inference profile

**What this project does:**
- `lambdas/travel-agent/agent_config.py:5` sets `model_id="us.anthropic.claude-3-5-haiku-20241022-v1:0"`.
- The `us.` prefix is a **cross-region inference profile**, which automatically routes to whichever region has capacity (us-east-1, us-west-2, us-east-2 for this profile).
- `lib/agent.js:52-55` IAM policy: `bedrock:InvokeModel` + `bedrock:InvokeModelWithResponseStream` on `Resource: "*"`.

**AWS guidance** (`get_bedrock_patterns`):
- Cross-region inference profiles are recommended for production to avoid regional capacity issues.
- Cost is identical to in-region for the same model.
- Account must have **Marketplace subscription** to the model (Bedrock auto-subscribes the first time the call is denied — which is exactly what happened in our deploy session).

**Verdict:** correct pattern. The `Resource: "*"` is the trade-off (see `analysis/aws-security.md` §2 cluster B).

---

## 7. Pattern: Two REST APIs vs one

**What this project does:**
- One REST API for the agent (`travel-agent-api`)
- Two REST APIs for MCP (`flights-mcp-api`, `hotels-mcp-api`)

**Why split?** Three reasons that make this *not* over-engineering:
1. **Different authorizers** — agent API verifies Cognito tokens (RS256/JWKS); MCP API verifies the agent's HS256 token. Same API would need two authorizers and per-route routing logic.
2. **Different trust domains** — MCP API is internal-service-to-service; agent API is public. Splitting lets you change CORS, rate limits, and access logs independently.
3. **Independent deploys** — you can roll out an MCP change without touching the agent route's deployment.

**Verdict:** good design. Defensible cost (2× $0/month idle, 2× $3.50/M when busy).

---

## 8. Pattern: Lambda Layer for Python deps

**What this project does:**
- `lib/agent.js:18-31` — Python deps (Strands SDK, boto3, etc.) bundled as a layer via Docker `pip install`.
- `layers/dependencies/requirements.txt` lists the deps.

**Why a layer?** Per CDK best practices and the layer's docstring:
> The function code zip stays small → faster deploys & cold-starts. The layer can be reused by other functions in the stack.

**Trade-offs to know:**
1. The layer is rebuilt on every `cdk deploy` if `requirements.txt` changes — no incremental caching beyond Docker's. ~30s overhead.
2. Layer size limit: 250 MB unzipped (all layers combined). Strands + boto3 + opentelemetry is ~150 MB; you have headroom but not infinite.
3. Cross-architecture: must bundle on `linux/arm64` to match the runtime. We hit this bug — the original code used the default `bundlingImage` (x86) which produced x86 wheels for arm64 Lambda → `pydantic_core` import error. Fix: `platform: 'linux/arm64'` and `--platform manylinux2014_aarch64` to pip.

---

## 9. Pattern: Per-user MCP context propagation

**What this project does:**
- Agent mints a JWT containing `user_id`, `user_name`.
- MCP authorizer puts the validated claims on `req.auth`.
- `lambdas/flights-mcp/index.js` and `lambdas/hotels-mcp/index.js` re-verify the JWT in-handler and pass claims into tool context.
- Both MCP services use the same per-component JWT contract (`sub` coupling) and read user claims from verified token fields.

**Why this pattern matters for AI agents:** the LLM only sees what's in the prompt and the tool args. It does NOT see the auth context. So even if the LLM is jailbroken into "ignore previous instructions" or "pretend you're talking to Alice when you're actually Bob", the tool call's auth context is whatever the JWT says — which is set by the agent code, not the LLM.

This is the architectural answer to the *prompt injection / impersonation* class of attacks: identity rides on side-channel claims, not in the conversation. The README brags about it for a reason.

---

## 10. Patterns NOT used (that you might add later)

| Pattern | When to add |
|---|---|
| **Bedrock prompt caching** | When token costs become noticeable (`get_bedrock_patterns` calls this out). Cache the system prompt + tool descriptions, save ~80% on input tokens. |
| **Bedrock Guardrails** | When the agent talks to less-trusted users — content filtering, denied topics, PII redaction. Per-text-unit charge. |
| **Bedrock Knowledge Base** | If you need to ground the agent in corporate documents (travel policy PDFs, etc.). The current implementation hardcodes policies into a tool response. |
| **Bedrock Agents (the managed service)** | Trade-off: less code (no Strands SDK), but locked into Bedrock's orchestration. Strands gives you more control. |
| **Step Functions** for long workflows | If a single user request takes > 30s of agent reasoning, Lambda timeout becomes a problem. Express SFN can hold for 5 min. |
| **HTTP API (instead of REST API)** | 70% cheaper, native JWT authorizer (could delete both Lambda authorizers), lower latency. |
| **CloudFront in front of API Gateway** | If your users are geographically distributed; REGIONAL endpoints are us-east-1 only. |
| **API Gateway WAF** | Rate-limiting, IP allow/deny. Recommended for any public agent endpoint to prevent runaway token cost from abuse. |
| **EventBridge / Step Functions for tool calls** | Async pattern: tools that take >10s (real bookings, external APIs) should be triggered via EB and the agent polls for results. |

---

## 11. References pulled from AWS docs

- [Cognito Hosted UI](https://docs.aws.amazon.com/help-panel/cognito/latest/console/hp-hosted-ui.html)
- [Cognito callback URL constraints](https://docs.aws.amazon.com/sdk-for-kotlin/api/latest/cognitoidentityprovider/aws.sdk.kotlin.services.cognitoidentityprovider.model/-user-pool-client-type/callback-urls.html)
- [API Gateway Lambda Token Authorizer (SAM)](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/sam-property-api-lambdatokenauthorizationidentity.html)
- [API Gateway authorizer cache TTL (CDK)](https://docs.aws.amazon.com/cdk/api/v1/python/aws_cdk.aws_apigateway/RequestAuthorizerProps.html)
- [Set up CloudWatch logging for REST APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-logging.html)
- [Strands SDK session management](https://strandsagents.com/docs/user-guide/concepts/agents/session-management/index.md)
- [Strands `S3SessionManager` API](https://strandsagents.com/docs/api/python/strands.session.s3_session_manager/index.md)
- [AWS MCP Server marketplace integration](https://docs.aws.amazon.com/marketplace/latest/userguide/integrating-mcp.html)
- [Amazon Bedrock pricing — Anthropic models](https://aws.amazon.com/bedrock/pricing/)
- AWS Pricing MCP `get_bedrock_patterns` — architecture patterns for Bedrock applications and cost drivers
