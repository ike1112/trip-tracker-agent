# PRP: AWS Budget alarm CDK construct

**Confidence:** **9/10** for one-pass execution. Single new L1-resource construct with an exact in-repo precedent (`NotifierServerConstruct` — same `scope.node.tryGetContext` + `EMAIL_PATTERN` synth-validation + named-export shape) and an exact test precedent (`test/notifier-server.test.js`). The one sharp edge (the CloudFormation type of `budgetLimit.amount`) was found by the adversarial pass and inverted: it is the **number `10`**, not a string — §0 #1, verified by direct synth, pinned as numbers throughout. The other deliberate call is dropping the `sesMode`-style allowlist (no mode flag — Budgets are free/deploy-safe). Both are locked decisions below, not open questions.

---

## 0. Pre-implementation review response (Codex adversarial pass + direct verification, 2026-05-16)

Codex (read the live repo + the installed `aws-cdk-lib@2.196.0` surface) found 4 defects. The HIGH was verified by **directly synthesising a `CfnBudget`** (stronger than a re-read); #2 is a known environment fact; #3/#4 were grep-verified against the cited lines. All 4 are true and the body below is revised. A separate independent-reviewer subagent was deliberately skipped here (unlike the secrets PRP): every finding is black-and-white and directly verifiable, not a judgement call, and this is a LOW-complexity 4-task PRP — proportionate verification.

| # | Sev | Finding (verified) | Resolution | Sections |
|---|-----|--------------------|------------|----------|
| 1 | **HIGH** | The "gotcha" was **backwards**. `CfnBudget.SpendProperty.amount` is typed `number` (`node_modules/aws-cdk-lib/aws-budgets/lib/budgets.generated.d.ts:297`) and CDK's `numberToCloudFormation` is identity. Direct synth confirms `BudgetLimit.Amount` is the **number `10`** (`typeof === 'number'`), and `Threshold` is the **number `80`/`100`**, NOT strings. The PRP asserted string `'10'` in §6/§11/Gate 5/Task 3 — every Group B test + Gate 5 would have failed. | Assert **number** `10` and number thresholds everywhere. The "string serialisation" gotcha is deleted and inverted into an explicit "Amount/Threshold are numbers — do NOT quote them" note. | §1; §6; §11 B2/B3; §12 Gate 5; §14 Task 3; §15 |
| 2 | MED | Gate 5 used Bash env-assignment (`DUFFEL_API_KEY=stub … node -e`); the documented environment is PowerShell, and the existing full-stack jest tests set env in JS (`test/agent-bedrock-iam.test.js:15-18`) precisely to be shell-agnostic. | Gate 5 sets `process.env.DUFFEL_API_KEY/LITEAPI_API_KEY` **inside** the `node -e` before requiring the stack — no shell-specific env syntax. | §12 Gate 5 |
| 3 | LOW | Task 4 only flipped boundary [6]. Other budget refs would be left stale/contradictory: `docs/threat-model.md:188` "(planned, slice 9)", `:197` "AWS Budget alarm at $10/mo **with SNS topic email subscription** (slice 9)" — SNS is explicitly NOT built (§10) AND `slice 9` is a roadmap-label rule violation; `:243` "(deferred)". | Task 4 updates **all three** (188/197/243): drop "planned"/"deferred"/"slice 9"/"SNS topic", make them backward references to "AWS Budgets email subscriber (design-spec §300; commit <sha>)". | §14 Task 4 |
| 4 | LOW | Companion §5 line 205 says "AWS Budget alarm **deployed**". This PRP builds the CDK construct + tests but deploys nothing; rewording that sub-clause to "built" makes the checklist semantically inconsistent (and the whole `[ ]` line also still has CI/README/Loom/ADR-0007 unbuilt). | Task 4 does **NOT** touch companion §5. The line stays `[ ]` and "deployed" stays accurate-as-pending until a real deploy + the other sub-items land. | §14 Task 4 |

Net: no logic/architecture change — the construct shape is unaffected. The corrections are: assert numbers not strings, env-agnostic Gate 5, broader + rule-compliant threat-model edit, and do not touch the companion checklist. Confidence holds at 9/10 (the one sharp edge was found and inverted before implementation).

---

## 1. Summary

The last code item on the production-readiness launch checklist (companion spec §5 slice-9; design-spec §300: *"Mandatory: AWS Budget alarm at $10/month with email notification. Cheap insurance against a runaway loop."*). Add `lib/budget-alarm.js` — a `BudgetAlarmConstruct` that creates one account-level `AWS::Budgets::Budget` (COST, $10/month) with two threshold notifications (80% ACTUAL, 100% FORECASTED), each emailing a context-supplied address. Wire it into the stack. Pin the contract with `test/budget-alarm.test.js` using the notifier construct's synth-time-validation test pattern.

## 2. Problem statement

The poller runs Bedrock + two external APIs on an EventBridge cron. A misconfigured cadence or an alert loop could run up an AWS bill with no out-of-band signal — the CloudWatch dashboard (slice 8) shows operational metrics but nothing watches *spend*. Design-spec §300 makes a $10/month Budget alarm with email notification a **mandatory** launch gate. It does not exist yet (`git grep -i budget -- lib/` returns nothing). Until it lands, the checklist line `[ ] (slice 9) … AWS Budget alarm deployed` cannot be ticked and no real deploy is cost-safe.

## 3. Solution shape

A new CDK construct `lib/budget-alarm.js`, mirroring `lib/notifier-server.js`'s exact conventions:

1. **Email source.** `scope.node.tryGetContext('budgetAlarmEmail') ?? scope.node.tryGetContext('notifierRecipientEmail')`. The notifier recipient is already the human who acts on trip-tracker signals and is email-regex-validated wherever the notifier is built; reusing it as the default keeps one address to maintain, with `budgetAlarmEmail` as an explicit override.
2. **Synth-time validation.** Copy `NotifierServerConstruct`'s `EMAIL_PATTERN` (anchored, rejects bare domains / CRLF / leading-trailing-consecutive dots) and fail loud at synth: `if (!email || !EMAIL_PATTERN.test(email)) throw new Error(...)`. The error string names `budgetAlarmEmail/notifierRecipientEmail` so the operator knows which context to set.
3. **The budget.** One `aws-cdk-lib/aws-budgets` `CfnBudget`:
   - `budget: { budgetType: 'COST', timeUnit: 'MONTHLY', budgetLimit: { amount: 10, unit: 'USD' }, budgetName: 'trip-tracker-monthly-cost' }`
   - `notificationsWithSubscribers`: two entries —
     - `{ notification: { notificationType: 'ACTUAL', comparisonOperator: 'GREATER_THAN', threshold: 80, thresholdType: 'PERCENTAGE' }, subscribers: [{ subscriptionType: 'EMAIL', address: email }] }`
     - same with `notificationType: 'FORECASTED', threshold: 100`.
4. **No mode flag.** Unlike the notifier's `sesMode` (which gates a real vs stub SES send), an AWS Budget is free to create and harmless to deploy, and synth/tests only assert the template. There is no live/stub distinction to make, so the `ALLOWED_SES_MODES`-style allowlist is deliberately NOT mirrored (locked decision §9 #2). Only the email-regex validation transfers.
5. **No props.** The construct reads its email from context directly (as the notifier reads its emails from context). It has no dependency on any table, Lambda, or other construct, so the stack instantiates it as `new BudgetAlarmConstruct(this, 'BudgetAlarmConstruct')` with no props object.
6. **Named export.** `module.exports = { BudgetAlarmConstruct };` — matches `notifier-server.js` (NOT the default-export shape `agent.js`/`poller-server.js` use; the stack must destructure-import accordingly).

## 4. Metadata

| Field | Value |
|---|---|
| Type | NEW_CAPABILITY (production-readiness close-out) |
| Complexity | LOW |
| Systems Affected | 1 new CDK construct + 1 stack wiring line + 1 new jest test file |
| New deps | None (`aws-cdk-lib@2.196.0` ships `aws-budgets.CfnBudget` — verified `typeof === 'function'`) |
| Estimated atomic tasks | 4 |

## 5. UX / operator-view transformation

### Before state

```
EventBridge cron ─▶ Poller ─▶ Bedrock + Duffel + LiteAPI
                       │
                       ▼
            CloudWatch dashboard (ops metrics only)

A misconfigured cadence / alert loop runs up spend with NO
out-of-band signal. Design-spec §300 gate unmet; slice-9
checkbox blocked; no deploy is cost-safe.
```

### After state

```
            cdk deploy (account-level, region-agnostic)
                       │
                       ▼
        AWS::Budgets::Budget  trip-tracker-monthly-cost
        COST / MONTHLY / $10 USD
          ├─ 80%  ACTUAL    GREATER_THAN ─▶ EMAIL <addr>
          └─ 100% FORECASTED GREATER_THAN ─▶ EMAIL <addr>

addr = budgetAlarmEmail ?? notifierRecipientEmail (email-regex
validated at synth; blank/malformed throws before deploy).
A runaway loop now emails the operator at 80% of $10 and again
when the month is forecast to exceed $10.
```

### Interaction changes

| Location | Before | After | Operator impact |
|---|---|---|---|
| `cdk deploy` | no spend guard | one free Budget + 2 email alerts | gets emailed before a runaway loop becomes an expensive one |
| synth with bad/blank budget email | (n/a — no construct) | throws at synth | misconfig caught before deploy, not after a surprise bill |

## 6. Mandatory reading

| Priority | File | Lines | Why |
|---|---|---|---|
| P0 | `lib/notifier-server.js` | 1-75 | The exact pattern to mirror: `EMAIL_PATTERN` const (12-13), `scope.node.tryGetContext` (54-56), synth-time `throw new Error(...)` validation (60-75), JSDoc design-notes shape (15-49), named export (last line). Copy `EMAIL_PATTERN` verbatim. |
| P0 | `test/notifier-server.test.js` | 1-99 | The exact test pattern: `build(...)` returns a thunk `() => new Construct(...)`; `expect(build(...)).toThrow(/regexName/)` / `.not.toThrow()`; `describe` + `test.each` for accepted/rejected email shapes. |
| P0 | `lib/strands-agent-on-lambda-stack.js` | 1-115 | Where constructs are instantiated + how named-export constructs are imported (`const { NotifierServerConstruct } = require('./notifier-server')`). Add the import + one instantiation line. |
| P1 | `lib/poller-server.js` | 60-81 | The synth-time allowlist `throw` idiom (reference only — NOT applied here; §9 #2 explains why no mode flag). |

**External documentation:**

| Source | Section | Why |
|---|---|---|
| [aws-cdk-lib v2.196 aws-budgets CfnBudget](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_budgets.CfnBudget.html) | `budget` (BudgetDataProperty) + `notificationsWithSubscribers` | L1 prop shape: `budgetType`, `timeUnit`, `budgetLimit.{amount,unit}`, `notificationsWithSubscribers[].{notification,subscribers}` |
| [AWS::Budgets::Budget CFN ref](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-budgets-budget.html) | BudgetData / Notification / Subscriber | **GOTCHA (verified by synth, §0 #1):** `CfnBudget.SpendProperty.amount` is typed `number` and CDK's `numberToCloudFormation` is identity, so the synthesised `BudgetLimit.Amount` is the **JS number `10`** (`typeof === 'number'`), and `Notification.Threshold` is the **number `80`/`100`**. Tests/Gate MUST assert numbers — do NOT quote them as `'10'`/`'80'`. |

## 7. Patterns to mirror

### EMAIL REGEX + SYNTH VALIDATION (from `lib/notifier-server.js:12-13, 60-64`)

```js
const EMAIL_PATTERN =
    /^[A-Za-z0-9_%+\-]+(\.[A-Za-z0-9_%+\-]+)*@[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}$/;
// ...
if (!email || !EMAIL_PATTERN.test(email)) {
    throw new Error(
        `budgetAlarmEmail (or notifierRecipientEmail fallback) is required and must look like an email; got: ${email}`
    );
}
```

Copy `EMAIL_PATTERN` byte-for-byte — do not re-derive it. (The regex is also independently maintained in `notifier-server.js`; duplication here is intentional, same rationale as the triplicated verifier in ADR 0006 — a shared util across constructs is a bigger change than the fix. A one-line comment notes the source.)

### CfnBudget (canonical CDK L1; new in this PRP)

```js
// SOURCE: aws-cdk-lib v2.196 aws-budgets docs (no existing precedent in repo)
const budgets = require('aws-cdk-lib/aws-budgets');
new budgets.CfnBudget(this, 'CostBudget', {
    budget: {
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: { amount: 10, unit: 'USD' },
        budgetName: 'trip-tracker-monthly-cost',
    },
    notificationsWithSubscribers: [
        { notification: { notificationType: 'ACTUAL', comparisonOperator: 'GREATER_THAN', threshold: 80, thresholdType: 'PERCENTAGE' },
          subscribers: [{ subscriptionType: 'EMAIL', address: email }] },
        { notification: { notificationType: 'FORECASTED', comparisonOperator: 'GREATER_THAN', threshold: 100, thresholdType: 'PERCENTAGE' },
          subscribers: [{ subscriptionType: 'EMAIL', address: email }] },
    ],
});
```

### NAMED-EXPORT CONSTRUCT (from `lib/notifier-server.js` last line)

```js
module.exports = { BudgetAlarmConstruct };
```

Stack import mirrors the notifier: `const { BudgetAlarmConstruct } = require('./budget-alarm');`

### SYNTH-VALIDATION TEST (from `test/notifier-server.test.js:18-33`)

```js
function build(budgetEmail, recipientEmail) {
    const ctx = {};
    if (budgetEmail !== undefined) ctx.budgetAlarmEmail = budgetEmail;
    if (recipientEmail !== undefined) ctx.notifierRecipientEmail = recipientEmail;
    const app = new App({ context: ctx });
    const stack = new Stack(app, `T${counter++}`);
    return () => new BudgetAlarmConstruct(stack, 'B');
}
```

## 8. Files to change

| File | Action | Justification |
|---|---|---|
| `lib/budget-alarm.js` | CREATE | `BudgetAlarmConstruct`: reads `budgetAlarmEmail` ?? `notifierRecipientEmail` from context, `EMAIL_PATTERN` synth-validation (copied from notifier), one `CfnBudget` (COST/MONTHLY/$10) with 80% ACTUAL + 100% FORECASTED EMAIL notifications. JSDoc design-notes block in the notifier's style. Named export. |
| `lib/strands-agent-on-lambda-stack.js` | UPDATE | Add `const { BudgetAlarmConstruct } = require('./budget-alarm');` with the other requires; instantiate `new BudgetAlarmConstruct(this, 'BudgetAlarmConstruct');` alongside the other constructs (no props — it reads context itself, depends on nothing). |
| `test/budget-alarm.test.js` | CREATE | Synth-time-validation jest tests in the `notifier-server.test.js` shape: accepted/rejected email shapes, fallback-to-notifier-recipient, blank/malformed throws, AND template-shape assertions on the synthesised `AWS::Budgets::Budget` (exactly 1 resource; `BudgetLimit {Amount:10,Unit:'USD'}` — numeric `10`; `TimeUnit:'MONTHLY'`; `BudgetType:'COST'`; two notifications with the expected numeric `Threshold` + `NotificationType`/`ComparisonOperator` + EMAIL subscriber address). |

## 9. Locked decisions

1. **Email = `budgetAlarmEmail` ?? `notifierRecipientEmail`.** One address to maintain; the notifier recipient is already the human who acts on trip-tracker signals. `budgetAlarmEmail` is the explicit override. The construct re-validates with `EMAIL_PATTERN` regardless of source (it does not assume the notifier already validated — the notifier may not be built in a given synth, e.g. an isolated construct test).
2. **No mode flag.** AWS Budgets are free to create and harmless to deploy; synth/tests only assert the template. There is no live/stub distinction (unlike SES). The `sesMode`/`ALLOWED_SES_MODES` allowlist is deliberately NOT mirrored — mirroring it would add a meaningless knob. Only the email-regex validation transfers.
3. **`EMAIL_PATTERN` is copied, not shared.** Duplicated from `notifier-server.js` with a source comment. A shared `lib/email-validate.js` util is a larger refactor than this checklist item; the regex is stable (ADR 0005) and a future divergence would fail this construct's own email tests.
4. **Two notifications: 80% ACTUAL + 100% FORECASTED.** ACTUAL@80% is the early warning ("you've spent $8"); FORECASTED@100% is the trajectory warning ("this month is on track to exceed $10") and catches a runaway loop before month-end. Both email the same address. This matches the design-spec intent ("cheap insurance against a runaway loop") better than a single 100%-ACTUAL alert that only fires after the money is already spent.
5. **`budgetName: 'trip-tracker-monthly-cost'` is fixed.** A stable name makes the budget findable in the AWS console and idempotent across redeploys. (CFN would auto-name it otherwise; a stable explicit name is the right call for a singleton account budget.)
6. **Account-level, region-agnostic.** `AWS::Budgets::Budget` is global; the construct needs no region/account env and synthesises under the existing full-stack-synth gate context unchanged.

## 10. NOT building (explicit)

- **SNS topic / Chatbot / Slack budget actions.** Email subscriber only. Design-spec §300 says "email notification"; an SNS+subscription path is more infra for no added value at personal scale.
- **`AWS::Budgets::BudgetsAction` (auto-remediation, e.g. auto-detach IAM/stop resources).** Out of scope; the alarm is a *signal*, the operator decides the action. Auto-remediation on a personal project risks locking the owner out of their own stack.
- **Per-service budgets (separate Bedrock / Lambda budgets) or cost-allocation-tag filters.** One account-wide $10 COST budget is the spec. Cost filters are a future refinement if spend ever needs attribution.
- **A shared email-validation util.** §9 #3 — copy, don't refactor.
- **Stub/live mode + an env var.** §9 #2 — not applicable to Budgets.
- **CloudWatch billing alarm (the older `AWS/Billing` metric-alarm approach).** `AWS::Budgets::Budget` is the modern, region-agnostic mechanism and is what design-spec §300 / companion §47 call for.

## 11. Test matrix

### Group A — email validation (jest, `test/budget-alarm.test.js`)
- `test_A1_accepts_well_formed_budgetAlarmEmail` (`test.each` of valid shapes, mirror notifier's accepted list)
- `test_A2_rejects_malformed_budgetAlarmEmail` (`test.each`: bare domain, leading @, no TLD, 1-char TLD, leading/trailing domain hyphen, double/leading/trailing dot in local, CRLF injection, space, empty, undefined) ⇒ `toThrow(/budgetAlarmEmail|notifierRecipientEmail/)`
- `test_A3_falls_back_to_notifierRecipientEmail_when_budgetAlarmEmail_unset` (no `budgetAlarmEmail`, valid `notifierRecipientEmail` ⇒ not throw)
- `test_A4_budgetAlarmEmail_overrides_notifierRecipientEmail` (both set, budget email distinct ⇒ the synthesised subscriber address is the override, not the fallback)
- `test_A5_throws_when_neither_email_is_set` ⇒ `toThrow`

### Group B — synthesised budget shape (jest, same file, `Template.fromStack`)
- `test_B1_exactly_one_budgets_budget_resource`
- `test_B2_budget_is_COST_MONTHLY_limit_10_USD` (assert `BudgetType:'COST'`, `TimeUnit:'MONTHLY'`, `BudgetLimit:{Amount:10,Unit:'USD'}` — Amount is the **number** `10`, see §6 gotcha; quoting it as `'10'` fails)
- `test_B3_has_80pct_ACTUAL_and_100pct_FORECASTED_notifications` (assert both `Notification` blocks: `NotificationType`, `Threshold` as the **number** `80`/`100` (not `'80'`), `ComparisonOperator: 'GREATER_THAN'`, `ThresholdType: 'PERCENTAGE'`)
- `test_B4_both_notifications_have_one_EMAIL_subscriber_with_the_resolved_address`
- `test_B5_budgetName_is_trip_tracker_monthly_cost`

### Group C — stack wiring (jest, fold into B or a `full-stack synth` assertion)
- `test_C1_full_stack_synth_contains_exactly_one_budgets_budget` — instantiate `StrandsAgentOnLambdaStack` with the standard Docker-skipping context and assert one `AWS::Budgets::Budget` with the $10/MONTHLY/COST shape (proves the stack actually wires the construct, not just that the construct works in isolation).

## 12. Validation gates

### Gate 1 — Construct loads
```
node -e "const { BudgetAlarmConstruct } = require('./lib/budget-alarm'); console.log(typeof BudgetAlarmConstruct);"
```
EXPECT: `function`.

### Gate 2 — Stack requires cleanly
```
node -e "require('./lib/strands-agent-on-lambda-stack'); console.log('ok');"
```
EXPECT: `ok`.

### Gate 3 — Jest suite (new + regression)
```
cd C:/Users/isabe/Downloads/trip-tracker-agent && npx jest test/
```
EXPECT: prior 98 + ~16 new (Group A/B/C) = ~114 passing, including the pre-existing `notifier-server`, `observability-dashboard`, `secrets-construct`, `stack-secrets-wiring`, `agent-bedrock-iam` suites still green.

### Gate 4 — Comment-cleanliness ripgrep (new files)
```
rg -n --no-heading 'slice[ -_]?\d|\bT[1-9]\b|\bTask [1-9]\b|Checkpoint [A-Z]\b' \
  C:/Users/isabe/Downloads/trip-tracker-agent/lib/budget-alarm.js \
  C:/Users/isabe/Downloads/trip-tracker-agent/test/budget-alarm.test.js
rg -n --no-heading -w 'basically|simply|obviously|essentially|clearly|merely|kind of' \
  <same files>
```
EXPECT: zero matches in both (`.slice(` JS-method false positives are not applicable here — no such call in this construct).

### Gate 5 — Full-stack synth + budget-shape assertion (Docker-skipping node-eval, per `project_cdk_test_invocation_gotchas`)

Env is set INSIDE the eval (no shell-specific `VAR=…` prefix — §0 #2), so it runs identically under Bash or PowerShell:
```
node -e "
  process.env.DUFFEL_API_KEY='stub'; process.env.LITEAPI_API_KEY='stub';
  const { App } = require('aws-cdk-lib');
  const { StrandsAgentOnLambdaStack } = require('./lib/strands-agent-on-lambda-stack');
  const app = new App({ context: {
    'aws:cdk:bundling-stacks': [], mcpMode:'fixture',
    bedrockModelId:'claude-haiku-4-5-20251001', bedrockMode:'stub',
    notifierSenderEmail:'s@example.com', notifierRecipientEmail:'me@example.com', sesMode:'stub',
  }});
  new StrandsAgentOnLambdaStack(app, 'TestStack', {});
  const t = app.synth().getStackByName('TestStack').template;
  const b = Object.values(t.Resources||{}).filter(r => r.Type === 'AWS::Budgets::Budget');
  if (b.length !== 1) { console.error('expected 1 budget, got', b.length); process.exit(1); }
  const bd = b[0].Properties.Budget;
  if (bd.BudgetType !== 'COST' || bd.TimeUnit !== 'MONTHLY') { console.error('wrong budget type/unit'); process.exit(1); }
  if (bd.BudgetLimit.Amount !== 10 || bd.BudgetLimit.Unit !== 'USD') { console.error('wrong limit', JSON.stringify(bd.BudgetLimit)); process.exit(1); }
  const ns = b[0].Properties.NotificationsWithSubscribers || [];
  if (ns.length !== 2) { console.error('expected 2 notifications, got', ns.length); process.exit(1); }
  const thr = ns.map(n => n.Notification.Threshold).sort((a,c)=>a-c);
  if (thr[0] !== 80 || thr[1] !== 100) { console.error('wrong thresholds', thr); process.exit(1); }
  const addrs = ns.flatMap(n => n.Subscribers.map(s => s.Address));
  if (!addrs.every(a => a === 'me@example.com')) { console.error('subscriber addr wrong', addrs); process.exit(1); }
  console.log('GATE5 ok: 1 budget, COST/MONTHLY/10 USD (number), 80+100 numeric thresholds, email me@example.com');
"
```
EXPECT: the `GATE5 ok: …` line. `Amount` and `Threshold` are asserted as **numbers** (`!== 10`, `!== 80`), per §0 #1.

### Gate 6 — Notifier + poller regression (pure)
```
cd C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/notifier && \
  "C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/python.exe" -m pytest tests/ -q
"C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/python.exe" \
  -m pytest C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/poller/tests/ \
            C:/Users/isabe/Downloads/trip-tracker-agent/evals/tests/ -q
```
EXPECT: notifier 126 passing; poller + evals 312 passing. (This construct touches no Python; a pure regression check that the stack-wiring edit didn't break the synth those suites' fixtures don't exercise — defensive, cheap.)

## 13. Constraints inherited

- **Zero `slice X` / `T#` / `Task N` / `Checkpoint A-Z` / `phase N`** and **zero filler** in any new file or commit message (global CLAUDE.md). Implementation commits describe intent.
- All construct tests synthesise with `'aws:cdk:bundling-stacks': []` to skip Docker bundling (memory `project_cdk_test_invocation_gotchas`).
- Jest from repo root; Python via `.venv-tests/Scripts/python.exe`.
- **Multi-reviewer gate** at the end: code-reviewer five-axis → security-auditor → test-engineer → code-reviewer comments. Sequential per memory `feedback_subagents_sequential`, different models per `feedback_multi_model_workflow`. Fixes inline per reviewer, pinned by tests.

## 14. Step-by-step

### Task 1: CREATE `lib/budget-alarm.js`
- **ACTION**: New `BudgetAlarmConstruct extends Construct`. (a) `const budgets = require('aws-cdk-lib/aws-budgets');` + `const { Construct } = require('constructs');`. (b) Copy `EMAIL_PATTERN` verbatim from `lib/notifier-server.js:12-13` with a one-line "SOURCE: lib/notifier-server.js (ADR 0005); copied, not shared — see PRP §9 #3" comment. (c) JSDoc design-notes block in the notifier's style (what/why: free account budget, $10, 80 ACTUAL + 100 FORECASTED, email-only, no mode flag because Budgets are free/deploy-safe). (d) In the constructor: `const email = scope.node.tryGetContext('budgetAlarmEmail') ?? scope.node.tryGetContext('notifierRecipientEmail');` then the `if (!email || !EMAIL_PATTERN.test(email)) throw new Error(...)` guard naming both context keys. (e) One `CfnBudget` per §7. (f) `module.exports = { BudgetAlarmConstruct };`.
- **MIRROR**: `lib/notifier-server.js:1-75` (regex + tryGetContext + throw + JSDoc + named export).
- **VALIDATE**: Gate 1.

### Task 2: UPDATE `lib/strands-agent-on-lambda-stack.js`
- **ACTION**: Add `const { BudgetAlarmConstruct } = require('./budget-alarm');` alongside the other `require`s. Instantiate `new BudgetAlarmConstruct(this, 'BudgetAlarmConstruct');` alongside the other constructs (no props object — it reads context itself and depends on nothing). Place it near the other context-reading constructs (e.g. after the `SecretsConstruct` / before or after the dashboard — order does not matter, it has no deps).
- **VALIDATE**: Gate 2.

### Task 3: CREATE `test/budget-alarm.test.js`
- **ACTION**: Jest, mirroring `test/notifier-server.test.js`. `build(budgetEmail, recipientEmail)` thunk per §7. Group A (email accept/reject/fallback/override/neither) using `describe` + `test.each`. Group B using `const { Template, Match } = require('aws-cdk-lib/assertions'); Template.fromStack(stack)` → `resourceCountIs('AWS::Budgets::Budget', 1)` + `hasResourceProperties` for the COST/MONTHLY/`Amount:10`/notifications/subscriber shape. Group C: instantiate `StrandsAgentOnLambdaStack` with the Docker-skip context and assert exactly one budget of the right shape.
- **GOTCHA (§0 #1, verified by synth)**: `BudgetLimit.Amount` is the **number** `10` and `Notification.Threshold` the **number** `80`/`100` — assert `Amount: 10` / `Threshold: 80`, NOT quoted strings. `Template.hasResourceProperties` is a partial/subset match, so use `Match.arrayWith`/`Match.objectLike` (import `Match` from `aws-cdk-lib/assertions`) for the notification array to avoid over-pinning ordering.
- **VALIDATE**: Gate 3.

### Task 4: threat-model touch-ups (docs) — do NOT touch the companion checklist
- **ACTION (threat-model.md only)**: Flip all THREE budget references to backward references for the now-built construct, and remove the inaccuracies §0 #3 found:
  - `docs/threat-model.md:188` boundary [6] "Cost runaway" row: `AWS Budget alarm at $10/mo (planned, slice 9) is the safety net.` → `AWS Budget alarm at $10/mo (BudgetAlarmConstruct, lib/budget-alarm.js; design-spec §300) is the safety net.`
  - `docs/threat-model.md:197`: `AWS Budget alarm at $10/mo with SNS topic email subscription (slice 9).` → drop "SNS topic" (we use a **direct EMAIL subscriber, no SNS** — §10) and "slice 9" (roadmap-label rule): `AWS Budget alarm at $10/mo, direct email subscriber (BudgetAlarmConstruct).`
  - `docs/threat-model.md:243`: `AWS Budget alarm at $10/mo (deferred).` → `AWS Budget alarm at $10/mo (built — BudgetAlarmConstruct).`
- **DO NOT** touch companion spec §5 line 205 (§0 #4): it says "AWS Budget alarm **deployed**" and this PRP deploys nothing; the whole `[ ]` line also still has CI/README/Loom/ADR-0007 unbuilt. Leave it exactly as-is — "deployed-as-pending" stays accurate.
- **No ADR**: design-spec §300 already records the decision; a Budget alarm is not an architecture-decision fork. Stated so the reviewer gate does not flag a missing ADR.
- **VALIDATE**: Gate 4 over the changed threat-model lines (no new `slice`/filler introduced); `grep -n 'Budget alarm' docs/threat-model.md` shows zero `slice 9`/`planned`/`deferred`/`SNS` in the three rows, and `git diff -- docs/superpowers/specs/2026-05-10-trip-tracker-production-readiness.md` is empty.

## 15. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Implementer quotes `Amount`/`Threshold` as strings (the original PRP's inverted gotcha) → Group B + Gate 5 fail | LOW | LOW | §0 #1 + §6 + §11 B2/B3 + Gate 5 + Task 3 now all pin **numbers** (`10`, `80`, `100`), verified by direct synth. The wrong assumption was caught and inverted pre-implementation. |
| `EMAIL_PATTERN` copy drifts from the notifier's | LOW | LOW | Copied verbatim with a SOURCE comment; this construct's Group A tests independently pin the same accept/reject shapes the notifier tests pin, so a divergence fails here. |
| Construct instantiated with a props object out of habit (notifier takes props) | LOW | LOW | §3 #5 + §14 Task 2 explicitly state NO props; Gate 2 + Group C catch a constructor-signature mismatch. |
| Reviewer flags "missing ADR for a new construct" | LOW | LOW | §14 Task 4 pre-empts: design-spec §300 already records the decision; a Budget alarm is not an architectural fork. Documented so it is a deliberate non-decision, not an omission. |
| `notificationsWithSubscribers` shape rejected by CFN (wrong enum casing) | LOW | MED | Enums pinned exactly in §7 (`ACTUAL`/`FORECASTED`/`GREATER_THAN`/`PERCENTAGE`/`EMAIL`); Gate 5 synthesises the real stack and would fail on a CFN-invalid shape. |

## What "done" looks like

- `lib/budget-alarm.js` created; `BudgetAlarmConstruct` named-exported.
- Stack imports + instantiates it (no props).
- `test/budget-alarm.test.js` created; Group A/B/C green.
- All 6 validation gates green; jest `test/` ~114 passing with every pre-existing suite still green; notifier 126 + poller/evals 312 regression clean.
- `git grep -i budget -- lib/ test/` shows the new construct + test; all three `docs/threat-model.md` budget rows (188/197/243) flipped to backward references with no `slice`/`SNS`/`planned`/`deferred`; companion spec §5 is untouched (`git diff` empty there).
- No `slice/T#/Task-N/filler` in any new file or commit.

## Confidence

**9/10.** One L1 resource, exact in-repo precedent for every line (notifier construct + its test), no new deps, no runtime/Lambda surface, no Python. The single sharp edge (string-vs-number `Amount`) is pinned three times. Point off because `aws-budgets` is new to this repo so the `notificationsWithSubscribers` enum casing has no in-repo precedent — Gate 5 (real-stack synth) is the backstop that catches a CFN-invalid shape before the reviewer gate.
