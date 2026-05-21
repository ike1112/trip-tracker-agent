# Trip Tracker Agent — Engineering Analysis

This document explains what the project demonstrates as a piece of
production AI engineering, separate from how to run it. For
operational docs, see
[`README.md`](../README.md),
[`architecture.md`](./architecture.md),
[`DESIGN.md`](./DESIGN.md),
[`SYSTEM.md`](./SYSTEM.md),
[`threat-model.md`](./threat-model.md),
and the seven [`ADRs`](./adr/README.md).

---

## Thesis

Two kinds of work happen in this system. The **non-deterministic**
work — parsing natural-language intent, writing the human-readable
reason behind an alert — runs through the model. The **deterministic**
work — scheduling, persistence, identity, retries, dedup, IAM checks —
runs as plain code. The boundary between the two is the project's
design principle.

The rest of this document is the evidence behind seven engineering
claims that follow from that boundary:

1. [The model handles judgment; everything else is plain code.](#1-the-model-handles-judgment-everything-else-is-plain-code)
2. [User identity is enforced in code, not in the prompt.](#2-user-identity-is-enforced-in-code-not-in-the-prompt)
3. [Cost is bounded by default, not by hope.](#3-cost-is-bounded-by-default-not-by-hope)
4. [Failures are isolated and recoverable.](#4-failures-are-isolated-and-recoverable)
5. [The model's output is evaluated, not just deployed.](#5-the-models-output-is-evaluated-not-just-deployed)
6. [Observability is tested, not just configured.](#6-observability-is-tested-not-just-configured)
7. [Deferred work is named explicitly.](#7-deferred-work-is-named-explicitly)

Each section follows the same shape: **What** the claim is, **How** it
is built, and **Why it matters** for production AI systems.

---

## 1. The model handles judgment; everything else is plain code

**What.** The boundary between non-deterministic and deterministic
work is visible in the file layout, not hidden in a system prompt.
Every call to Bedrock lives in two places: the chat agent's tool loop
([`lambdas/travel-agent/agent.py`](../lambdas/travel-agent/agent.py))
and the poller's alert adjudication step
([`lambdas/poller/bedrock_decide.py`](../lambdas/poller/bedrock_decide.py)).
Every other file is plain Python or JavaScript.

**How.** The chat path runs the model through the Strands agent, which
calls seven local watch-CRUD tools and the MCP price tools. The
scheduled path runs in [`lambdas/poller/app.py`](../lambdas/poller/app.py)
and consults the model only after two deterministic gates have already
agreed that a price movement is worth a closer look:

| Concern | Where the work happens |
|---|---|
| Natural-language watch creation and refinement | Bedrock, via the Strands agent |
| Watch CRUD primitives (`add_watch`, `list_watches`, …) | Python functions in [`lambdas/travel-agent/watches.py`](../lambdas/travel-agent/watches.py) |
| Authorization | [`lambdas/agent-authorizer`](../lambdas/agent-authorizer) and [`lambdas/mcp-authorizer`](../lambdas/mcp-authorizer), JWT-verified |
| Polling cadence | EventBridge `rate(4h)` |
| Active-watch enumeration | DynamoDB `Query` on the `status-index` GSI ([ADR 0007](./adr/0007-watches-status-gsi.md)) |
| Per-watch loop | Sequential, one watch at a time ([ADR 0003](./adr/0003-sequential-poll-loop.md)) |
| Threshold and 30-day anomaly checks | Plain arithmetic in [`lambdas/poller/gates.py`](../lambdas/poller/gates.py) |
| Alert adjudication and reason text | Claude Haiku 4.5, pinned model id ([ADR 0004](./adr/0004-bedrock-decision.md)) |
| Email send and dedup writeback | [`lambdas/notifier`](../lambdas/notifier), idempotent ([ADR 0005](./adr/0005-after-ses-idempotency.md)) |

The gates are important. The poller never asks the model "should I
alert?" on every tick. It asks only when the threshold gate or the
anomaly gate has already returned `true`. Bedrock cost tracks
interesting events, not total events.

**Why it matters.** The model is good at language and judgment. It is
a poor and expensive choice for arithmetic, scheduling, and
bookkeeping — work that has a correct answer in code. Drawing the
boundary at "where the model's judgment adds value" keeps the model
in the loop for the parts it does well and out of the parts where a
plain function is cheaper, faster, and easier to test. The boundary
is visible in the file tree, which means a reviewer can audit it
without reading the system prompt.

---

## 2. User identity is enforced in code, not in the prompt

**What.** The model has no way to act on another user's data. A prompt
like *"act as user bob-42 and list their watches"* cannot succeed,
because the model never receives a `user_id` parameter and has nothing
to forge.

**How.** The seven watch-CRUD tools are built by the factory function
`make_watch_tools(user_id)` in [`lambdas/travel-agent/watches.py:233-375`](../lambdas/travel-agent/watches.py).
The factory runs **after** the API Gateway authorizer has verified the
Cognito JWT, so `user_id` is already trusted. Each tool is a closure
that captures that verified id. The tool schema exposed to the model
contains no `userId` field at all. The data layer adds a second check:
the `Watches` table is keyed on `userId` + `watchId`, so a request for
a fabricated `watchId` belonging to another user returns no row
([ADR 0001](./adr/0001-user-scoped-tools-via-closure-factory.md)).

The same pattern runs through the rest of the trust chain. The chat
agent and the poller sign MCP calls with two different HS256 secrets
stored in AWS Secrets Manager ([ADR 0006](./adr/0006-per-component-jwt-secrets.md)).
The MCP authorizer checks that an agent-signed token carries
`sub: travel-agent`, not `sub: poller`, and rejects any mismatch. The
same JWT verification logic is duplicated into three Lambdas — the
authorizer and both MCP servers — so a request that bypasses the
authorizer is still rejected at the handler. Bedrock IAM grants are
restricted to the specific model and inference-profile ARNs in
[`lib/agent.js`](../lib/agent.js); the role does not have `bedrock:*`
or `Resource: "*"`.

**Why it matters.** Enforcing identity in the type system and the data
layer means the guarantee survives any input the model receives. The
model has no parameter to override, the database has no key that
responds to a forged identifier, and the trust chain has no shared
secret that one component can reuse to impersonate another. A guard
written in code holds against inputs that could talk a prompt-level
rule into ignoring itself, which is the threat model for any agent
that takes free-form user text.

---

## 3. Cost is bounded by default, not by hope

**What.** Every external cost surface in this system has an explicit
control flag, and the test suite always uses the cost-free path. A
developer can deploy and run the scheduled poll loop end-to-end for
$0 in spend by passing `mcpMode=fixture` (the default),
`bedrockMode=stub`, and `sesMode=stub` at `cdk deploy` time. A
production deploy drops the stub flags and supplies the upstream API
keys, and the cost is paid only on the calls that are actually made.
Idle cost at rest is two Secrets Manager secrets, around $0.80 per
month combined.

One exception is honest to name: the chat agent's own Bedrock model
is not stubbable. Chatting with the agent always hits live Bedrock and
costs real tokens, in any deploy mode. The chat path is what an
operator evaluates the agent against, so stubbing it would defeat the
evaluation; the documented Prerequisites require the operator to grant
the agent's model access before the first chat call.

**How.** Three layers, plus a backstop.

The first layer is the mode flags ([ADR 0002](./adr/0002-fixture-replay-mode.md)).
`mcpMode` defaults to `fixture`, which loads recorded JSON responses
instead of calling Duffel or LiteAPI. `bedrockMode` and `sesMode`
default to `live` for production deploys, with `stub` modes available
that return canned responses and skip the live calls; the entire test
suite uses the stub paths regardless of deploy configuration. The chat
agent's Bedrock call is configured separately and runs live in every
deploy mode, as noted above.

The second layer is the gates before the poller's Bedrock call. The
poller runs a threshold check and a 30-day anomaly check in
[`lambdas/poller/gates.py`](../lambdas/poller/gates.py) before calling
the model. Most polls return early at the gates and never consult
Bedrock. Poller model spend scales with the number of polls that
surface a candidate alert, not with the total number of polls.

The third layer is IAM scoping. The Bedrock IAM grant in
[`lib/agent.js`](../lib/agent.js) is restricted to the specific model
and inference-profile ARNs, derived from the same CDK context value
that selects the runtime model. The grant and the runtime call cannot
drift; an attempt to invoke an unauthorised model returns
`AccessDeniedException` instead of a bill.

The backstop is the AWS Budget alarm in
[`lib/budget-alarm.js`](../lib/budget-alarm.js): $10 per month,
notifications at 80% of actual spend and 100% of forecast spend. The
budget fires an email before the money is gone (forecast) and again
when most of it is spent (actual). It does not auto-remediate, because
auto-disable on a personal account is more dangerous than the spend
it would prevent.

**Why it matters.** Cost in an LLM-driven system is a correctness
property, not an operational nuisance: a runaway loop can spend a
month's budget in an afternoon, and a mispriced model call on every
poll compounds over weeks. Giving every cost surface its own control
flag, running the test suite in the cost-free configuration, scoping
IAM to the exact resource ARNs, and backing it all up with a Budget
alarm that fires on the forecast rather than the post-mortem is what
makes the system safe to leave running unattended.

---

## 4. Failures are isolated and recoverable

**What.** A single bad upstream call does not break the schedule. A
single failed alert does not lose dedup state or cause duplicate emails
in steady state. A leaked secret cannot impersonate the other
component.

**How.** Four properties hold together:

**Sequential per-watch loop ([ADR 0003](./adr/0003-sequential-poll-loop.md)).**
The poller iterates `for watch in iter_active_watches()` and catches
`McpCallError`, `ValueError`, and `KeyError` per watch. A failed watch
logs `watch_errored` and the loop continues. The Lambda has
`reservedConcurrentExecutions = 1`, so an EventBridge tick cannot fan
out concurrent pollers that would race the dedup writeback.

**After-SES idempotent writeback ([ADR 0005](./adr/0005-after-ses-idempotency.md)).**
The notifier sends the email first, then writes `lastAlertedAt` back to
the watch under a conditional expression. If SES fails, the writeback
does not happen and the next poll retries (at-least-once). If the
writeback fails after a successful send, the next poll's 5%
price-proximity dedup band catches the duplicate before another email
goes out. The conditional expression protects against out-of-order
retries that would otherwise backdate the dedup state.

**Fail-closed on auth and secrets.** The MCP authorizer rejects on any
verification error, including a failure to fetch the signing secret
from Secrets Manager. There is no path that returns "allow" on an
infrastructure error. The two-secret coupling ([ADR 0006](./adr/0006-per-component-jwt-secrets.md))
means a leaked agent secret cannot mint a poller-valid token, and
vice versa.

**Graceful degradation in the chat path.** If the hotels MCP server is
down, the agent loses hotel tools for that turn but keeps the flight
tools. The user sees a partial answer with an explanation, not a 500.

**Why it matters.** Production AI systems fail in partial ways more
often than they fail completely. The interesting design question is not
"does it work when everything is up" but "what happens during a
partial outage." This project answers that question explicitly for
each boundary, and the answer is documented in the ADRs alongside the
trade-offs.

---

## 5. The model's output is evaluated, not just deployed

**What.** The project answers the question *"how do you know the model
is making the right call?"* with a runnable harness, not an assertion.

**How.** The [`evals/`](../evals) package contains 33 labeled decision
fixtures under [`evals/fixtures/decision/`](../evals/fixtures/decision/),
covering the matrix the gates exist to handle: stable fare (no alert),
clear anomaly (alert), under-budget fresh case (alert), recent dedup
case (no alert), over-budget summer peak (no alert), borderline cases,
lateral-min matches, gates-miss cases, hotel-split cases.

Each fixture carries a labeled expected outcome. The runner in
[`evals/run_evals.py`](../evals/run_evals.py) feeds the fixture through
the same `bedrock_decide` path the poller uses, then calls an LLM-as-
judge ([`evals/judge_client.py`](../evals/judge_client.py)) that uses
Claude Sonnet to grade Haiku 4.5's `{alert, reason}` output against the
label. The judge prompt is checked in at
[`evals/judge_prompts/decision.md`](../evals/judge_prompts/decision.md),
so the grading rubric is reviewable and version-controlled.

A baseline run is committed at
[`evals/results/2026-05-13-baseline.md`](../evals/results/2026-05-13-baseline.md).
The package installs against the pinned
[`requirements-test.txt`](../requirements-test.txt) and runs from CI.
A regression against the baseline fails the run.

**Why it matters.** Model behaviour is an artefact that can regress
the same way a code change can. A prompt edit, a model version
bump, or a change to the gates can shift which fixtures pass without
breaking a unit test. A labeled fixture set, a written grading rubric,
and a committed baseline give a change a visible signal — pass or
fail, with a diff — instead of an opinion. That signal is what makes
it safe to iterate on the prompt or the model id.

---

## 6. Observability is tested, not just configured

**What.** The CloudWatch dashboard would catch a renamed function or a
dropped EMF counter. Most dashboards would not.

**How.** The dashboard construct in
[`lib/observability-dashboard.js`](../lib/observability-dashboard.js)
is pinned by two tests that go beyond "the dashboard exists":

**Metric-shape assertions.** The dashboard tests check the metric
*dimensions* on each widget, not just the labels. A widget that says
"Travel Agent invocations" but reads the dimension of a different
function would otherwise pass. The dimensions are the real link to the
data source; testing them is what makes the dashboard trustworthy.

**Cross-language constant sync.** The poller emits EMF counters from
Python (`watches_polled`, `watches_errored`, `alerts_sent`,
`bedrock_decisions_made`). The JavaScript dashboard reads those names
from a constants block. A sync test compares the two so that renaming
a counter on one side without the other fails the build, not the
dashboard at 3am.

**Two synth passes are byte-identical (`cdk-diff-clean`).** A
non-deterministic CDK construct can introduce a noise diff on every
deploy. The test enforces that two synth passes produce the same
output.

The rest of the observability layer is conventional but complete: every
Lambda has `tracing: ACTIVE` for X-Ray, every record carries an
`xray_trace_id`, the notifier has a CloudWatch alarm on send failures.

**Why it matters.** A dashboard is only useful if the data behind it
is the data the widget thinks it is reading. Testing the *shape* of
the metric — dimensions, names, the link between producer and
consumer — is what keeps a dashboard trustworthy across refactors. A
renamed function or a dropped counter then surfaces as a build
failure instead of a silent gap in the on-call view.

---

## 7. Deferred work is named explicitly

**What.** The ADRs and the threat model contain an explicit list of
what v1 does **not** build, and why. The boundary between "implemented"
and "deferred" is documented, not implied.

**How.** A non-exhaustive list of named deferrals, each tied to its ADR
or scope note:

| Deferred | Where it's named | Why it's deferred |
|---|---|---|
| Bedrock Guardrails on chat free-text | [`DESIGN.md` §3](./DESIGN.md) | The infrastructure-level guards cover the v1 surface; a multi-user release would add this layer. |
| DLQ on async notifier invoke | [`DESIGN.md` §9](./DESIGN.md) | A full SES outage after retry exhaustion loses an alert; acceptable for v1, not for production. |
| SNS bounce / complaint handling | [`DESIGN.md` §9](./DESIGN.md) | Single verified recipient at v1; multi-recipient is the upgrade path. |
| Automated secrets rotation | [ADR 0006](./adr/0006-per-component-jwt-secrets.md) | Manual console + redeploy is the documented process until a rotation Lambda is justified. |
| Sharded GSI partition key for `status` | [ADR 0007](./adr/0007-watches-status-gsi.md) | The current `status` PK is low cardinality; the ADR scopes the decision to personal scale and names a sharded/composite PK as the production answer. |
| Per-watch retry policy on transient MCP errors | [`DESIGN.md` §8](./DESIGN.md) | The sequential loop catches and continues; a typed retry policy is out of scope for v1. |
| Multi-recipient Cognito-driven lookup | [`DESIGN.md` §9](./DESIGN.md) | Documented as the upgrade path; single recipient at v1. |

**Why it matters.** Naming the boundary between v1 and production is
what makes the implemented work credible. Each deferral is paired
with the trade-off that justifies it, so a reviewer can audit the
scope as a set of decisions rather than guessing at omissions. The
list also doubles as a backlog: every entry has a clear next step
and a clear reason it was not the next step *this* time.

---

## Interview talking points

A one-line summary of each section, suitable for a verbal answer to
"walk me through this project":

1. **Design principle.** Non-deterministic work — natural-language
   parsing and the human-readable alert reason — runs through the
   model. Deterministic work — scheduling, persistence, identity,
   retries, dedup, IAM — runs as plain code. The boundary is visible
   in the file tree, not buried in a system prompt.
2. **Identity.** The LLM never sees a `user_id`. Watch tools are
   closures bound to the verified JWT subject, and the database key
   schema is the second line of defense. Identity is enforced by the
   type system, not by the prompt.
3. **Cost.** Every external cost surface has a control flag; the test
   suite always runs the cost-free path; `mcpMode` defaults to
   fixture, `bedrockMode` and `sesMode` have stub modes available
   (the chat agent's Bedrock call is always live and stubbing it
   would defeat agent evaluation); the gates before the poller's
   Bedrock call bound model spend to candidate alerts; IAM grants are
   ARN-scoped; an AWS Budget alarm fires on both actual and forecast
   spend.
4. **Failure isolation.** Sequential per-watch loop with reserved
   concurrency 1; at-least-once SES with a conditional dedup writeback;
   fail-closed authorizers; graceful degradation when one MCP server
   is down.
5. **Evaluation.** 33 labeled decision fixtures, an LLM-as-judge
   harness that uses a stronger model to grade the poller's output,
   and a committed baseline that a regression would fail.
6. **Observability.** Metric *shapes* are tested, not just metric
   existence. A cross-language constant-sync test catches drift
   between the Python EMF producers and the JS dashboard consumers.
7. **Honest scope.** Deferred items are named in the ADRs alongside
   the trade-offs. The boundary between v1 and production is
   documented, not hidden.

---

## References

External documentation, anchored where the project uses the concept:

- [AWS Lambda](https://docs.aws.amazon.com/lambda/) — eight functional
  Lambdas, ARM64, `reservedConcurrentExecutions = 1` on the poller.
- [Amazon Bedrock](https://docs.aws.amazon.com/bedrock/) — chat agent
  (Claude Sonnet 4.5; default 3.5 Haiku) and poller decision
  (Claude Haiku 4.5, pinned model id).
- [Amazon DynamoDB best practices for global secondary indexes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-indexes-general.html)
  — the `status-index` GSI of [ADR 0007](./adr/0007-watches-status-gsi.md).
- [Amazon Cognito](https://docs.aws.amazon.com/cognito/) — User Pool,
  hosted UI, JWKS for the RS256 user JWT.
- [AWS Secrets Manager](https://docs.aws.amazon.com/secretsmanager/) —
  the two HS256 signers of [ADR 0006](./adr/0006-per-component-jwt-secrets.md).
- [Amazon EventBridge](https://docs.aws.amazon.com/eventbridge/) — the
  scheduled `rate(4h)` poller trigger.
- [Amazon SES](https://docs.aws.amazon.com/ses/) — resource-scoped
  sender identity, plain-text body.
- [Amazon CloudWatch](https://docs.aws.amazon.com/cloudwatch/) —
  dashboard, EMF metrics, alarm.
- [AWS X-Ray](https://docs.aws.amazon.com/xray/) — cross-service trace
  on every Lambda.
- [AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html) —
  the $10 per month backstop.
- [Strands Agents SDK](https://strandsagents.com/) — the chat agent,
  `S3SessionManager`, tool patterns.
- [Model Context Protocol](https://modelcontextprotocol.io/) — the
  tool-isolation pattern the flights and hotels servers implement.
- Michael Nygard, *Documenting Architecture Decisions* — the format
  the seven ADRs in [`docs/adr/`](./adr/) follow.
