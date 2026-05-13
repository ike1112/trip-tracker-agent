# Slice 6 — Bedrock decision + evals — Todo

Companion to [`slice-6-bedrock-decision.plan.md`](./slice-6-bedrock-decision.plan.md). One checklist per task.

## Task 1 — `bedrock_decide.py` + unit tests (mocked boto3) — ✅ DONE (commit `16b6a96`)

- [x] **Multi-model gate (test design FIRST):** spawn `agent-skills:test-engineer` (Sonnet) for the parser/fallback test matrix
- [x] `bedrock_decide.py` — boto3 InvokeModel wrapper, prompt builder, strict JSON parser
- [x] `BEDROCK_MODE` env var (live/stub) at module load
- [x] `BEDROCK_MODEL_ID` env var, default `claude-haiku-4-5-20251001`
- [x] Defensive fallback: parse/IAM/network failures → `{alert: False, reason: "model_response_invalid"|"model_call_failed", bedrock_called: True}`
- [x] Tests: `test_bedrock_decide.py` — 39 tests across 10 groups (parsing, injection safety, error paths, mode selection, content pinning, etc.)
- [x] No real Bedrock calls during pytest
- [x] `pytest` green (168 passing)
- [x] Commit

## Task 2 — Wire `decision.py` + CDK IAM + integration test — ✅ DONE (commit `5e5a49e`)

- [x] `decision.py` — delegates to `bedrock_decide.decide(...)` when gates pass; pre-gate skips short-circuit before any model call
- [x] `lib/poller-server.js` — `bedrock:InvokeModel` IAM grant resource-scoped to the model ARN; `BEDROCK_MODEL_ID` + `BEDROCK_MODE` env vars; synth-time validation of context values
- [x] `tests/conftest.py` — sets `BEDROCK_MODE=stub` at module load so the full suite stays cost-free
- [x] Tests: `test_decision.py` updated; `test_decision_live_mode.py` adds 6 live-mode tests via mocked Bedrock client
- [x] `pytest` green (174 passing: 129 prior + 39 T1 + 6 T2)
- [x] **Multi-model gate:** `agent-skills:security-auditor` (Sonnet) — fix-then-ship; addressed inference-profile ARN format + bedrockMode allowlist
- [x] Findings addressed
- [x] Commit

### → Checkpoint A — ✅ PASSED
- [x] Mocked end-to-end Bedrock pipeline working
- [x] `agent-skills:code-reviewer` (Sonnet) — APPROVE with fixes; replaced misleading no-call test with patched-client version, removed dead branches
- [x] Findings addressed
- [x] Human approval before Task 3 (2026-05-13)
- [x] Pre-flight cleanup commit `ce13c80` — stripped stale task-context refs from source comments before T3 starts

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
