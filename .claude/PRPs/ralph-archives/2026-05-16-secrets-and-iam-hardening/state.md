---
iteration: 1
max_iterations: 10
plan_path: "tasks/secrets-and-iam-hardening.prp.md"
input_type: "plan"
started_at: "2026-05-16T00:22:22Z"
---

# PRP Ralph Loop State

## Codebase Patterns
(Consolidate reusable patterns here — future iterations read this first)

- Construct test synth MUST pass `'aws:cdk:bundling-stacks': []` in App context to skip Docker bundling (memory `project_cdk_test_invocation_gotchas`).
- Python tests run via `.venv-tests/Scripts/python.exe -m pytest`.
- Pytest for a lambda package with `tests/conftest.py` runs from the package root: `cd lambdas/<pkg> && pytest tests/ -q` (NOT repo-root).
- flights-mcp / hotels-mcp / mcp-authorizer use Node's built-in runner: `cd lambdas/<pkg> && npm test` (`node --import ./tests/setup.js --test`), NOT jest. CDK construct tests use jest from repo root.
- All numeric DDB-bound fields use `Decimal(str(value))`, never `Decimal(float)`.
- Bedrock/secret-fetch is LAZY cached-on-first-use, never import-time (so tests can stub the client).
- The two-secret + sub-coupling JWT verifier is TRIPLICATED verbatim across mcp-authorizer/index.js + flights-mcp/index.js + hotels-mcp/index.js (not a shared module — PRP §9 #11, §10).

## Current Task
Execute `tasks/secrets-and-iam-hardening.prp.md` (12 tasks, §0 adversarial-review-revised) and iterate until all 7 validation gates pass.

## Plan Reference
tasks/secrets-and-iam-hardening.prp.md

## Instructions
1. Read the plan file (esp. §0 review table, §7 patterns, §8 files, §9 locked decisions, §14 step-by-step, §12 gates).
2. Implement all incomplete tasks (Task 1 → 12, including Task 5b + Task 11).
3. Run ALL 7 validation gates from §12.
4. If any gate fails: fix and re-validate.
5. Update the plan file: mark completed tasks, add notes.
6. When ALL 7 gates pass: output <promise>COMPLETE</promise>.

Hard constraints (PRP §13): zero `slice X` / `T#` / `Task N` / `Checkpoint A-Z` / `phase N` and zero filler (`basically`, `simply`, `obviously`, `essentially`, `merely`, `kind of`) in any NEW source/test/ADR file or commit message. Implementation commits describe intent, not roadmap position.

## Progress Log

### Iteration 1 — 2026-05-16

**Completed:** All 12 tasks (1, 2, 3, 4, 5, 5b, 6, 7, 8, 9, 10, 11).
- `lib/secrets.js` created; stack wired (literal deleted); agent/poller/flights/hotels constructs updated (both server + authorizer get both secret ARNs + grantRead).
- Triplicated two-secret + sub-coupling verifier in mcp-authorizer + flights-mcp + hotels-mcp index.js (lazy `getSecret`, `__seedSecretCacheForTests` seam). mcp-authorizer gained a `node --test` script + setup.js.
- Python: `jwt_signer.py` lazy `_get_secret()` + SUBJECT=trip-tracker-poller; `mcp_client_manager.py` lazy `_get_agent_secret()`; `app.py` dead read deleted; `agent_config.py` reads `AGENT_BEDROCK_MODEL_ID`.
- Agent Bedrock grant = 3 US FM-region ARNs + inference-profile ARN (no `*`).
- ADR 0006 written; threat-model 2 rows flipped + changelog appended; ADR README → Accepted.
- conftest + poller integration mocks updated for the new sub (Task 11).

**Validation status:**
- Gate 1 notifier: 126 PASS
- Gate 2 poller 206 + evals 106 = 312 PASS
- Gate 3: jest test/ A5+B8+C7=20 PASS; mcp-authorizer node --test D 8 PASS; flights 19 PASS; hotels 18 PASS (full `jest test/` regression run in flight)
- Gate 4: filler clean; the only `slice\d` ripgrep hits are `String(...).slice(0, 8)` — a JS String method in PRE-EXISTING untouched code, a known regex false-positive (`[ -_]` range includes `(`). NOT a roadmap label. Intent satisfied.
- Gate 5 full-stack synth IAM: PASS
- Gate 6 threat-model forward-refs: clean
- Gate 7 git grep old secret in lib/+lambdas: clean

**Learnings (codebase patterns added above):** lazy-not-import-time secret fetch; triplicated verifier; `node --test` runners per MCP package; `_secrets=None` lazy boto3 client avoids NoRegionError in unit tests.

**Next:** confirm full `jest test/` regression green → archive + completion. Then PRP §13 sequential 4-reviewer gate (separate phase, gated).

---
