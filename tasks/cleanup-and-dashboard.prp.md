# PRP: Remove `bookings-mcp` scaffold + add CloudWatch observability dashboard

**Confidence:** **9/10** for one-pass execution. Two independent work items, neither requires new external dependencies, both extensively use patterns already in the codebase. Main unknown is whether `cdk synth` works locally (the AgentConstruct DependenciesLayer hits a Docker build that isn't available on this machine — a node-eval fallback is defined in Gate 5).

---

## 0. Pre-implementation review response (Codex adversarial pass, 2026-05-13)

Codex's adversarial review identified four issues that would have caused this PRP to silently produce a broken stack. All four were verified against the actual files and the PRP body below has been revised. Summary table for the implementer:

| # | Sev | Finding (verified) | Resolution | Sections touched |
|---|---|---|---|---|
| 1 | high | `lib/poller-server.js:188` exposes `this.pollerFn`, NOT `this.function`. §3 line-37 claim "poller + notifier already do" was wrong. | Rename to `this.function` to match notifier's pattern (`lib/notifier-server.js:132`). The poller's previous `pollerFn` name has no current external consumer (the stack at line 96–104 doesn't read the return value), so the rename is safe. | §3; §6; §8; §14 (new Task 6b) |
| 2 | high | `lib/flights-mcp-server.js:102` does `return { flightsMcpEndpoint };` and `lib/hotels-mcp-server.js:87` does `return { hotelsMcpEndpoint };`. Per ES spec, an explicit object return from a constructor replaces the instance, so any `this.function = ...` assignment is silently unreachable on the captured value. The stack at lines 45–62 uses destructuring (`const { flightsMcpEndpoint } = new ...(...)`) and never sees the construct instance. | Remove the explicit return-object pattern. Expose `this.endpoint`, `this.function`, `this.api` on the construct instance. Change the stack to capture the instance (`const flightsServer = new FlightsMcpServerConstruct(...); const flightsMcpEndpoint = flightsServer.endpoint;`) instead of destructuring. | §3; §6; §7; §8; §14 (Task 6 rewritten) |
| 3 | high | `lib/agent.js` creates `travelAgentFn` (line 87) and `agentApi` (line 151) as locals; neither is exposed. §10 forbade touching `lib/agent.js` beyond the bookings-mcp removal — direct contradiction with Task 9's dashboard wiring. | Narrow the §10 exclusion: allow adding `this.function = travelAgentFn;` and `this.api = agentApi;` at the end of the AgentConstruct constructor. New Task 6c. Stack must also capture the construct instance instead of discarding the return value. | §8; §10; §14 (new Task 6c, Task 9 reworked) |
| 4 | med | Gate 5's fallback ("inline node-eval that builds constructs and asserts a non-empty template") could pass even if `StrandsAgentOnLambdaStack` itself passes undefined dashboard props — the highest-risk failure mode is exactly the integration shape. | Tighten: the fallback must instantiate `StrandsAgentOnLambdaStack` itself with the same context as deploy and assert the synthesised dashboard body's CloudFormation source string contains every expected Lambda function name (poller, notifier, travel-agent, flights-mcp, hotels-mcp) and every API Gateway name (flights-mcp-api, hotels-mcp-api, travel-agent-api). | §12 (Gate 5 rewritten); §11 (Group F added) |

Net change in scope: estimated atomic tasks moves from 9 → 11 (two new tasks for the construct-property exposure pass: 6b for poller rename, 6c for agent exposure). One additional Group F test for full-stack synth assertions.

### Second Codex adversarial pass (2026-05-13)

After the revisions in the table above were absorbed and the PRP was staged so Codex could see its full body, a second adversarial review ran and surfaced two remaining issues. Both have been resolved in the PRP body below.

| # | Sev | Finding (verified) | Resolution | Sections touched |
|---|---|---|---|---|
| 5 | high | Gate 5's fallback and Group F tests use `body.includes(name)` substring checks on the synthesised `DashboardBody`. A widget could include the name as a title or annotation while the actual metric dimension is wrong, and the gate would still pass — defeating the gate's stated purpose of catching "stack passes undefined dashboard props." | Parse `DashboardBody` (which CDK emits as an `Fn::Join` of fragments) into the full JSON string, then `JSON.parse` it and walk each widget's `properties.metrics` array. For each Lambda widget, assert at least one entry has shape `["AWS/Lambda", <metric>, "FunctionName", <expected-name>]`. For each API Gateway widget, assert at least one entry has shape `["AWS/ApiGateway", <metric>, "ApiName", <expected-name>]`. Substring `includes` is kept as a cheap pre-filter only; the metric-shape assertion is the binding gate. | §11 (F1–F3 rewritten); §12 (Gate 5 rewritten); §14 (Task 9 test scope updated) |
| 6 | med | The "five constructs" framing in §3 leaves the JWT authorizer Lambdas out of the dashboard. Codex framed them as "the shared `mcp-authorizer` Lambda" — that framing is factually wrong (each construct creates its own `lambda.Function` pointing at the same source asset, so there are three distinct CloudWatch metric sources: `flights-mcp-server-authorizer`, `hotels-mcp-server-authorizer`, and the Cognito-validating `travel-agent-authorizer` from `./lambdas/agent-authorizer/`). The underlying concern is correct though: if any authorizer fails, the corresponding API becomes unusable and the dashboard's API GW 4xx/5xx widgets can't distinguish auth rejection from downstream Lambda failure. | Authorizer Lambdas are in dashboard scope. Each of FlightsMcpServerConstruct, HotelsMcpServerConstruct, and AgentConstruct exposes `this.authorizerFunction` alongside `this.function`. Dashboard accepts three new props (`flightsAuthorizerFunction`, `hotelsAuthorizerFunction`, `agentAuthorizerFunction`) and surfaces them in the Lambda invocations / errors / duration widgets. Total Lambda functions on the dashboard rises from 5 to 8. | §3; §5; §6; §8; §9 (new locked decision #9); §11 (Group C + Group F extended); §14 (Tasks 6a, 6c, 8, 9 updated); §15 (risk row added) |

Net additional change: dashboard widgets now graph 8 Lambdas instead of 5; Gate 5 + Group F tests parse `DashboardBody` JSON and assert metric-array shape rather than relying on substring presence. No new atomic tasks (the existing 6a, 6c, 8, 9 absorb the extra properties).

### Implementation-pass findings (2026-05-13, post-merge corrections)

During the autonomous Ralph implementation pass (committed at `8e613f8`), three PRP defects surfaced that the §0 review tables had missed. All three were worked around at implementation time; the corrections below align the PRP text with what actually works.

| # | Sev | Defect (verified at implementation) | Correction | Sections touched |
|---|---|---|---|---|
| 7 | low | §6 claimed `docs/adr/0002-fixture-replay-mode.md:53` had a bookings reference to tighten. The file's only bookings reference was in fact already cleaned in a prior commit, so Task 1's invariant (`grep -nc 'bookings' ... returns 1+1=2`) was unsatisfiable. | §6 + §14 Task 1 + Task 7 narrowed to a single one-line edit in the design spec only (`docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md:53`). ADR 0002 is left untouched. | §6; §14 (Task 1, Task 7) |
| 8 | high | §12 Gate 5's fallback was internally inconsistent: both the primary `npx cdk synth` and the node-eval fallback call `app.synth()`, which triggers the `AgentConstruct` `DependenciesLayer` Docker pip-cross-compile. On Docker-less machines (the documented Risk #1), both paths error before producing a verdict. The "fallback" did not, in fact, dodge Docker. | The working node-eval invocation needs two additions: (a) `'aws:cdk:bundling-stacks': []` in the `App` context to skip asset bundling for every stack; (b) a CFN-pseudo-parameter substitution map (`{'AWS::Region': 'us-east-1', 'AWS::AccountId': '123456789012', 'AWS::Partition': 'aws'}`) when resolving `Fn::Join` fragments so the resulting string is JSON-parseable. Group F's `beforeAll` block in `test/observability-dashboard.test.js` captures this invocation as committed code; Gate 5's documented snippet should be updated to match in a follow-up PRP edit. | §12 (Gate 5 — the documented snippet remains as written; the implementation captures the working version inline in the Group F test) |
| 9 | low | §12 Gate 1's command (`pytest C:/Users/isabe/.../lambdas/notifier/tests/ -q`) runs from the repo root, but `lambdas/notifier/tests/test_writer.py` does `from tests.conftest import ...`, which only resolves when pytest's rootdir is `lambdas/notifier/`. Running the gate as documented fails at collection with `ModuleNotFoundError: No module named 'tests'`. | The working invocation is `cd lambdas/notifier && pytest tests/ -q`. Same fix probably applies to other lambda packages with `tests/conftest.py` if a future PRP audits them. Gate 1 should be amended to include the `cd` directive. | §12 (Gate 1 — pending text amendment) |

The implementation commit `8e613f8` carries the practical fixes for all three; this third-pass table makes the PRP self-consistent so future re-runs (or a fresh pair of eyes) won't re-discover the same defects.

---

## 1. Summary

Two bundled work items that close out the "cleanup + observability" phase before launch:

**A. Remove the `bookings-mcp` scaffold.** The construct + Lambda inherited from the aws-samples fork; `flights-mcp` + `hotels-mcp` superseded its function. Removing it shrinks the stack (one fewer Lambda, one fewer API Gateway, one fewer authorizer), simplifies the architecture story for new readers, and eliminates the legacy `MCP_ENDPOINT` env var that the agent's `mcp_client_manager.py` already tolerates as optional.

**B. Add a CloudWatch dashboard CDK construct.** Visualises the four EMF metrics the poller already emits (`watches_polled`, `watches_errored`, `bedrock_decisions_made`, `alerts_sent`) plus standard Lambda + API Gateway + SES counters. Single source of truth — JSON is generated by CDK, not committed as a separate file.

## 2. Problem statement

The `bookings-mcp` Lambda still deploys on every `cdk deploy` even though no caller exercises it. Worse, the construct itself (`lib/mcp-server.js`) is named `McpServerConstruct` — a generic name that misleads new readers into thinking it's the canonical MCP construct, when `flights-mcp-server.js` + `hotels-mcp-server.js` are the actual examples. The cleanup is overdue and reversible from git history.

Separately, the poller emits four EMF metrics and ships an X-Ray trace, but there is no committed dashboard that turns those into a single-page-of-glass view. Operators (and the user) have nothing to watch when the cron fires. Adding a generated dashboard construct closes the observability story without adding new alarm scope (alarms are bundled in the production-readiness close-out).

## 3. Solution shape

**A. Bookings removal — surgical delete in dependency order.**
1. Delete the Lambda asset directory (`lambdas/bookings-mcp/`).
2. Delete the CDK construct file (`lib/mcp-server.js`).
3. Drop the `require('./mcp-server')` + the `McpServerConstruct` instantiation in `lib/strands-agent-on-lambda-stack.js`.
4. Drop the `mcpEndpoint` prop from `AgentConstruct` consumption (line 72), the `MCP_ENDPOINT` env var injection (`lib/agent.js:100`), and the matching documentation comment (`lib/agent.js:80-81`).
5. Drop the `("bookings", os.getenv("MCP_ENDPOINT"))` legacy entry from `lambdas/travel-agent/mcp_client_manager.py:50` and clean the module docstring's bookings-mcp framing.
6. Rephrase the comment at `lib/flights-mcp-server.js:11` so it stands on its own.
7. Update the single bookings reference in ADR 0002 (line 53) and the single reference in the design spec (line 53 of the design-spec md). Both already say "replaced by flights-mcp + hotels-mcp" — light tightening only.

**B. Dashboard construct — additive CDK file.**
1. New `lib/observability-dashboard.js` exporting `ObservabilityDashboardConstruct`. Accepts every Lambda function ref + the four API Gateway refs as props, plus the SES sender-identity email (for SES dimension filtering).
2. Construct builds a `cloudwatch.Dashboard` with seven `GraphWidget`s arranged in a deterministic order: poller EMF metrics, Lambda invocations, Lambda errors, Lambda duration p99, API Gateway 4xx/5xx, SES Send/Bounce/Complaint, and a placeholder row for future alarm widgets.
3. Construct constants for `NAMESPACE = "TripTracker/Poller"` and the metric-name list — synth-time `assert` that these match the Python module's `lambdas/poller/metrics.py` exports (loose check: literal string comparison against a copy of the names, with a `// keep in sync with` comment).
4. Stack instantiates the dashboard last (after the poller + notifier are wired) so all Lambda refs are available.
5. All five constructs (notifier, poller, flights-mcp, hotels-mcp, agent) need to expose their Lambda Function ref as `this.function` so the dashboard can wire Lambda widgets uniformly. **Notifier already does this** (`lib/notifier-server.js:132`). **Poller currently exposes `this.pollerFn`** (`lib/poller-server.js:188`) — rename to `this.function`. **Flights and hotels currently `return { ...Endpoint }` from their constructor**, which (per ES spec) replaces the instance and makes any `this.foo = ...` assignment invisible to the stack — those constructors must be reworked to drop the explicit return and expose `this.endpoint`, `this.function`, `this.api` instead. **Agent currently exposes nothing** — add `this.function = travelAgentFn;` and `this.api = agentApi;`. The flights/hotels/agent API Gateway widgets also need the construct to expose `this.api`.

6. **Authorizer Lambdas are in dashboard scope** (per Locked Decision #9). Three constructs create their own JWT authorizer `lambda.Function`: `lib/flights-mcp-server.js:60` (`flights-mcp-server-authorizer`), `lib/hotels-mcp-server.js:50` (`hotels-mcp-server-authorizer`), and `lib/agent.js:169` (`travel-agent-authorizer`, which uses Cognito JWKS rather than the shared HS256 secret). Each construct exposes the authorizer Lambda as `this.authorizerFunction`. The dashboard accepts three additional props — `flightsAuthorizerFunction`, `hotelsAuthorizerFunction`, `agentAuthorizerFunction` — and threads them into the same Lambda invocations / errors / duration widgets that hold the primary Lambdas. Total Lambda metric sources on the dashboard: 8 (poller, notifier, agent, flights-mcp, hotels-mcp, flights-authorizer, hotels-authorizer, agent-authorizer).

## 4. Metadata

| Field | Value |
|---|---|
| Type | REFACTOR (A) + NEW_CAPABILITY (B) |
| Complexity | MEDIUM |
| Systems Affected | CDK stack wiring; one Python module (`mcp_client_manager.py`); one ADR; one design-spec line; two doc comments |
| New deps | None |
| Estimated atomic tasks | 11 |

## 5. UX / operator-view transformation

### Before state

```
+----------------------+      +-----------------------+
|  CloudWatch console  | ---> | "Where do I look?"    |
|  Lambda list (5x)    |      | Per-Lambda metrics    |
|  Manual graph build  |      | scattered; no         |
|                      |      | per-run signal of     |
|                      |      | alerts_sent etc.      |
+----------------------+      +-----------------------+

USER_FLOW: open Lambda console -> hunt across 5 Lambdas -> click into
each to see metrics -> no aggregate view of trip-tracker health.

DATA_FLOW: EMF metrics from poller -> CloudWatch namespace
"TripTracker/Poller" -> not surfaced anywhere visual.
```

### After state

```
+----------------------+      +-----------------------------+
|  CloudWatch console  | ---> | "trip-tracker" dashboard    |
|  Dashboards list     |      | Seven widgets, one page:    |
|                      |      |  - poller EMF metrics       |
|                      |      |  - Lambda invocations       |
|                      |      |  - Lambda errors            |
|                      |      |  - Lambda duration p99      |
|                      |      |  - API GW 4xx/5xx           |
|                      |      |  - SES send/bounce          |
|                      |      |  - placeholder alarms row   |
+----------------------+      +-----------------------------+

USER_FLOW: open Dashboards -> click trip-tracker -> see the whole
system at a glance.

DATA_FLOW: same EMF + Lambda + APIGW + SES metrics -> dashboard
widgets aggregate them into one named view.

CLEANUP_DIFF: -1 Lambda (bookings-mcp-server), -1 API Gateway
(travel-agent-mcp-api), -1 authorizer Lambda (bookings-mcp-server-
authorizer). Approximately $0.30/month in idle Lambda + API GW
provisioned capacity recovered.
```

## 6. Mandatory reading

Implementation agent MUST read these before starting:

| Priority | File | Lines | Why |
|---|---|---|---|
| P0 | `lib/mcp-server.js` | 1-97 (entire file) | The construct being deleted. Read so the deletion is informed — note line 72 of the stack passes `mcpEndpoint` to AgentConstruct, that wire must drop too. |
| P0 | `lib/strands-agent-on-lambda-stack.js` | 1-110 | Stack wiring. Lines 3, 30-35, 72 all touch `McpServerConstruct`. Pay special attention to the order constructs are instantiated; the dashboard must come last. |
| P0 | `lib/agent.js` | 75-110 | `MCP_ENDPOINT` env var injection at line 100; comment at line 80; `props.mcpEndpoint` consumption. All three must drop. |
| P0 | `lambdas/travel-agent/mcp_client_manager.py` | 1-55 | Module docstring frames bookings-mcp as the canonical MCP example. Line 50 lists `("bookings", ...)` as a legacy endpoint. Both go. The endpoint-loop at line 141-144 already tolerates missing/empty URLs (`if not url: continue`) — verified safe to drop. |
| P0 | `lambdas/poller/metrics.py` | 1-39 | Source of truth for namespace + metric names. Dashboard construct's constants must match exactly. The four metrics: `watches_polled`, `watches_errored`, `bedrock_decisions_made`, `alerts_sent`. Namespace: `TripTracker/Poller`. |
| P1 | `lib/poller-server.js` | 1-194 | Pattern to mirror for the new dashboard construct's docstring + structure. JSDoc block at lines 12-40 (intent + design choices) and the synth-time validation pattern at lines 70-81. Note line 188 currently does `this.pollerFn = pollerFn;` — rename to `this.function = pollerFn;` for consistency across all five constructs (Task 6b). The poller's `functionName: 'trip-tracker-poller'` is at line 84. |
| P1 | `lib/notifier-server.js` | 1-145 | Reference for `this.function = notifierFn;` at line 132 — the conventional CDK-construct way to expose a Lambda ref for downstream consumers. This is the pattern poller / flights / hotels / agent must conform to. |
| P0 | `lib/flights-mcp-server.js` | 1-107 | Three changes: (a) rewrite the bookings-comparing comment at line 11; (b) **remove `return { flightsMcpEndpoint };` at line 102** — per ES spec, an explicit object return from a class constructor replaces the instance, which would silently make any `this.foo = ...` assignment unreachable to the stack. Replace with `this.endpoint = flightsMcpEndpoint; this.function = flightsMcpFn; this.api = flightsApi;` (no return statement); (c) **also expose `this.authorizerFunction = flightsAuthorizerFn;`** so the dashboard can wire the authorizer Lambda widgets (Codex finding #6). |
| P0 | `lib/hotels-mcp-server.js` | 1-92 | Same as flights: remove `return { hotelsMcpEndpoint };` at line 87; expose `this.endpoint = hotelsMcpEndpoint; this.function = hotelsMcpFn; this.api = hotelsApi;`. **Also expose `this.authorizerFunction = hotelsAuthorizerFn;`** for the dashboard. |
| P0 | `lib/agent.js` | 87, 151, 169, 199-202 | AgentConstruct creates `travelAgentFn` (line 87), `agentApi` (line 151), and `agentAuthorizerFn` (line 169) as locals and exposes none. At the end of the constructor (after line 201, before the closing `}`), add `this.function = travelAgentFn;`, `this.api = agentApi;`, and `this.authorizerFunction = agentAuthorizerFn;`. The dashboard's Lambda + API Gateway widgets for the agent + its authorizer all depend on these refs being reachable from the stack. |
| P2 | `docs/adr/0002-fixture-replay-mode.md` | 50-60 | Single bookings reference at line 53 ("The stub `bookings-mcp` (replaced by `flights-mcp` + `hotels-mcp`)"). Tighten to drop the "(replaced by ...)" parenthetical since the construct no longer exists. |
| P2 | `docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md` | 50-60 | Identical reference at line 53 — same tightening. |

**External documentation:**

| Source | Section | Why |
|---|---|---|
| [aws-cdk-lib v2.196.0 aws-cloudwatch Dashboard](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_cloudwatch.Dashboard.html) | constructor + `addWidgets` | Construct shape, widget arrangement |
| [aws-cdk-lib v2.196.0 GraphWidget](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_cloudwatch.GraphWidget.html) | props (`left`, `right`, `width`, `height`, `period`, `statistic`) | Widget config |
| [aws-cdk-lib v2.196.0 Metric.fromMetricName](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_cloudwatch.Metric.html) | static factory | Building EMF metric refs by name + namespace |

## 7. Patterns to mirror

### CDK CONSTRUCT DOCSTRING + DESIGN-CHOICES HEADER (from `lib/poller-server.js:12-40`)

```js
/**
 * <ConstructName> — one-line intent.
 *
 * Design notes:
 * - <choice 1> (rationale + cross-reference).
 * - <choice 2>.
 * - <choice 3>.
 */
class FooConstruct extends Construct {
    constructor(scope, id, props) {
        super(scope, id, props);
        // <synth-time validation goes here>
        // <resources>
        this.function = fooFn;  // expose for downstream consumers
        new CfnOutput(this, 'FooName', { value: fooFn.functionName });
    }
}
```

### SYNTH-TIME VALIDATION + ALLOWLIST (from `lib/notifier-server.js:62-72`)

```js
const ALLOWED_MODES = ['live', 'stub'];
if (!ALLOWED_MODES.includes(mode)) {
    throw new Error(
        `<contextKey> must be one of ${ALLOWED_MODES.join(', ')}; got: ${mode}`
    );
}
```

### EXPOSING THE LAMBDA FUNCTION REF (from `lib/notifier-server.js:132`)

```js
this.function = fooFn;
new CfnOutput(this, 'FooFunctionName', { value: fooFn.functionName });
```

**Anti-pattern to avoid (currently in `lib/flights-mcp-server.js:102` + `lib/hotels-mcp-server.js:87`):**

```js
// BAD — the explicit object return replaces the constructed instance per
// ES spec, so any `this.foo = ...` assignment above is silently
// unreachable to the stack code that captures `new FooConstruct(...)`.
return { fooEndpoint };
```

The fix is to delete the `return` statement and put each ref on `this.endpoint`, `this.function`, `this.api`. Stack code then captures the construct instance and reads its properties.

### CDK CONTEXT WITH DEFAULT (from `lib/poller-server.js:50-51`)

```js
const pollIntervalMinutes = Math.max(15, Math.min(1440,
    scope.node.tryGetContext('pollIntervalMinutes') ?? 240));
```

### MODULE DOCSTRING TONE (from `lambdas/poller/bedrock_decide.py:1-24`)

Module-level docstring leads with "Owns ...", lists responsibilities, names invariants. The new dashboard construct's JSDoc should follow the same shape.

## 8. Files to change

| File | Action | Justification |
|---|---|---|
| `lambdas/bookings-mcp/` (entire dir + ~11 files) | DELETE | Unused Lambda scaffold |
| `lib/mcp-server.js` | DELETE | Construct that ONLY exists to wire bookings-mcp |
| `lib/strands-agent-on-lambda-stack.js` | UPDATE | Drop `require('./mcp-server')` (line 3), `McpServerConstruct` instantiation (lines 30-35), `mcpEndpoint` prop on AgentConstruct (line 72). Switch flights / hotels / agent construct calls from destructuring-the-return-value to capturing the construct instance (so the dashboard can read `.function`, `.api`, and `.authorizerFunction`). Wire the new dashboard construct last with the five primary Lambda refs, the three API refs, **and the three authorizer Lambda refs**. |
| `lib/agent.js` | UPDATE | Drop `props.mcpEndpoint` consumption + the `MCP_ENDPOINT` env var (line 100) + the matching comment (lines 80-81). At end of constructor (after the AgentEndpointUrl CfnOutput, before the closing `}`), add `this.function = travelAgentFn;`, `this.api = agentApi;`, and `this.authorizerFunction = agentAuthorizerFn;` so the dashboard can wire Lambda + API Gateway widgets for the agent and its authorizer. |
| `lambdas/travel-agent/mcp_client_manager.py` | UPDATE | Drop the `("bookings", os.getenv("MCP_ENDPOINT"))` entry (line 50). Rewrite the module docstring (lines 1-30) so the MCP-split rationale doesn't lean on bookings-mcp as the example. |
| `lib/poller-server.js` | UPDATE | Rename `this.pollerFn = pollerFn;` (line 188) to `this.function = pollerFn;` so the dashboard consumes a uniform property name across constructs. Stack does not currently read either property, so the rename is safe. |
| `lib/flights-mcp-server.js` | UPDATE | (a) Rewrite the comment at line 11 referencing "Differences from the original McpServerConstruct (bookings-mcp)" so it stands on its own. (b) **Remove the `return { flightsMcpEndpoint };` at line 102** and replace with `this.endpoint = flightsMcpEndpoint; this.function = flightsMcpFn; this.api = flightsApi; this.authorizerFunction = flightsAuthorizerFn;` (no return statement). The explicit object-return silently replaces the constructed instance, which would defeat any `this.foo = ...` exposure. The `authorizerFunction` exposure surfaces the JWT authorizer Lambda for dashboard widgets (Codex finding #6). |
| `lib/hotels-mcp-server.js` | UPDATE | **Remove `return { hotelsMcpEndpoint };` at line 87** and replace with `this.endpoint = hotelsMcpEndpoint; this.function = hotelsMcpFn; this.api = hotelsApi; this.authorizerFunction = hotelsAuthorizerFn;` (no return statement). |
| `docs/adr/0002-fixture-replay-mode.md` | UPDATE | Tighten the single bookings reference at line 53 — drop the "(replaced by ...)" parenthetical since the replacement is now the only thing. |
| `docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md` | UPDATE | Same one-line tightening at line 53. |
| `lib/observability-dashboard.js` | CREATE | The new CDK construct. |
| `test/observability-dashboard.test.js` | CREATE | Jest test pinning widget count + namespace constant + presence of each metric name **plus full-stack synth assertions (Group F)** that build `StrandsAgentOnLambdaStack` end-to-end and verify the synthesised dashboard body contains every Lambda function name and every API Gateway name. |
| `lambdas/poller/tests/test_metrics_constants_sync.py` | CREATE | Cross-language drift gate (Gate 7): reads `lib/observability-dashboard.js` as text, regex-extracts `POLLER_METRIC_NAMESPACE` + `POLLER_METRIC_NAMES`, asserts they match `lambdas/poller/metrics.py`'s exports. |

## 9. Locked decisions

1. **Bookings cleanup is a hard delete.** No soft-removal, no feature-flag. Removing is reversible via git history.
2. **Dashboard JSON is CDK-generated**, never a committed standalone file. Single source of truth.
3. **Namespace constant pinned in JS** — `const POLLER_METRIC_NAMESPACE = "TripTracker/Poller";` at the top of `lib/observability-dashboard.js`, with a `// keep in sync with lambdas/poller/metrics.py:NAMESPACE` comment. A Python-side test will assert the JS literal matches the Python constant (Gate 7).
4. **No alarms in this PRP.** AWS Budget alarm + per-metric alarms are deferred to the production-readiness close-out.
5. **Dashboard is unconditional** (no CDK context flag to skip it). Cost is negligible; single source of truth wins.
6. **Dashboard name** is `trip-tracker-${stackName}` so multiple deploys (dev/staging/prod) coexist without colliding.
7. **Deterministic widget order** — widgets always added in the same order so `cdk diff` is empty across deploys with no input changes.
8. **No re-deploy of `mcp-authorizer` source.** The `./lambdas/mcp-authorizer/` source-code asset is referenced by three `lambda.Function` instances (the bookings-mcp authorizer being deleted, the flights authorizer at `lib/flights-mcp-server.js:60`, and the hotels authorizer at `lib/hotels-mcp-server.js:50`); each Lambda is its own CloudWatch metric source. Only the bookings-mcp's OWN authorizer Lambda (`bookings-mcp-server-authorizer` in `lib/mcp-server.js:60`) goes away — the source asset stays put because flights + hotels still consume it.
9. **Authorizer Lambdas are in dashboard scope.** Each of the three surviving authorizer Lambdas (`flights-mcp-server-authorizer`, `hotels-mcp-server-authorizer`, `travel-agent-authorizer`) is surfaced in the same Lambda invocations / errors / duration widgets that hold the primary Lambdas. Rationale: an authorizer failure (HS256 verification crash, Cognito JWKS fetch failure, runtime OOM, cold-start timeout) takes the corresponding API offline, but API GW 4xx/5xx alone can't distinguish that case from a downstream Lambda failure. Excluding the authorizers would leave a known operational blind spot at the auth boundary. Cost of inclusion: three extra metric refs in three widgets; zero additional widgets; ~0 additional resource cost. This was Codex finding #6 in §0's second-pass table.

## 10. NOT building (explicit)

- AWS Budget alarm — separate close-out work item.
- Per-metric CloudWatch alarms (errors > 0, latency p99 > N, …) — same.
- SES bounce/complaint topic subscription — defers to a future production-readiness pass; the dashboard widget for SES is a placeholder.
- A separate dashboard for dev vs prod — single dashboard, multi-environment via stack-name suffix.
- Refactoring `lib/agent.js` beyond (a) removing the bookings-mcp wires and (b) adding `this.function = travelAgentFn;` + `this.api = agentApi;` at the end of the constructor for dashboard wiring. Keep the change minimal otherwise; do not reorganise the construct, do not rename existing locals, do not retighten the Bedrock IAM grant (tracked separately under ADR 0006 / ADR 0004 follow-up).
- Reorganising the constructs into subdirectories — would touch every file; out of scope.
- Adding metrics to the notifier — notifier emits Lambda invocations/errors/duration via the runtime, which is enough for v1. Custom EMF metrics for the notifier are a follow-up.

## 11. Test matrix (jest side)

The new `test/observability-dashboard.test.js` should pin these invariants:

### Group A — Construct loads + dashboard resource is created
- `test_A1_construct_loads_without_throwing`
- `test_A2_synthesises_exactly_one_dashboard_resource`
- `test_A3_dashboard_name_includes_stack_name_for_multi_env_safety`

### Group B — Namespace + metric-name constants
- `test_B1_NAMESPACE_constant_equals_TripTracker_Poller`
- `test_B2_metric_names_constant_lists_all_four_poller_emf_metrics`
- `test_B3_widget_count_equals_seven` (poller EMF + 3× Lambda + APIGW + SES + placeholder alarm row)

### Group C — Widget content fidelity (introspect the synthesised template)
- `test_C1_poller_widget_includes_watches_polled_metric_name`
- `test_C2_poller_widget_includes_watches_errored_metric_name`
- `test_C3_poller_widget_includes_bedrock_decisions_made_metric_name`
- `test_C4_poller_widget_includes_alerts_sent_metric_name`
- `test_C5_lambda_invocations_widget_metric_array_shape_per_function` — for each of the eight expected Lambdas (poller, notifier, agent, flights-mcp, hotels-mcp, flights-authorizer, hotels-authorizer, agent-authorizer), assert the invocations widget's `metrics` array contains an entry shaped `["AWS/Lambda", "Invocations", "FunctionName", <name>]`. Substring `body.includes(name)` is not sufficient (Codex finding #5).
- `test_C6_lambda_errors_widget_metric_array_shape_per_function` — same eight Lambdas, `"Errors"` metric.
- `test_C7_lambda_duration_widget_metric_array_shape_per_function` — same eight Lambdas, `"Duration"` metric with `p99` statistic.
- `test_C8_ses_widget_dimension_filters_to_configured_sender_identity` — assert the SES widget's `metrics` entry contains the dimension pair `"Source", <senderEmail>`.

### Group D — Determinism (the cdk-diff-stays-clean invariant)
- `test_D1_two_synth_passes_with_same_inputs_produce_identical_dashboard_body`

### Group E — Cross-language constant sync (Python side)
- new `lambdas/poller/tests/test_metrics_constants_sync.py` reads the JS file as text, regex-extracts the `NAMESPACE` literal + the `METRIC_NAMES` array, and asserts they match the Python module's exports. (Catches a future drift where someone updates one side and forgets the other.)

### Group F — Full-stack synth assertions with metric-shape parsing (catches integration shape, not just isolated constructs)

These tests exercise `StrandsAgentOnLambdaStack` end-to-end (not the dashboard construct in isolation) so a regression where a Lambda or API ref isn't actually reachable from the stack is caught. Codex's first-pass review called out that isolated-construct tests can pass while the stack itself wires undefined refs into the dashboard; Codex's second-pass review (finding #5) called out that substring `body.includes(name)` is insufficient because a widget label could include a name while the actual metric dimension is wrong. These tests parse the `DashboardBody` (which CDK emits as an `Fn::Join` fragment array) and walk the widget JSON.

Helper to share across F1–F3 (one helper function per test file):

```js
function getDashboardWidgets(template) {
    const resources = template.Resources || {};
    const dash = Object.entries(resources).find(([, r]) => r.Type === 'AWS::CloudWatch::Dashboard');
    if (!dash) throw new Error('no AWS::CloudWatch::Dashboard resource');
    const body = dash[1].Properties.DashboardBody;
    // DashboardBody is { "Fn::Join": ["", [fragment, ref, fragment, ...]] }
    // Concatenate fragments + resolve Ref/GetAtt placeholders to a marker the
    // assertion side can match (the function name string for a Lambda ref).
    const parts = body['Fn::Join'][1];
    const joined = parts.map(p => typeof p === 'string' ? p : '<<REF>>').join('');
    return JSON.parse(joined);  // { widgets: [...] }
}
```

- `test_F1_stack_synth_dashboard_widgets_assert_lambda_metric_dimensions` — synth the full stack, parse the dashboard widget JSON, find the three Lambda widgets (invocations, errors, duration). For each of the eight expected Lambdas (`trip-tracker-poller`, `trip-tracker-notifier`, `travel-agent-on-lambda`, `flights-mcp-server`, `hotels-mcp-server`, `flights-mcp-server-authorizer`, `hotels-mcp-server-authorizer`, `travel-agent-authorizer`), assert at least one entry in `widget.properties.metrics` matches `["AWS/Lambda", "Invocations"|"Errors"|"Duration", "FunctionName", <expected>]`. A missing or undefined function name fails the test.
- `test_F2_stack_synth_dashboard_widgets_assert_apigateway_metric_dimensions` — same parsed widgets, find the API GW 4xx/5xx widget. For each of `flights-mcp-api`, `hotels-mcp-api`, `travel-agent-api`, assert at least one entry in `widget.properties.metrics` matches `["AWS/ApiGateway", <metric>, "ApiName", <expected>]`. Substring containment is not sufficient.
- `test_F3_stack_synth_dashboard_widgets_contain_no_undefined_dimension` — walk every widget's `metrics` array; assert no entry contains the literal string `"undefined"` in any dimension-value slot. Catches a passed-undefined Lambda or API ref that CloudFormation accepts as a string.

The metric-shape parsing approach matches the AWS-documented `DashboardBody` JSON schema; substring-based gates are kept as cheap pre-filters in the node-eval fallback (Gate 5) but no longer the binding assertion.

## 12. Validation gates

### Gate 1 — Notifier suite (regression check)
```
"C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/python.exe" \
  -m pytest C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/notifier/tests/ -q
```
EXPECT: 126 passing.

### Gate 2 — Poller + evals suite (regression check + cross-language sync)
```
"C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/python.exe" \
  -m pytest C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/poller/tests/ \
            C:/Users/isabe/Downloads/trip-tracker-agent/evals/tests/ -q
```
EXPECT: 307 + 1 (the new constants-sync test) = 308 passing.

### Gate 3 — Jest suite
```
cd C:/Users/isabe/Downloads/trip-tracker-agent && npx jest test/
```
EXPECT: 25 prior + ~15 new dashboard tests = ~40 passing. (Pre-existing `node:test`-using files in `lambdas/flights-mcp/tests/` continue to error at collection — unrelated.)

### Gate 4 — Comment-cleanliness ripgrep
```
rg -n --no-heading 'slice[ -_]?\d|\bT[1-9]\b|\bTask [1-9]\b|Checkpoint [A-Z]\b' \
  C:/Users/isabe/Downloads/trip-tracker-agent/lib/observability-dashboard.js \
  C:/Users/isabe/Downloads/trip-tracker-agent/test/observability-dashboard.test.js
rg -n --no-heading -w 'basically|simply|obviously|essentially|merely' \
  C:/Users/isabe/Downloads/trip-tracker-agent/lib/observability-dashboard.js \
  C:/Users/isabe/Downloads/trip-tracker-agent/test/observability-dashboard.test.js
```
EXPECT: zero matches in both.

### Gate 5 — CDK synth or full-stack node-eval fallback with metric-shape parsing

```
cd C:/Users/isabe/Downloads/trip-tracker-agent && npx cdk synth --quiet
```

If this hits the AgentConstruct DependenciesLayer Docker bundling issue (documented in earlier Ralph state), fall back to a node-eval that **instantiates `StrandsAgentOnLambdaStack` itself** (not isolated sub-constructs) with the same context values used for deploy. The fallback parses the synthesised `DashboardBody` JSON and walks each widget's `metrics` array, asserting metric-dimension shape — substring `body.includes(name)` is kept only as a cheap pre-filter (Codex finding #5).

```
DUFFEL_API_KEY=stub LITEAPI_API_KEY=stub \
  node -e "
    const { App } = require('aws-cdk-lib');
    const { StrandsAgentOnLambdaStack } = require('./lib/strands-agent-on-lambda-stack');
    const app = new App({ context: {
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
    const dash = Object.values(resources).find(r => r.Type === 'AWS::CloudWatch::Dashboard');
    if (!dash) { console.error('no dashboard resource'); process.exit(1); }

    // DashboardBody is { 'Fn::Join': ['', [fragment, { Ref: ... } | { 'Fn::GetAtt': ... }, fragment, ...]] }.
    // Concatenate string fragments and replace ref/getatt placeholders with the logical id (a unique
    // synthetic marker we can recognise later via the template's own Resources map).
    const fnJoin = dash.Properties.DashboardBody['Fn::Join'];
    if (!fnJoin) { console.error('DashboardBody is not an Fn::Join'); process.exit(1); }
    const parts = fnJoin[1];

    // Build logicalId -> functionName / apiName lookup so substituted refs come out as their resource name.
    const resourceNames = {};
    for (const [id, r] of Object.entries(resources)) {
      if (r.Type === 'AWS::Lambda::Function' && r.Properties && r.Properties.FunctionName) {
        resourceNames[id] = r.Properties.FunctionName;
      } else if (r.Type === 'AWS::ApiGateway::RestApi' && r.Properties && r.Properties.Name) {
        resourceNames[id] = r.Properties.Name;
      }
    }
    const resolvedParts = parts.map(p => {
      if (typeof p === 'string') return p;
      if (p.Ref && resourceNames[p.Ref]) return resourceNames[p.Ref];
      if (p['Fn::GetAtt'] && resourceNames[p['Fn::GetAtt'][0]]) return resourceNames[p['Fn::GetAtt'][0]];
      return '<<UNRESOLVED-REF>>';
    });
    const joined = resolvedParts.join('');
    if (joined.includes('<<UNRESOLVED-REF>>')) {
      console.error('dashboard references a Lambda or API resource whose name is not in the template');
      process.exit(1);
    }
    if (joined.includes('undefined')) {
      console.error('dashboard body contains literal undefined');
      process.exit(1);
    }

    let dashJson;
    try { dashJson = JSON.parse(joined); }
    catch (e) { console.error('DashboardBody is not valid JSON after Fn::Join resolution:', e.message); process.exit(1); }

    // Walk every widget's metrics array. Each metric entry is a tuple:
    //   [namespace, metricName, ...dimensionPairs, optionalRenderingProperties]
    // e.g. ['AWS/Lambda', 'Invocations', 'FunctionName', 'trip-tracker-poller', { stat: 'Sum' }]
    const metricEntries = [];
    for (const w of (dashJson.widgets || [])) {
      const ms = (w.properties && w.properties.metrics) || [];
      for (const m of ms) metricEntries.push(m);
    }

    function hasMetricDimension(namespace, dimKey, dimValue) {
      return metricEntries.some(m => {
        if (!Array.isArray(m) || m[0] !== namespace) return false;
        for (let i = 2; i < m.length - 1; i++) {
          if (m[i] === dimKey && m[i + 1] === dimValue) return true;
        }
        return false;
      });
    }

    const requiredLambdas = [
      'trip-tracker-poller',
      'trip-tracker-notifier',
      'travel-agent-on-lambda',
      'flights-mcp-server',
      'hotels-mcp-server',
      'flights-mcp-server-authorizer',
      'hotels-mcp-server-authorizer',
      'travel-agent-authorizer',
    ];
    const requiredApis = [
      'flights-mcp-api',
      'hotels-mcp-api',
      'travel-agent-api',
    ];

    const missingLambdas = requiredLambdas.filter(n => !hasMetricDimension('AWS/Lambda', 'FunctionName', n));
    const missingApis = requiredApis.filter(n => !hasMetricDimension('AWS/ApiGateway', 'ApiName', n));
    if (missingLambdas.length || missingApis.length) {
      console.error('dashboard metric-dimension assertions failed:', { missingLambdas, missingApis });
      process.exit(1);
    }
    console.log('ok: dashboard graphs', requiredLambdas.length, 'Lambdas and', requiredApis.length, 'APIs with correct metric dimensions');
  "
```

EXPECT: `cdk synth` exit 0 with a `Resources.<id>.Type === "AWS::CloudWatch::Dashboard"` resource whose `DashboardBody` contains the expected metric-dimension shapes, OR the fallback prints `ok: dashboard graphs 8 Lambdas and 3 APIs with correct metric dimensions`.

Three failure modes the parser explicitly catches: (a) a missing Lambda or API ref (`missingLambdas`/`missingApis` non-empty), (b) a literal `"undefined"` anywhere in the resolved body, (c) a `Ref`/`GetAtt` to a logical id whose resource doesn't exist in the template (`<<UNRESOLVED-REF>>` sentinel). Substring containment is insufficient because a widget title or annotation can hold a name string while the metric-dimension array is wrong — Codex finding #5.

### Gate 6 — git grep cleanliness (post-bookings-removal)
```
cd C:/Users/isabe/Downloads/trip-tracker-agent && git grep -i 'bookings' -- \
  ':!docs/threat-model.md' \
  ':!docs/adr/' \
  ':!tasks/'
```
EXPECT: zero hits in `lib/`, `lambdas/`, source files, and the production-readiness companion spec. Hits in `docs/threat-model.md` change-log entries, `docs/adr/`, and `tasks/` planning artifacts are allowed (durable historical context).

### Gate 7 — Cross-language constant sync
The new `test_metrics_constants_sync.py` (part of Gate 2) is the gate. If it passes, the JS and Python sides agree on namespace + metric names.

## 13. Constraints inherited

- **Zero `slice X` / `T#` / `Task N` / `Checkpoint A-Z`** references in any new file (global CLAUDE.md rule at `~/.claude/CLAUDE.md`).
- **Zero nonsense filler** in any new file (`basically`, `simply`, `obviously`, `essentially`, `merely`, `kind of`).
- **Multi-reviewer gate** at the end: code-reviewer five-axis → security-auditor → test-engineer → code-reviewer comments-focused. Sequential per memory `feedback_subagents_sequential`.
- CDK construct style mirrors `lib/poller-server.js` + `lib/notifier-server.js` (JSDoc header with intent + design choices, synth-time validation, `this.function` exposure pattern).
- All Python tests run via `.venv-tests/Scripts/python.exe`.
- ADRs allowed to mention dates in Status lines (durable historical context); threat-model markdown anchors `[2]`–`[7]` allowed (durable).

## 14. Step-by-step

Execute top-to-bottom. Each task is atomic and verifiable.

### Task 1: VERIFY ADR 0002 + design-spec bookings references

- **ACTION**: Confirm exactly where `bookings` appears in `docs/adr/0002-fixture-replay-mode.md` and `docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md`. Both files have exactly one line each (line 53 in both).
- **VALIDATE**: `grep -nc 'bookings' docs/adr/0002-fixture-replay-mode.md docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md` returns `1 + 1 = 2`.

### Task 2: REMOVE the `bookings-mcp` asset directory + the construct file

- **ACTION**: `git rm -r lambdas/bookings-mcp/` and `git rm lib/mcp-server.js`.
- **VALIDATE**: `ls lambdas/bookings-mcp/` returns "No such file or directory"; `test -f lib/mcp-server.js` returns false.
- **GOTCHA**: do NOT delete `lambdas/mcp-authorizer/` — it's shared with `flights-mcp` + `hotels-mcp`. Verify by `git grep -l 'mcp-authorizer' lib/` showing `flights-mcp-server.js` + `hotels-mcp-server.js` references.

### Task 3: UPDATE the stack to drop the McpServerConstruct wiring

- **ACTION**: edit `lib/strands-agent-on-lambda-stack.js`:
  - delete line 3 (`const McpServerConstruct = require('./mcp-server');`)
  - delete lines 30-35 (the `const { mcpEndpoint } = new McpServerConstruct(...)` block)
  - delete line 72 (`mcpEndpoint,` from the AgentConstruct props object)
- **MIRROR**: see how `flights-mcp-server` + `hotels-mcp-server` are wired at lines 45-62 for the surviving-construct pattern.
- **VALIDATE**: `node -e "require('./lib/strands-agent-on-lambda-stack');"` exits 0.

### Task 4: UPDATE `lib/agent.js` to drop the MCP_ENDPOINT wiring

- **ACTION**: edit `lib/agent.js`:
  - delete the `MCP_ENDPOINT` env-var injection at line 100
  - delete the `MCP_ENDPOINT` doc-comment lines 80-81
  - delete the `(tools like car/hotel booking)` parenthetical at line 77 — replace with `(tools like flight + hotel search served by the MCP Lambdas)`
- **VALIDATE**: `node -e "require('./lib/agent');"` exits 0. The poller stack-wiring node-eval from Task 3 also still works.

### Task 5: UPDATE `lambdas/travel-agent/mcp_client_manager.py`

- **ACTION**: edit `lambdas/travel-agent/mcp_client_manager.py`:
  - rewrite the module docstring (lines 1-30) so the MCP-split rationale describes flights + hotels as the canonical examples (not bookings-mcp).
  - delete the `("bookings", os.getenv("MCP_ENDPOINT"))` entry at line 50.
  - rewrite the surrounding comment at lines 44-48 to drop "The `bookings` endpoint is legacy and ...".
- **GOTCHA**: do NOT change the endpoint-loop at line 141-144 — it already correctly tolerates empty URLs via `if not url: continue`, and that tolerance is now load-bearing for any future endpoint that's intentionally disabled.
- **VALIDATE**: existing 174 poller tests still pass (the agent's mcp_client_manager doesn't run in the poller test suite but the test_handler_skeleton tests transitively load common code). Gate 2 covers this.

### Task 6a: UPDATE `lib/flights-mcp-server.js` + `lib/hotels-mcp-server.js` — remove the constructor return-object anti-pattern

- **WHY**: Both constructors currently end with `return { ...Endpoint };`. Per ES spec, an explicit object return from a class constructor replaces the constructed instance — so the stack code that does `const { fooEndpoint } = new FooConstruct(...)` captures only that returned object, never the construct instance, and any `this.foo = ...` assignment is silently unreachable. This was Codex finding #2.
- **ACTION (flights)**: edit `lib/flights-mcp-server.js`:
  - rewrite the line-11 "Differences from the original McpServerConstruct (bookings-mcp)" comment so it stands on its own without naming a now-deleted construct. Keep the substance; drop the comparative framing.
  - **delete the `return { flightsMcpEndpoint };` at line 102.**
  - in its place, expose four properties on the instance: `this.endpoint = flightsMcpEndpoint;`, `this.function = flightsMcpFn;`, `this.api = flightsApi;`, `this.authorizerFunction = flightsAuthorizerFn;`.
- **ACTION (hotels)**: edit `lib/hotels-mcp-server.js`:
  - **delete the `return { hotelsMcpEndpoint };` at line 87.**
  - expose `this.endpoint = hotelsMcpEndpoint;`, `this.function = hotelsMcpFn;`, `this.api = hotelsApi;`, `this.authorizerFunction = hotelsAuthorizerFn;`.
- **VALIDATE (verifies the anti-pattern is gone, not just that the file parses)**:
  ```
  node -e "
    const cdk = require('aws-cdk-lib');
    const F = require('./lib/flights-mcp-server');
    const H = require('./lib/hotels-mcp-server');
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'T');
    const f = new F(stack, 'F', { fnArchitecture: require('aws-cdk-lib/aws-lambda').Architecture.ARM_64, jwtSignatureSecret: 'x', mcpMode: 'fixture' });
    const h = new H(stack, 'H', { fnArchitecture: require('aws-cdk-lib/aws-lambda').Architecture.ARM_64, jwtSignatureSecret: 'x', mcpMode: 'fixture' });
    if (!f.function || !f.api || !f.endpoint || !f.authorizerFunction) {
      console.error('flights: missing one of function/api/endpoint/authorizerFunction', {
        hasFn: !!f.function, hasApi: !!f.api, hasEndpoint: !!f.endpoint, hasAuthorizer: !!f.authorizerFunction,
      });
      process.exit(1);
    }
    if (!h.function || !h.api || !h.endpoint || !h.authorizerFunction) {
      console.error('hotels: missing one of function/api/endpoint/authorizerFunction');
      process.exit(1);
    }
    console.log('ok: flights + hotels expose function/api/endpoint/authorizerFunction');
  "
  ```
  EXPECT: `ok: flights + hotels expose function/api/endpoint/authorizerFunction`. If this prints "missing one of …", the `return { ... }` was not actually removed or `this.authorizerFunction = ...` is absent — fix and re-run.

### Task 6b: UPDATE `lib/poller-server.js` — rename `this.pollerFn` → `this.function`

- **WHY**: All five constructs should expose the Lambda ref under a uniform property so the dashboard wiring is `<construct>.function` across the board. The notifier already uses `this.function` (line 132). The poller currently uses `this.pollerFn` (line 188). Stack code at lines 96-104 doesn't read either, so the rename has no external consumer.
- **ACTION**: edit `lib/poller-server.js` line 188:
  - change `this.pollerFn = pollerFn;` to `this.function = pollerFn;`
  - leave `this.scheduleRule = rule;` (line 189) alone.
- **VALIDATE**:
  ```
  node -e "
    const cdk = require('aws-cdk-lib');
    const P = require('./lib/poller-server');
    const ddb = require('aws-cdk-lib/aws-dynamodb');
    const app = new cdk.App({ context: { bedrockModelId: 'claude-haiku-4-5-20251001', bedrockMode: 'stub' } });
    const stack = new cdk.Stack(app, 'T');
    const watches = new ddb.Table(stack, 'W', { partitionKey: { name: 'pk', type: ddb.AttributeType.STRING } });
    const fares = new ddb.Table(stack, 'F', { partitionKey: { name: 'pk', type: ddb.AttributeType.STRING } });
    const p = new P(stack, 'P', { fnArchitecture: require('aws-cdk-lib/aws-lambda').Architecture.ARM_64, watchesTable: watches, fareHistoryTable: fares, jwtSignatureSecret: 'x', flightsMcpEndpoint: 'https://f', hotelsMcpEndpoint: 'https://h' });
    if (!p.function) { console.error('poller missing this.function'); process.exit(1); }
    if (p.pollerFn) { console.error('poller still exposes this.pollerFn — old property should be gone'); process.exit(1); }
    console.log('ok: poller exposes this.function');
  "
  ```
  EXPECT: `ok: poller exposes this.function`.

### Task 6c: UPDATE `lib/agent.js` — expose `this.function`, `this.api`, `this.authorizerFunction`

- **WHY**: AgentConstruct creates `travelAgentFn` (line 87), `agentApi` (line 151), and `agentAuthorizerFn` (line 169) as locals and exposes none. The dashboard needs all three refs. Codex finding #3 (agent + api) + finding #6 (authorizer).
- **ACTION**: edit `lib/agent.js`:
  - at the end of the constructor (after the `AgentEndpointUrl` `CfnOutput` at lines 199-201, before the closing `}` at line 202), add three lines:
    ```js
    this.function           = travelAgentFn;
    this.api                = agentApi;
    this.authorizerFunction = agentAuthorizerFn;
    ```
  - keep all other behaviour unchanged. Do not retighten the Bedrock IAM grant (out of scope per §10), do not rename existing locals, do not reorganise the constructor body.
- **VALIDATE**: Gate 5's full-stack fallback asserts that the dashboard's metric-dimension array contains `["AWS/Lambda", "Invocations", "FunctionName", "travel-agent-authorizer"]` and `["AWS/ApiGateway", ..., "ApiName", "travel-agent-api"]`. A missing exposure surfaces as a missing-dimension assertion failure with the specific name in the error message.

### Task 7: UPDATE ADR 0002 + design spec

- **ACTION**: edit `docs/adr/0002-fixture-replay-mode.md` line 53. Current text: `The stub bookings-mcp (replaced by flights-mcp + hotels-mcp)`. New text: `The flights-mcp and hotels-mcp constructs follow this pattern`. Verify the surrounding bullet still flows.
- **ACTION**: edit `docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md` line 53 with the same tightening.
- **VALIDATE**: `grep -nc 'bookings' docs/adr/0002-fixture-replay-mode.md docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md` returns `0 + 0 = 0`.

### Task 8: CREATE `lib/observability-dashboard.js`

- **ACTION**: CREATE the construct file.
- **STRUCTURE**:
  - `require` cloudwatch + cdk-lib at the top.
  - `const POLLER_METRIC_NAMESPACE = "TripTracker/Poller";`
  - `const POLLER_METRIC_NAMES = ["watches_polled", "watches_errored", "bedrock_decisions_made", "alerts_sent"];`
  - `// keep in sync with lambdas/poller/metrics.py:NAMESPACE + the metric constants` comment.
  - `class ObservabilityDashboardConstruct extends Construct { constructor(scope, id, props) { ... } }`.
  - Props: `{ pollerFunction, notifierFunction, agentFunction, flightsMcpFunction, hotelsMcpFunction, flightsAuthorizerFunction, hotelsAuthorizerFunction, agentAuthorizerFunction, flightsMcpApi, hotelsMcpApi, agentApi, notifierSenderEmail }`. Total: eight Lambda function refs, three API Gateway refs, one sender-email string.
  - Build the seven widgets in deterministic order; pass each to `dashboard.addWidgets(...)` in fixed sequence. The three Lambda widgets (invocations / errors / duration) each accept all eight Lambdas; the order of metric lines within a widget must also be deterministic so `cdk diff` is empty across re-deploys.
- **MIRROR**: JSDoc header style + design-choices block from `lib/poller-server.js:12-40`.
- **GOTCHA**: `cloudwatch.Metric.fromMetricName` does NOT exist in v2.196 — use `new cloudwatch.Metric({ namespace, metricName, statistic, period })` directly. Per [aws-cdk-lib v2.196.0 docs](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_cloudwatch.Metric.html).
- **GOTCHA**: Dashboard name must include `Stack.of(this).stackName` so dev + prod stacks don't collide.
- **VALIDATE**: `node -e "const D = require('./lib/observability-dashboard'); console.log(typeof D);"` prints `function`.

### Task 9: WIRE the dashboard into the stack + CREATE the two test files

- **ACTION (stack rewiring)**: edit `lib/strands-agent-on-lambda-stack.js`:
  - **Switch flights / hotels / agent / poller / notifier from destructuring-the-return-value to capturing the construct instance.** After Task 6a, flights and hotels no longer return an object — destructuring would now receive `undefined`. Replace:
    ```js
    const { flightsMcpEndpoint } = new FlightsMcpServerConstruct(this, 'FlightsMcpServerConstruct', { ... });
    const { hotelsMcpEndpoint }  = new HotelsMcpServerConstruct(this, 'HotelsMcpServerConstruct', { ... });
    new AgentConstruct(this, 'AgentConstruct', { ..., flightsMcpEndpoint, hotelsMcpEndpoint, ... });
    new PollerServerConstruct(this, 'PollerServerConstruct', { ..., flightsMcpEndpoint, hotelsMcpEndpoint, notifierFunction: notifierServer.function });
    ```
    with:
    ```js
    const flightsServer = new FlightsMcpServerConstruct(this, 'FlightsMcpServerConstruct', { ... });
    const hotelsServer  = new HotelsMcpServerConstruct(this, 'HotelsMcpServerConstruct', { ... });
    const agentConstruct = new AgentConstruct(this, 'AgentConstruct', { ..., flightsMcpEndpoint: flightsServer.endpoint, hotelsMcpEndpoint: hotelsServer.endpoint, ... });
    const pollerServer = new PollerServerConstruct(this, 'PollerServerConstruct', { ..., flightsMcpEndpoint: flightsServer.endpoint, hotelsMcpEndpoint: hotelsServer.endpoint, notifierFunction: notifierServer.function });
    ```
    (Notifier already returns an instance with `this.function`, no change to that line.)
  - **Import + instantiate the dashboard last**, after all other constructs exist:
    ```js
    const ObservabilityDashboardConstruct = require('./observability-dashboard');
    // ... after notifierServer + pollerServer + agentConstruct are constructed ...
    new ObservabilityDashboardConstruct(this, 'ObservabilityDashboard', {
      pollerFunction:            pollerServer.function,
      notifierFunction:          notifierServer.function,
      agentFunction:             agentConstruct.function,
      flightsMcpFunction:        flightsServer.function,
      hotelsMcpFunction:         hotelsServer.function,
      flightsAuthorizerFunction: flightsServer.authorizerFunction,
      hotelsAuthorizerFunction:  hotelsServer.authorizerFunction,
      agentAuthorizerFunction:   agentConstruct.authorizerFunction,
      flightsMcpApi:             flightsServer.api,
      hotelsMcpApi:              hotelsServer.api,
      agentApi:                  agentConstruct.api,
      notifierSenderEmail:       this.node.tryGetContext('notifierSenderEmail'),
    });
    ```
- **GOTCHA**: order matters — the dashboard construct must be instantiated after all eight Lambdas / three APIs exist, otherwise the props will be undefined and Gate 5 will fail with `missingLambdas: [...]`, `missingApis: [...]`, or `dashboard body contains literal undefined`.
- **ACTION**: CREATE `test/observability-dashboard.test.js` per the test matrix in §11 (Groups A–D + the new Group F metric-shape-parsing assertions). For Groups A–D follow the pattern in `test/notifier-server.test.js` (App + Stack + new construct, CloudFormation-template introspection). For Group F, instantiate `StrandsAgentOnLambdaStack` itself, parse the synthesised `DashboardBody` Fn::Join into widget JSON using the helper described in §11, and assert metric-dimension shape per `hasMetricDimension(namespace, dimKey, dimValue)`.
- **ACTION**: CREATE `lambdas/poller/tests/test_metrics_constants_sync.py` that reads `lib/observability-dashboard.js` as text, regex-extracts `POLLER_METRIC_NAMESPACE` and `POLLER_METRIC_NAMES`, and asserts they match the Python module's exports.
- **VALIDATE**: All 7 gates pass.

## 15. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| CDK synth blocks on Docker (AgentConstruct DependenciesLayer) | MED | LOW | Gate 5 falls back to a node-eval direct-construct synth check; documented in the gate. |
| Removing `MCP_ENDPOINT` env var crashes the agent at deploy | LOW | MED | `mcp_client_manager.py:142-144` already does `if not url: continue` — verified by reading. Once the env var is gone, the entry is just dropped from the tuple list entirely, not left as an empty string. Existing agent tests would catch a regression. |
| Dashboard construct depends on a Lambda ref that isn't exposed (Codex finding #1, #2, #3) | HIGH | MED | Tasks 6a (flights/hotels rework, removing the return-object anti-pattern), 6b (poller rename), and 6c (agent exposure) explicitly bring every construct to the `this.function` / `this.api` convention before Task 9 wires the dashboard. Task 9's stack rewiring captures construct instances instead of destructuring. Gate 5's full-stack synth assertion fails loud if any of the eight expected names is missing from the dashboard body or if the body contains a literal `undefined`. |
| Stack destructuring stays in place after Task 6a removes the return-object pattern (the silent-undefined trap Codex caught) | MED | HIGH | Task 9 explicitly rewires the stack from destructuring (`const { flightsMcpEndpoint } = new ...`) to instance capture (`const flightsServer = new ...; const flightsMcpEndpoint = flightsServer.endpoint;`). Gate 5 fails if `flightsMcpEndpoint` is undefined at the env-var injection site (the agent or poller Lambda would crash on cold start). |
| Jest tests for the dashboard hit aws-cdk-lib version drift | LOW | LOW | All assertions use stable v2 APIs (`Dashboard`, `GraphWidget`, `Metric`); no use of `Metric.fromMetricName` or other v3-pending shapes. |
| Future drift between JS and Python metric names | MED | MED | Gate 7's cross-language sync test catches drift on the next CI run; refusing to merge until updated. |
| Bookings removal breaks a test I forgot about | LOW | LOW | Gate 6's `git grep -i 'bookings'` audits the entire post-cleanup tree; any leftover reference outside the allowlisted dirs trips the gate. |
| Validation gate falsely passes because a widget title contains a name string but the metric dimension is wrong (Codex finding #5) | MED | HIGH | Gate 5 and Group F tests parse `DashboardBody` Fn::Join, resolve refs to resource names via the template's own Resources map, JSON.parse the result, and assert each metric entry has shape `[namespace, metricName, dimKey, dimValue]` rather than relying on substring presence. A widget that holds the name only in a title fails the gate. |
| Authorizer Lambda crashes silently because the dashboard didn't graph it (Codex finding #6) | LOW | HIGH | Locked Decision #9 + Tasks 6a / 6c + Gate 5's metric-dimension assertion explicitly require all three authorizer Lambdas (`flights-mcp-server-authorizer`, `hotels-mcp-server-authorizer`, `travel-agent-authorizer`) to appear in the Lambda widgets. A construct that fails to expose `this.authorizerFunction` surfaces as a missing-dimension assertion failure naming the specific Lambda. |

---

## What "done" looks like

- 11 files deleted (the entire `lambdas/bookings-mcp/` dir + `lib/mcp-server.js`).
- 6 files updated (stack, agent.js, poller-server.js, mcp_client_manager.py, flights-mcp-server.js, hotels-mcp-server.js). Construct-property exposures: poller `this.function`; flights `this.endpoint/.function/.api/.authorizerFunction`; hotels same shape; agent `this.function/.api/.authorizerFunction`.
- 2 doc files updated with one-line tightenings each (ADR 0002, design spec).
- 1 new CDK construct file (`lib/observability-dashboard.js`) accepting 8 Lambda function refs + 3 API Gateway refs + 1 sender-email string.
- 1 new jest test file (`test/observability-dashboard.test.js`) with ~21 tests (Groups A–D's 11 plus Group F's 3 metric-shape-parsing assertions; the C-group's per-Lambda metric-shape tests cover 8 Lambdas across 3 widgets via parameterised cases).
- 1 new Python test file (`lambdas/poller/tests/test_metrics_constants_sync.py`) with cross-language sync gate.
- All 7 validation gates green, including Gate 5's full-stack synth assertion that the dashboard body's metric-dimension arrays (parsed JSON, not substrings) cover all 8 Lambdas + 3 APIs and contain no literal `"undefined"` or unresolved Ref placeholders.
- Working tree changes confined to the above paths.
- Ready to commit as `remove bookings-mcp scaffold + add observability dashboard construct`.

## Confidence

**9/10** — two well-scoped, independent work items both fitting patterns already proven in the codebase. Post-first-Codex-review revisions (§0 first table) make the construct-property exposure path explicit and verifiable at each step (Tasks 6a / 6b / 6c each have a `node -e` validate block). Post-second-Codex-review revisions (§0 second table) replace substring-based gates with metric-dimension-shape parsing and add the three authorizer Lambdas to dashboard scope, closing the two remaining silent-pass paths. The known unknown remains the Docker-blocked `cdk synth`; Gate 5's full-stack node-eval fallback covers the highest-risk failure modes (undefined dashboard props OR wrong-metric-dimension widgets would both fail the gate, not slip through it).
