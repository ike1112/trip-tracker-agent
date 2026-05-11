# Slice 6 — Bedrock decision + evals — Todo

Companion to [`slice-6-bedrock-decision.plan.md`](./slice-6-bedrock-decision.plan.md). One checklist per task.

## Task 1 — `bedrock_decide.py` + unit tests (mocked boto3)

- [ ] **Multi-model gate (test design FIRST):** spawn `agent-skills:test-engineer` (Sonnet) for the parser/fallback test matrix
- [ ] `bedrock_decide.py` — boto3 InvokeModel wrapper, prompt builder, strict JSON parser
- [ ] `BEDROCK_MODE` env var (live/stub) at module load
- [ ] `BEDROCK_MODEL_ID` env var, default `claude-haiku-4-5-20251001`
- [ ] Defensive fallback: parse/IAM/network failures → `{alert: False, reason: "model_response_invalid"|"model_call_failed", bedrock_called: True}`
- [ ] Tests:
  - [ ] `test_bedrock_decide.py` — prompt determinism, JSON-only parsing positive + 6 malformation cases, IAM error fallback, throttle fallback, stub-mode shape, live-mode happy path
- [ ] No real Bedrock calls during pytest
- [ ] `pytest` green
- [ ] Commit

## Task 2 — Wire `decision.py` + CDK IAM + integration test

- [ ] `decision.py` — replace stub body with `bedrock_decide.decide(...)` call
- [ ] `lib/poller-server.js` — `bedrock:InvokeModel` IAM grant resource-scoped to the model ARN; `BEDROCK_MODEL_ID` + `BEDROCK_MODE` env vars
- [ ] `tests/conftest.py` — set `BEDROCK_MODE=stub` in fixture env so existing 129 tests don't change behaviour
- [ ] Tests:
  - [ ] `test_decision.py` updated — stub-mode tests still green; new live-mode tests assert `bedrock_decide` invoked
  - [ ] `test_decision_live_mode.py` — integration with mocked Bedrock client
- [ ] `pytest` green (all 129 slice-5 + new T1/T2)
- [ ] **Multi-model gate:** spawn `agent-skills:security-auditor` (Sonnet) on IAM grant + Bedrock cost surface
- [ ] Address findings
- [ ] Commit

### → Checkpoint A
- [ ] Mocked end-to-end Bedrock pipeline working
- [ ] **Spawn `agent-skills:code-reviewer` (Sonnet)** on bedrock_decide + decision + CDK changes
- [ ] Address findings
- [ ] Human approval before Task 3
- [ ] Commit

## Task 3 — Eval scaffolding + runner tests

- [ ] **Multi-model gate (test design FIRST):** spawn `agent-skills:test-engineer` (Sonnet) for runner test matrix
- [ ] `evals/run_evals.py` — CLI: load fixtures → call `bedrock_decide.decide` → call Sonnet judge → emit markdown report
- [ ] `evals/judge_prompts/decision.md` — rubric for grading model output
- [ ] `evals/README.md` — how to run, cost note, when to re-run
- [ ] `evals/fixtures/decision/` — initial 2-3 fixtures
- [ ] `evals/tests/test_eval_runner.py` — loader, judge-prompt formatter, report writer, exit code
- [ ] `pytest` green
- [ ] Commit

## Task 4 — Golden set + baseline report + ADR 0004 + threat model `[6]`

- [ ] `evals/fixtures/decision/` — ≥30 hand-labelled cases (15 alert + 15 no-alert)
- [ ] `evals/results/2026-05-10-baseline.md` — committed sample run
- [ ] `docs/adr/0004-bedrock-decision.md` — Context/Decision/Consequences peer to 0001/0002/0003
- [ ] `docs/adr/README.md` — 0004 row flipped to Accepted
- [ ] `docs/threat-model.md` — new row(s) for Bedrock attack surface (prompt injection via bestOfferBlob, cost runaway, model-output validation as defence)

### → Checkpoint B (Slice 6 complete)
- [ ] **Multi-model final gate (SEQUENTIAL, not parallel — see memory `feedback_subagents_sequential`):**
  - [ ] `agent-skills:code-reviewer` (Sonnet) — five-axis review of slice as a whole
  - [ ] `agent-skills:security-auditor` (Sonnet) — Bedrock IAM + prompt-injection surface + threat model
  - [ ] `agent-skills:test-engineer` (Sonnet) — verify no placeholder tests, eval framework actually catches model regressions
- [ ] Address findings
- [ ] Human approval
- [ ] Commit
- [ ] Tick slice 6 row in production-readiness companion §5 launch checklist

---

## Locked decisions (§7 of plan)

1. ✅ Model: Haiku 4.5 (decision) + Sonnet 4.6 (judge)
2. ✅ `BEDROCK_MODE=stub` for tests
3. ✅ `BEDROCK_MODEL_ID` env var, default `claude-haiku-4-5-20251001`
4. ✅ Eval runner is local-only in slice 6 (CI in slice 9)
5. ✅ Defensive fallback: model failures → `{alert: False, reason: "model_*", bedrock_called: True}`
6. ✅ Golden set ≥ 30 cases
7. ✅ IAM grant resource-scoped to model ARN
