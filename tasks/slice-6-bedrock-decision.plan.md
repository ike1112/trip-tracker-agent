# Implementation Plan — Slice 6: Bedrock decision + evals baseline

**Date:** 2026-05-10
**Status:** Draft (awaiting human review)
**Companion specs:**
- Design: [`docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md`](../docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md) (§5 polling/decision flow + §6 evals)
- Production-readiness: [`docs/superpowers/specs/2026-05-10-trip-tracker-production-readiness.md`](../docs/superpowers/specs/2026-05-10-trip-tracker-production-readiness.md) (§2 slice 6, §4.3 evals as repo artifacts)

---

## 1. Overview

Slice 6 swaps the slice-5 stub for a **real Bedrock Haiku 4.5 call** in `decision.py` and lands the **eval framework** (fixtures + judge rubric + local runner + sample report). The poller's call sites do not change — slice 5 deliberately introduced the `bedrock_called` flag to make this swap a one-module change.

What slice 6 ships:
- `lambdas/poller/bedrock_decide.py` — new module that calls Bedrock Haiku 4.5 with the snapshot + history + watch criteria and parses `{alert, reason}` from the model's JSON response.
- `lambdas/poller/decision.py` — body modified: when gates pass, call `bedrock_decide()` instead of returning the stub.
- `lib/poller-server.js` — `bedrock:InvokeModel` IAM grant + `BEDROCK_MODEL_ID` env var.
- `evals/` — fixtures, judge prompts, `run_evals.py` runner, baseline report.
- ADR 0004 — Bedrock-decision rationale.
- Threat model `[6]` row appended (or augmenting `[5]`) — what changes when the model is invoked: cost surface, prompt-injection from FareHistory, model output validation.

What slice 6 does NOT ship:
- Eval-on-CI gating (slice 9 work item; cost discipline).
- Notifier / SES (slice 7).
- `lastAlertedAt` writeback (slice 7, ADR 0005).

---

## 2. Architecture decisions

### 2.1 New module `bedrock_decide.py` — keeps the wrapper isolated

The Bedrock call has its own concerns (model ID, prompt construction, response parsing, error handling, JSON-mode validation) that don't belong in `decision.py`. Splitting them mirrors the slice-3/slice-4 pattern of `client-live.js` vs `client-fixture.js` — a thin live wrapper behind a stable interface. `decision.py` keeps its routing logic and just delegates the model call.

### 2.2 Default mode: live; test/eval mode: stub

There's no fixture-replay mode for Bedrock here — unlike slice-3/slice-4 MCPs, the model output is the thing under test in evals. Three modes:

- **Live** (production / CI integration tests): real Bedrock InvokeModel.
- **Stub** (unit tests + slice-5 era behaviour): returns `{alert: True, reason: "stub"}` when called. Selected by env var `BEDROCK_MODE=stub` so tests can flip it deterministically.
- **Eval-runner**: live calls with metrics captured; same code path as production but the runner orchestrates fixture replay + judge calls.

Selector mechanism mirrors `MCP_MODE`: `BEDROCK_MODE` env var, default `live`. Local Lambda invocation tests run with `BEDROCK_MODE=stub` so they don't burn calls.

### 2.3 Model: Claude Haiku 4.5 (`claude-haiku-4-5-20251001`)

Per design-spec §4 model-choice: small bounded decision, Haiku is fast + cheap. Pin the exact model ID in `BEDROCK_MODEL_ID` env var (CDK default) so it's a one-line bump when AWS adds a newer Haiku.

### 2.4 Prompt structure — one-shot, JSON-only output

The prompt template lives in `lambdas/poller/bedrock_decide.py` (not a separate file — it's small, ~40 lines, and version-locked with the parsing logic). Structured as:

- **System:** "You are deciding whether a trip-price snapshot is worth alerting the user. Return JSON with `alert` (bool) and `reason` (string ≤200 chars). No prose, no markdown, no other keys."
- **User:** structured context — current total, max budget, 30-day median + min, the watch's `preferences`, the snapshot's `bestOfferBlob`.

Use Anthropic Messages API with `max_tokens: 200` and a low temperature (0.0–0.2). Parse the response as strict JSON; on any parse failure, fall back to **conservative no-alert** with `reason: "model_response_invalid"` (better to miss an alert than to send a confusing one).

### 2.5 Eval framework — local runner, not deployed

Per design-spec §6 + companion §4.3:

```
evals/
  fixtures/
    decision/
      tokyo-30day-low.json         # synthesized "should alert" case
      tokyo-above-threshold.json   # "should not alert" case
      ...                          # 30 cases minimum (15 alert, 15 no-alert)
  judge_prompts/
    decision.md                    # Sonnet 4.6 rubric for grading
  run_evals.py                     # local entrypoint
  results/
    2026-05-10-baseline.md         # sample run output (markdown table)
  README.md                        # how to run locally
```

`run_evals.py`: reads each fixture, calls the same `bedrock_decide()` function the poller uses, captures the response, then asks Sonnet 4.6 to judge (per the rubric in `judge_prompts/decision.md`) whether the response is reasonable for the case. Emits a markdown report.

Eval is **manual** — not on every PR (cost). Run before any major prompt change. Slice 9 wires it as a `workflow_dispatch` GitHub Action.

### 2.6 No eval-on-CI gating in slice 6

Companion §4.3 explicitly defers this. Slice 6 ships the runner + a baseline report; slice 9 wires the GHA `workflow_dispatch` trigger. Keeps slice 6 budget bounded.

### 2.7 Defensive fallback if model call fails

Three failure modes for the live Bedrock call: (1) IAM/network/throttle error → `BedrockDecideError` → caught in `app.py`'s per-watch try/except → `watch_errored`. (2) Model returns malformed JSON → fall back to `{alert: False, reason: "model_response_invalid", bedrock_called: True}` so the metric still increments correctly but no spurious alert fires. (3) Model returns valid JSON but with extra/missing fields → same fallback. Belt-and-braces against silent corruption.

### 2.8 Carryover handling

The two slice-5 carryovers (shared `JWT_SIGNATURE_SECRET` HIGH; agent's missing `iat`/`exp`) remain deferred to slice 9. Slice 6 does not touch them. Document this in the ADR 0004 References section so future readers don't think they were addressed here.

---

## 3. Dependency graph

```
                     ┌──────────────────────────┐
                     │ lib/poller-server.js     │  + bedrock:InvokeModel IAM
                     │  + BEDROCK_MODEL_ID env  │  + BEDROCK_MODE env
                     └────────────┬─────────────┘
                                  │ injects
                                  ▼
                ┌─────────────────────────────────┐
                │ lambdas/poller/decision.py      │  modified to call bedrock_decide
                │  (gates routing unchanged)       │  when gates pass
                └────────────┬────────────────────┘
                             │ calls
                             ▼
                ┌─────────────────────────────────┐
                │ lambdas/poller/bedrock_decide.py│  new — boto3 InvokeModel,
                │                                 │  prompt builder, JSON parser,
                │                                 │  fallback logic
                └────────────┬────────────────────┘
                             │
                             ▼
                ┌─────────────────────────────────┐
                │ evals/run_evals.py              │  uses the same bedrock_decide
                │  + fixtures + judge_prompts     │  function the poller uses
                │  + baseline results report      │
                └─────────────────────────────────┘

Cross-cutting: docs/adr/0004-bedrock-decision.md, docs/threat-model.md [6] row
```

---

## 4. Task list (vertical slices)

### Phase A — Bedrock wrapper

#### Task 1: `bedrock_decide.py` module + unit tests (mocked boto3)

**Description:** Create the new module that wraps the Bedrock InvokeModel call. All tests mock `boto3.client("bedrock-runtime")` so no real Bedrock calls fire. Stub mode returns `{alert: True, reason: "stub", bedrock_called: True}` for environments where `BEDROCK_MODE=stub`.

**Acceptance criteria:**
- [ ] `bedrock_decide.py` exposes `decide(snapshot, watch, history) -> dict` returning `{alert: bool, reason: str, bedrock_called: bool}` (matches the slice-5 contract).
- [ ] `BEDROCK_MODE` env var selects live vs stub at module load (mirrors `MCP_MODE` from `client.js`).
- [ ] `BEDROCK_MODEL_ID` env var pins the model (default `claude-haiku-4-5-20251001`).
- [ ] Prompt builder produces a deterministic structured payload (same inputs → same prompt text).
- [ ] Response parser: strict JSON-only; rejects extra keys, missing keys, non-bool `alert`, non-string `reason`.
- [ ] Fallback: any parse/IAM/network failure returns `{alert: False, reason: "model_response_invalid"|"model_call_failed", bedrock_called: True}`. The metric still fires (we tried), but no spurious alert.
- [ ] `BedrockDecideError` raised only on programmer-error paths (e.g., missing env var); not on transient model failures (those become defensive fallback).

**Verification:**
- [ ] `test_bedrock_decide.py`: prompt-build determinism; strict JSON parsing (positive case + 6 malformation cases); IAM error → defensive fallback; transient throttle → defensive fallback; stub-mode returns the expected shape; live-mode happy path with mocked `bedrock-runtime`.
- [ ] No real Bedrock calls during pytest.
- [ ] `pytest lambdas/poller/tests` green.

**Dependencies:** None.

**Files:** new `lambdas/poller/bedrock_decide.py`, `lambdas/poller/tests/test_bedrock_decide.py`. Modified: `lambdas/poller/requirements.txt` (no new deps — `boto3` already present).

**Estimated scope:** **M** (1 new module, 1 new test file, ~10–15 tests).

**Multi-model gate:** spawn `agent-skills:test-engineer` (Sonnet) BEFORE writing tests to design the prompt-builder + parser test matrix.

---

### Phase B — Wire it into the pipeline

#### Task 2: `decision.py` calls `bedrock_decide`; CDK IAM + env vars; integration test

**Description:** Replace the stub body in `decision.py` (the lines under "Slice 6 replaces this body...") with a call to `bedrock_decide.decide(snapshot, watch, history)`. Add the `bedrock:InvokeModel` IAM grant + `BEDROCK_MODEL_ID` + `BEDROCK_MODE` env vars to the poller construct.

**Acceptance criteria:**
- [ ] `decision.py` body, after gates pass, returns whatever `bedrock_decide.decide(...)` returns. The contract (`alert`, `reason`, `bedrock_called`) is unchanged from the caller's perspective.
- [ ] `lib/poller-server.js` adds `bedrockRuntimePolicy` granting `bedrock:InvokeModel` resource-scoped to the model ARN (not `*`).
- [ ] CDK env vars: `BEDROCK_MODEL_ID = "claude-haiku-4-5-20251001"` (or via context override); `BEDROCK_MODE = "live"` (default).
- [ ] Slice 5's existing `test_decide_does_not_call_bedrock_in_slice5` test gets renamed and rewritten to assert that `decide()` DOES call `bedrock_decide.decide` (in live mode) or returns the stub shape (in stub mode), with the correct env-mode selector.
- [ ] All existing 129 slice-5 tests still pass with `BEDROCK_MODE=stub` set in conftest.

**Verification:**
- [ ] `test_decision.py` updated: stub-mode tests still pass; new tests assert `bedrock_decide` is called in live mode (mocked).
- [ ] `test_handler_decides.py` + `test_e2e_poll.py` set `BEDROCK_MODE=stub` so they don't try to hit Bedrock.
- [ ] `pytest` green (all 129 + new T1/T2 tests).
- [ ] Construct loads + stack constructs cleanly (`node -e "require('./lib/strands-agent-on-lambda-stack').new..."`).

**Dependencies:** Task 1.

**Files:** Modified `lambdas/poller/decision.py`, `lib/poller-server.js`, `lambdas/poller/tests/conftest.py` (set `BEDROCK_MODE=stub` in env), `lambdas/poller/tests/test_decision.py`. New: `lambdas/poller/tests/test_decision_live_mode.py` (mocked-bedrock integration test).

**Estimated scope:** **M** (4 modified, 1 new test file).

**Multi-model gate:** spawn `agent-skills:security-auditor` (Sonnet) — first time IAM grant for an externally-billable AWS service is added; resource-scoping + cost-runaway concerns matter.

---

#### Checkpoint A — Production code path complete

After Task 2:
- [ ] Poller's decision pipeline runs end-to-end with mocked Bedrock.
- [ ] Stub-mode tests still green (129+ passing).
- [ ] **Spawn `agent-skills:code-reviewer` (Sonnet)** on the `bedrock_decide.py` + `decision.py` + CDK changes for structural review.
- [ ] Human approval before Task 3.

---

### Phase C — Eval framework

#### Task 3: Eval scaffolding (fixtures dir + judge prompts + runner) + tests

**Description:** Build `evals/` with the structure design-spec §6 prescribes. The runner is local-only (not deployed); it imports `bedrock_decide.decide` from the poller package and runs each fixture through it, then asks Sonnet 4.6 to judge.

**Acceptance criteria:**
- [ ] `evals/run_evals.py` — CLI script: `python evals/run_evals.py [--fixtures-dir ...] [--out ...]`. Reads fixtures, calls `bedrock_decide.decide`, calls Sonnet judge, writes markdown report.
- [ ] `evals/judge_prompts/decision.md` — rubric: pass/fail criteria for whether the model's `{alert, reason}` is reasonable for the case.
- [ ] `evals/fixtures/decision/` — schema documented in `evals/README.md`. Each fixture: `{snapshot, watch, history, expected_alert, expected_reason_themes, notes}`.
- [ ] `evals/README.md` — how to run locally; cost note (~$0.05/run for 30 fixtures); when to re-run.
- [ ] **Tests for the runner itself**: `tests/test_eval_runner.py` (in a new `evals/tests/` dir or in the existing poller tests/) — fixtures-loader correctness, judge-prompt formatting, report writer, exit code on failures.

**Verification:**
- [ ] `python evals/run_evals.py --fixtures-dir <test-fixtures> --out /tmp/test-report.md` exits 0 and produces a valid markdown report (verified by parsing).
- [ ] Eval runner tests pass without making any real Bedrock calls (mocked).
- [ ] `pytest` green.

**Dependencies:** Task 2.

**Files:** New `evals/{run_evals.py, README.md, judge_prompts/decision.md, fixtures/decision/}` (initial 2-3 fixtures committed; full set in T4), `evals/tests/test_eval_runner.py`.

**Estimated scope:** **M** (5–6 new files).

**Multi-model gate:** spawn `agent-skills:test-engineer` (Sonnet) BEFORE writing the runner tests to design the test matrix (especially around what counts as "the runner working" without making real Bedrock calls).

---

### Phase D — Golden set + sample report + ADR + threat model

#### Task 4: Hand-label 30+ golden cases + commit baseline report + ADR 0004 + threat model `[6]`

**Description:** The decision-quality eval needs a real, hand-labelled corpus. 15 alert-worthy cases + 15 not-alert-worthy cases minimum (companion §4.3). Run `run_evals.py` against the corpus to produce a baseline report; commit both. Write ADR 0004 documenting the Bedrock-decision rationale. Append `[6]` to the threat model (or augment `[5]`) covering the new attack surface.

**Acceptance criteria:**
- [ ] `evals/fixtures/decision/` contains ≥30 hand-labelled cases (synthetic but realistic — based on design-spec §3 schema). Each carries `expected_alert` + `expected_reason_themes` (judge guidance).
- [ ] `evals/results/2026-05-10-baseline.md` committed: per-case pass/fail + overall accuracy + judge rationales for failures.
- [ ] `docs/adr/0004-bedrock-decision.md` written: Context = "why use a model at all when gates already filter"; Decision = "the *reason* string is the user value of the email"; Consequences = cost (~$0.30/mo at personal scale), failure modes (defensive fallback), what changes for production.
- [ ] `docs/adr/README.md` index updated: 0004 row flipped from "(planned)" to "Accepted + slice 6".
- [ ] `docs/threat-model.md`: new row(s) covering Bedrock-as-attack-surface — prompt injection from FareHistory's `bestOfferBlob` (hotel name, airline name) into the model context; cost-runaway via the IAM grant; model-output validation as a defence layer.

**Verification:**
- [ ] Manual: `cat docs/adr/0004-bedrock-decision.md` reads as a peer to ADR 0001/0002/0003 (depth, structure, length — not a stub).
- [ ] Manual: threat model new row(s) match the format of `[3]` / `[3b]` / `[5]`.
- [ ] Eval baseline report shows the model gets at least 80% of golden cases right (loose bar — this is the slice-6 baseline; slice 9 can tighten).
- [ ] **Final multi-model gate (sequential, NOT parallel — see memory)**: code-reviewer (Sonnet), then security-auditor (Sonnet), then test-engineer (Sonnet).

**Dependencies:** Task 3.

**Files:** New `docs/adr/0004-bedrock-decision.md`, `evals/fixtures/decision/*.json` (30+), `evals/results/2026-05-10-baseline.md`. Modified: `docs/adr/README.md`, `docs/threat-model.md`.

**Estimated scope:** **L** (the golden-set hand-labelling is the bulk; spec calls it 30-50 cases).

---

#### Checkpoint B — Slice 6 complete

- [ ] All four tasks landed.
- [ ] Three reviewer subagents signed off (sequentially per memory).
- [ ] Launch checklist line for slice 6 in production-readiness companion §5 can be ticked.

---

## 5. Multi-model workflow summary

| Role | Model / Agent | When |
|------|---------------|------|
| Implementation | Opus 4.7 | Each task |
| Test design | `agent-skills:test-engineer` (Sonnet) | Before T1 (parser matrix), before T3 (runner test matrix) |
| Code review | `agent-skills:code-reviewer` (Sonnet) | Checkpoint A, in T4 final gate |
| Security review | `agent-skills:security-auditor` (Sonnet) | After T2 (IAM grant + Bedrock cost surface), in T4 final gate |

**Spawn reviewer subagents SEQUENTIALLY**, not in parallel — per durable memory `feedback_subagents_sequential.md` (parallel reviewers stalled in slice 5 final gate).

Per `feedback_meaningful_tests.md`: every test asserts real behaviour; no placeholder tests, no `does-not-raise` smoke. The Bedrock parser + fallback logic in particular is exactly the place that silent regressions hide.

---

## 6. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Bedrock cost surprise during dev | Med | `BEDROCK_MODE=stub` is the default in tests; live calls only happen via the eval runner. Eval runner cost note in README. |
| Model response drifts (Anthropic updates Haiku) | Med | Pin model ID in env var (`claude-haiku-4-5-20251001`). Eval runner detects drift the next time it's run. |
| Prompt injection via FareHistory `bestOfferBlob` (hotel name, airline name controlled by upstream provider) | Med/Low | Tool results never go back into a system prompt (existing pattern from threat model `[3]`); the model sees this as user-content data, not instructions. The closure-factory pattern (ADR 0001) caps worst case at "misinform the user" not "exfiltrate cross-user data". Add explicit assertion in the prompt-builder tests that the model context contains the values verbatim — a future change that interpolates them into the system prompt would break the test. |
| IAM grant too broad | Med | Resource-scope to the specific model ARN (`arn:aws:bedrock:us-east-1::foundation-model/<model-id>`), not `bedrock:*` and not `Resource: *`. Security-auditor gate at T2 verifies. |
| Eval runner accidentally runs in CI | Low | Not wired to CI in slice 6 (deferred to slice 9 `workflow_dispatch`). README explicitly says "manual only". |
| Defensive fallback masks a real model regression | Low/Med | Fallback logs `model_response_invalid` at WARNING; eval runner separately validates the model output against the expected shape, so silent fallbacks show up in the next eval run as failures. |
| 30 hand-labelled cases too few | Low | Spec calls 30-50 acceptable for v1. Slice 9 can expand. |

---

## 7. Locked decisions

1. **Model:** Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) for the decision; Sonnet 4.6 for the eval judge. Per design-spec §4.
2. **Stub mode:** `BEDROCK_MODE=stub` env var; tests run with this set so no test ever burns real Bedrock.
3. **Model ID env var:** `BEDROCK_MODEL_ID`, default `claude-haiku-4-5-20251001`. Override at deploy with `-c bedrockModelId=...`.
4. **Eval runner:** local-only in slice 6. CI integration in slice 9 (`workflow_dispatch`).
5. **Defensive fallback:** any model failure becomes `{alert: False, reason: "model_response_invalid"|"model_call_failed", bedrock_called: True}`. Conservative: no spurious alerts; metric still fires.
6. **Golden set size:** 30 minimum (15 alert + 15 no-alert) per spec §6 / companion §4.3 lower bound.
7. **IAM grant scope:** resource-scoped to the model ARN, not `bedrock:*`.

---

## 8. Out of scope for slice 6 (explicit)

- Eval-on-CI gating → slice 9
- Notifier / SES → slice 7
- `lastAlertedAt` writeback → slice 7 (ADR 0005)
- Per-user model preference (e.g., "be more conservative for me") → not in v1
- Streaming Bedrock responses → not needed; the response is short
- Model output as a tool call / agent loop → not needed; one-shot decision
- Pre-existing slice-1/2 carryovers (shared JWT secret, agent's missing iat/exp) → slice 9 / ADR 0006

---

## 9. Verification before starting implementation

- [x] Every task has acceptance criteria
- [x] Every task has explicit verification steps
- [x] Task dependencies identified (T1 → T2 → T3 → T4)
- [x] No task touches more than ~6 production files
- [x] Checkpoints exist (A after T2, B after T4)
- [x] Locked decisions captured (§7)
- [ ] **Human has reviewed and approved this plan** ← awaiting
