# Cost Analysis

Source tools:
- **AWS Pricing MCP server** (`get_pricing`, `get_bedrock_patterns`)
- **AWS Knowledge MCP server** — pricing pages for Bedrock, Lambda, API Gateway, Cognito
- Manual service inventory from `lib/*.js` and `cdk.out/StrandsAgentOnLambdaStack.template.json`

> Note: the `analyze_cdk_project` tool returned a partial list (it picked up `iam`, `apigateway`, `logs` but missed `lambda`, `cognito`, `s3`, and pulled false-positives like `sqs`, `sns`, `codecommit` from `node_modules`). Service inventory below is grounded in the CDK source.

---

## TL;DR

| Component | Per-request cost | Idle cost (per month, 1 user, 100 chats) |
|---|---|---|
| Web UI Gradio app | runs locally | $0 — not deployed to AWS |
| Cognito user pool (Lite tier) | — | $0 (free tier: 10K MAU/month) |
| API Gateway REST × 2 | $3.50 / 1M REST API calls | <$0.01 |
| Lambda × 4 (arm64) | tiny — see below | <$0.05 |
| S3 session store | <$0.01 | <$0.01 |
| **Bedrock Claude 3.5 Haiku** | **~$0.0008/1K input + $0.004/1K output** | dominates total |
| CloudWatch Logs | $0.50/GB ingest, $0.03/GB stored | <$0.10 |

**Idle cost (no traffic, just provisioned resources) ≈ $0/month.** Everything is pay-per-use; nothing has a minimum charge except CloudWatch Logs storage if you accumulate a lot.

**Per-chat cost** (one user prompt, one tool call, one model response, ~2K input tokens, ~500 output tokens): **~$0.004 per turn** dominated by Bedrock.

---

## 1. Service inventory (ground truth)

From the CloudFormation template:

| Service | Resources | Notes |
|---|---|---|
| Cognito | 1 UserPool, 1 UserPoolClient (with secret), 1 UserPoolDomain, 2 UserPoolUsers | Lite tier (default), 10K MAU free |
| API Gateway REST | 2 RestAPIs (`travel-agent-api`, `travel-agent-mcp-api`), 2 stages (`prod`), 2 token authorizers | Regional endpoint type |
| Lambda | 4 functions: travel-agent (python3.13, arm64, 1024 MB, 30 s), bookings-mcp-server (nodejs22.x, arm64, 1024 MB, 10 s), 2 authorizers (nodejs22.x, arm64, 1024 MB, 10 s), 1 layer | All arm64 |
| Lambda (CDK-generated) | 2 helper functions: `AwsCustomResource` (Cognito describe) + `AutoDeleteObjects` (S3) | Run only at deploy/destroy |
| S3 | 1 session-state bucket, 1 BucketPolicy | No encryption, no versioning configured |
| Bedrock | (no resource — runtime invocation only) | Claude 3.5 Haiku via cross-region inference profile `us.anthropic.claude-3-5-haiku-20241022-v1:0` |
| IAM | 5 roles + 2 inline policies | All Lambda exec roles + custom resource roles |
| CloudWatch Logs | (no log groups in template) | Auto-created on first invocation, **infinite retention by default** |

CDK staging assets land in the bootstrap bucket `cdk-hnb659fds-assets-<account>-<region>` — those are pre-existing and not counted here.

---

## 2. Per-service pricing (us-east-1, on-demand)

### 2.1 Bedrock Claude 3.5 Haiku — the dominant cost

This is the only paid AWS service that scales linearly with usage in this project.

Anthropic Claude 3.5 Haiku via `us.anthropic.claude-3-5-haiku-20241022-v1:0`:
- **Input tokens**: ~$0.0008 per 1,000 tokens (~$0.80 per 1M)
- **Output tokens**: ~$0.004 per 1,000 tokens (~$4.00 per 1M)
- Batch inference (50% off) is available but doesn't apply to interactive chat.
- Cross-region inference profile: same per-token price as in-region.

(Numbers from [aws.amazon.com/bedrock/pricing/](https://aws.amazon.com/bedrock/pricing/) — Anthropic section. Verify before quoting; AWS sometimes adjusts.)

**Worked example — one chat turn**:
- System prompt + agent tool descriptions: ~1,500 tokens (cached if you turn on prompt caching).
- User message: ~50 tokens.
- Conversation history loaded from S3 (cumulative): ~500 tokens after a few turns.
- Output: ~300 tokens.
- Tool calls might add another round trip (~200 tokens in, ~100 out).

Approx cost per turn: `(1500 + 50 + 500 + 200) × $0.0008/1000 + (300 + 100) × $0.004/1000` ≈ **$0.0034 in + $0.0016 out ≈ $0.005 per turn**.

100 turns/month ≈ **$0.50** at the model layer. That's the dominant line item by far.

**Cost optimization opportunities (per `get_bedrock_patterns`):**
- **Prompt caching** — the system prompt and tool descriptions don't change between turns. Bedrock supports prompt caching for Anthropic models (cache write at $0.001/1K, cache read at $0.00008/1K — about 10× cheaper than input tokens). Implementation: mark the static prefix with `cache_control: { type: 'ephemeral' }`. Could cut input tokens by ~80%.
- **Switch to batch** for non-interactive use cases (50% off). Not applicable here — the user expects sub-2s replies.
- **Stop excluding history** — if cost gets high, summarize old conversation rather than appending raw messages.

### 2.2 Lambda

Source: [aws.amazon.com/lambda/pricing/](https://aws.amazon.com/lambda/pricing/)
- **Requests**: $0.20 per 1M
- **Duration (arm64)**: $0.0000133334 per GB-second (about 20% cheaper than x86's $0.0000166667)
- **Free tier**: 1M requests + 400,000 GB-seconds per month, **does not expire**.

**This project's pattern per chat turn**:
- 1 invocation of `travel-agent-on-lambda` (~3s @ 1024 MB = 3 GB-s)
- 1 invocation of `travel-agent-authorizer` (~50 ms cached, ~500 ms cold; effectively free)
- 1+ invocation of `bookings-mcp-server` (~50 ms @ 1024 MB)
- 1+ invocation of `bookings-mcp-server-authorizer` (~10 ms — cached for 5 min)

A chat turn ≈ **3.1 GB-seconds + 4 requests**. Cost ≈ `3.1 × $0.0000133334 + 4 × $0.0000002` ≈ **$0.0000420** per turn — under a hundredth of a cent. **Effectively free** at any reasonable demo scale.

Cold-start adds ~3s init for the Python agent (Strands SDK + boto3 imports). For a demo, OK. For production: Lambda SnapStart (Python supported as of late 2024) or Provisioned Concurrency.

### 2.3 API Gateway REST

Source: [aws.amazon.com/api-gateway/pricing/](https://aws.amazon.com/api-gateway/pricing/)
- **REST APIs**: $3.50 per million calls (us-east-1, first 333M)
- **Free tier**: 1M REST API calls/month for the first 12 months (new accounts only)
- Data transfer out: standard AWS rates
- No charge for authorizer invocations themselves — those are billed as Lambda invocations.

Per chat turn: 2 REST API calls (1 to agent API, 1 to MCP API). **$0.000007 per turn**, again sub-penny.

> Migration note for cost: HTTP APIs are ~70% cheaper ($1.00/M vs $3.50/M) and would work here. The only blockers are (a) HTTP APIs use JWT authorizers natively, which would let you delete the Lambda authorizers entirely — *that's a feature*, not a regression — and (b) HTTP APIs don't support the API key/usage plan flow used by some clients (irrelevant here). Worth a future swap.

### 2.4 Cognito

Source: [aws.amazon.com/cognito/pricing/](https://aws.amazon.com/cognito/pricing/)
- **Lite tier** (this project's default): **first 10,000 MAUs free** — does not expire.
- Beyond 10K MAU/month: tiered (~$0.0055/MAU at low volumes, less at high).
- M2M token requests are paid (not used here).

For this demo with 2 users (Alice, Bob), Cognito is **free forever**.

### 2.5 S3

- Storage: $0.023/GB-month (Standard)
- Requests: $0.005/1K PUT, $0.0004/1K GET
- Strands `S3SessionManager` writes a JSON blob per message (the conversation history). For 100 turns, expect <1 MB of objects total.
- Cost: well under **$0.01/month** for any plausible demo usage.

### 2.6 CloudWatch Logs

- Ingest: **$0.50/GB**
- Storage: **$0.03/GB-month** (after first 5 GB free in some accounts)
- The agent Lambda logs ~2 KB per invocation (user JWT excerpts, prompt, response). 1000 turns = ~2 MB ingested ≈ $0.001.
- **However — without explicit `logRetention`, log groups grow unbounded.** Over years, this becomes the silent cost driver. Set `logs.RetentionDays.ONE_WEEK` or `ONE_MONTH`.

---

## 3. Idle (no-traffic) cost

| Resource | Charged when idle? |
|---|---|
| Cognito User Pool, Users, Hosted Domain | No |
| API Gateway REST APIs (no requests) | No |
| Lambda functions (no invocations) | No |
| Lambda Layer (storage) | Free |
| S3 bucket (empty) | No (pennies if not empty) |
| IAM roles/policies | Free |
| CloudWatch Logs (existing logs) | $0.03/GB-month for stored bytes |

**Idle baseline ≈ $0.00–$0.10/month** depending on accumulated logs.

---

## 4. Per-month cost models

### Model A — Demo / prototype (you, occasionally)

- 50 chats/month, ~5 turns each = 250 turns
- Bedrock: ~$1.25
- Lambda + API Gateway: <$0.01 (within free tier)
- S3 + Logs: <$0.10
- **Total ≈ $1.35/month**

### Model B — Internal team trial (50 employees)

- 50 users × 30 turns/day × 22 working days = 33,000 turns
- Bedrock: ~$165 (the rest is rounding error)
- Lambda + API Gateway: ~$1
- Logs (with 1-week retention): ~$2
- **Total ≈ $170/month**

### Model C — Production-scale corporate use (1000 active employees, 100K MAU)

- 1000 users × 30 turns/day × 22 days = 660,000 turns
- Bedrock: ~$3,300 (with prompt caching, ~$700)
- Lambda + API Gateway: ~$25
- Logs: ~$30
- **Cognito tips out of free tier** if MAU > 10K: ~$50/month for 100K MAU on Lite
- **Total ≈ $3,400/month** (or ~$800 with prompt caching)

The Bedrock model layer dominates at every scale — the rest is rounding error. **Prompt caching is the highest-leverage optimization** above demo scale.

---

## 5. Cost-related findings & recommendations

1. **Enable prompt caching** for the system prompt + tool descriptions. ~80% input-token reduction at meaningful scale.
2. **Set `logRetention` on every Lambda** — currently infinite. Even at low traffic, logs accumulate over years.
3. **Consider HTTP APIs** instead of REST APIs — 70% cheaper per call, JWT authorizer is built in (could delete the two Lambda authorizers entirely). Latency also lower.
4. **No NAT, no VPC** — good. NAT Gateways are the #1 silent surprise on AWS bills (~$32/month base + data). This project correctly avoids them.
5. **arm64 Lambdas** — already done (`lib/strands-agent-on-lambda-stack.js:9`). Costs ~20% less than x86.
6. **The S3 bucket has `autoDeleteObjects: true`** — costs come back to zero on `cdk destroy`. No orphaned-data risk.
7. **No CloudWatch alarms** — at this scale you don't need them, but at production scale, set Bedrock-spend alarms; runaway agent loops can rack up token cost fast.

---

## 6. References

- [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) — Anthropic table
- [AWS Lambda pricing](https://aws.amazon.com/lambda/pricing/) — arm64 Graviton2 ~20% cheaper than x86
- [Amazon API Gateway pricing](https://aws.amazon.com/api-gateway/pricing/) — REST $3.50/M, HTTP $1.00/M
- [Amazon Cognito pricing](https://aws.amazon.com/cognito/pricing/) — Lite tier 10K MAU free, no expiry
- AWS Pricing MCP server `get_bedrock_patterns` — token-counting assumptions and prompt caching pattern
