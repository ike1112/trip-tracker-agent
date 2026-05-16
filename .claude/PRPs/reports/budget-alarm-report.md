# Implementation Report — AWS Budget alarm CDK construct

**Plan:** `tasks/budget-alarm.prp.md`
**Completed:** 2026-05-16
**Iterations:** 1 (Ralph, max 10)

## Summary

Added `BudgetAlarmConstruct` (`lib/budget-alarm.js`) — one account-level
`AWS::Budgets::Budget` (COST, $10/month) with 80% ACTUAL + 100% FORECASTED
email notifications, mirroring `NotifierServerConstruct`'s `EMAIL_PATTERN`
synth-time validation. Closes the last code item on the production-readiness
launch checklist (design-spec §300; companion §5 slice-9).

## Tasks completed (4)

1. `lib/budget-alarm.js` — construct: `budgetAlarmEmail ?? notifierRecipientEmail`,
   EMAIL_PATTERN regex (copied verbatim from notifier, sourced comment),
   `CfnBudget` COST/MONTHLY/$10 + two notifications, named export, no props,
   no mode flag (Budgets are free/deploy-safe — §9 #2).
2. `lib/strands-agent-on-lambda-stack.js` — require + `new BudgetAlarmConstruct(this, 'BudgetAlarmConstruct')`.
3. `test/budget-alarm.test.js` — Group A (email accept/reject/fallback/override/neither),
   Group B (synth shape: numeric `Amount:10`/`Threshold:80,100`, names, subscribers),
   Group C (full-stack wiring). 27/27.
4. `docs/threat-model.md` rows 188/197/243 flipped to backward references
   (dropped stale `slice 9`/`SNS`/`planned`/`deferred`); companion §5 left
   untouched (nothing deployed — §0 #4).

## Validation results (all 6 gates)

| Gate | Result |
|------|--------|
| 1 construct loads | `function` |
| 2 stack requires | `ok` |
| 3 full jest `test/` | 6 suites / 125 tests pass (98 prior + 27 new) |
| 4 cleanliness (new files + 3 threat rows) | clean; companion §5 untouched |
| 5 full-stack synth budget shape | 1 budget, COST/MONTHLY/numeric 10, 80+100, email resolved |
| 6 notifier + poller/evals regression | 126 + 312 pass |

## Codebase patterns discovered

- `aws-budgets.CfnBudget` serialises `BudgetLimit.Amount` and
  `Notification.Threshold` as **numbers**, not strings (PRP §0 #1, the
  adversarial pass caught the PRP's inverted assumption pre-implementation).
- A jest test that calls `jest.resetModules()` + re-`require`s the stack
  must ALSO re-require `App`/`Template`/`Match` from `aws-cdk-lib` INSIDE
  the test — top-of-file matcher imports are a different module instance
  post-reset and silently fail to match (`Missing key 'name'/'partial'/'pattern'`).

## Deviations from plan

- C1 (full-stack) initially failed on the cross-instance `Match` issue
  above; fixed by relocating the `aws-cdk-lib`/`assertions` requires
  inside the test, after `jest.resetModules()`. No production-code change.

## Outstanding

PRP §13 sequential 4-reviewer gate (code five-axis → security → test →
comments) — separate phase, not a Ralph iteration.
