# Slice 5 — Poller Lambda — Todo

Companion to [`plan.md`](./plan.md). One checklist per task; tick as you go.

## Task 1 — Lambda skeleton + DDB enumeration + CDK shell

- [x] Create `lambdas/poller/` directory with `app.py`, `enumerator.py`, `requirements.txt`, `dev-requirements.txt`
- [x] Create `tests/__init__.py`, `tests/conftest.py` (moto fixture + custom MemoryLogHandler since powertools breaks capsys/caplog)
- [x] `enumerator.iter_active_watches()` — Scan + FilterExpression status=active; handle pagination; clear error on missing env var
- [x] `app.handler()` — call enumerator, log one structured record per watch
- [x] `lib/poller-server.js` — Python 3.13 ARM64 Lambda, X-Ray ACTIVE, env vars, `watchesTable.grantReadData()`, **EventBridge rule disabled**
- [x] Wire construct into `lib/strands-agent-on-lambda-stack.js`
- [x] Tests (9/9 passing):
  - [x] `test_enumerator.py` — active/paused/archived/empty/multi-page/missing-env
  - [x] `test_handler_skeleton.py` — handler emits structured log per active watch + record-shape + count summary + zero case
- [x] `pytest lambdas/poller/tests` green
- [~] `npx cdk synth --quiet` blocked by pre-existing Docker bundling for the agent's deps layer (unrelated to T1); construct loads + `new Stack(...)` succeeds via direct node test
- [x] **Multi-model gate:** spawn `agent-skills:code-reviewer` (Sonnet) on the new files — verdict: fix-then-ship
- [x] Address review findings (env-var guard, dead fallback removed, timeout comment fixed, log-prefix fixture clarified, direct-dict access)
- [ ] Commit

## Task 2 — Internal JWT signer + MCP HTTP client

- [x] `jwt_signer.py` — HS256, sub=`travel-agent`, user_id claim, 5-minute TTL, missing-secret guard
- [x] `mcp_client.py` — JSON-RPC tools/call via `urllib.request`, 15s timeout, **2MB response cap**, **no-redirect handler**
- [x] `McpCallError` exception + handler-side try/except (one bad watch doesn't break the loop)
- [x] Date math helper: `dateWindow → {departDate, returnDate, pax, nights}` for both MCPs (Decimal-safe)
- [x] `app.handler()` extended: per watch, sign JWT once, call both MCPs, log offer counts; endpoints validated at handler entry
- [x] CDK env vars: `JWT_SIGNATURE_SECRET`, `FLIGHTS_MCP_ENDPOINT`, `HOTELS_MCP_ENDPOINT`
- [x] Pass endpoints in from `lib/strands-agent-on-lambda-stack.js`
- [x] Tests (38/38 total):
  - [x] `test_jwt_signer.py` (7) — round-trip + tampered + expired + missing-secret + empty-user + custom-ttl + alg=none rejection
  - [x] `test_mcp_client.py` (16) — success / 4xx / 5xx / timeout / malformed / jsonrpc-error / missing-result / empty-content / payload-not-json + date math (incl. Decimal nights)
  - [x] `test_handler_with_mcp.py` (8) — happy path, error isolation, empty table, **wrong-sub rejection**, **error-body not in log**, **endpoints required**
- [x] `pytest` green
- [x] **Multi-model gate:** spawn `agent-skills:security-auditor` (Sonnet) — verdict fix-then-ship
- [x] Address findings (response cap + redirect block + endpoint validation + body-not-in-log + alg=none/sub-rejection tests). HIGH (shared secret) and MED-3 (agent token TTL) are pre-existing slice-1 issues, deferred to ADR 0006 in slice 9 — already documented in threat model line 64.
- [ ] Commit

## Task 3 — Snapshot composer + FareHistory writer

- [ ] `snapshot.compose_snapshot(watch, flight, hotel)` — cheapest-of-list, full schema, 90d ttl
- [ ] `bestOfferBlob` per design-spec §3 (airline, flightNumber, stops, departDate, returnDate, hotelName, checkin, checkout, bookingDeepLink)
- [ ] `writer.write_snapshot(snapshot)` — put_item on FareHistory
- [ ] `app.handler()` calls compose → write per watch; logs flight/hotel/total
- [ ] CDK: `fareHistoryTable.grantReadWriteData(pollerFn)`
- [ ] **Multi-model gate (test design FIRST):** spawn `agent-skills:test-engineer` (Sonnet) to design snapshot edge cases
- [ ] Tests (built to that design):
  - [ ] `test_snapshot.py` — cheapest selection, empty offers, ttl correctness, bestOfferBlob field-by-field
  - [ ] `test_writer.py` — round-trip compose → write → query
  - [ ] `test_handler_writes_history.py` — moto + canned MCPs, assert FareHistory rows
- [ ] `pytest` green
- [ ] `npx cdk synth --quiet` green

### → Checkpoint A
- [ ] Pipeline runs end-to-end through persistence
- [ ] **Spawn `agent-skills:code-reviewer` (Sonnet) on the full `lambdas/poller/` tree**
- [ ] Address findings
- [ ] Human approval before Task 4
- [ ] Commit

## Task 4 — Gates + decision stub + CloudWatch metrics

- [ ] `gates.py` — three pure functions (dedup, threshold, anomaly) + named constants for thresholds
- [ ] `history_window.py` — `get_window(watch_id, since_iso)` Query helper
- [ ] `decision.py` — stubbed `decide()` returning `{alert: True, reason: "stub"}` post-gates
- [ ] `metrics.py` — powertools Metrics, namespace `TripTracker/Poller`, four metric names
- [ ] `app.handler()` wired through; per-watch decision logged
- [ ] **Multi-model gate (test design FIRST):** spawn `agent-skills:test-engineer` (Sonnet) for gate boundaries
- [ ] Tests (built to that design):
  - [ ] `test_gates.py` — table-driven boundary cases per gate
  - [ ] `test_history_window.py` — Query bounds + ordering
  - [ ] `test_decision.py` — alert / no-alert paths, reason always present
  - [ ] `test_metrics.py` — parse EMF JSON from stdout, assert four names + counts
  - [ ] `test_handler_decides.py` — full pipeline; low total → alerts_sent=1; high total → 0
- [ ] `pytest` green
- [ ] **Multi-model gate:** `agent-skills:code-reviewer` (Sonnet) on the gate logic
- [ ] Commit

## Task 5 — EventBridge enable + ADR 0003 + threat model + e2e

- [ ] `lib/poller-server.js` — enable EventBridge rule, `pollIntervalMinutes` context default 240, Lambda concurrency reservation = 1
- [ ] `docs/adr/0003-sequential-poll-loop.md` — Context/Decision/Consequences peer to ADR 0001/0002
- [ ] `docs/adr/README.md` — flip 0003 status to Accepted + slice 5
- [ ] `docs/threat-model.md` — append `[5] Poller → AWS services + MCPs` section
- [ ] `tests/test_e2e_poll.py` — full slice exercised through one handler invocation; assert FareHistory rows + four metrics + structured logs
- [ ] `pytest` green
- [ ] `npx cdk synth --quiet` green; rule + Lambda target + permission visible

### → Checkpoint B (Slice 5 complete)
- [ ] **Multi-model final gate (parallel — single message, three Agent calls):**
  - [ ] `agent-skills:code-reviewer` (Sonnet) — full slice five-axis review
  - [ ] `agent-skills:security-auditor` (Sonnet) — MCP boundary + secret handling + threat-model section
  - [ ] `agent-skills:test-engineer` (Sonnet) — verify no placeholder tests, gate boundaries covered, e2e proves alert path
- [ ] Address findings
- [ ] Human approval
- [ ] Commit
- [ ] Tick slice 5 row in production-readiness companion §5 launch checklist

---

## Locked decisions (human-approved 2026-05-10)

1. ✅ `pollIntervalMinutes` CDK context exposed, default 240
2. ✅ Lambda `reservedConcurrentExecutions = 1`
3. ✅ `passes_threshold` uses strict `<`
4. ✅ Dashboard JSON deferred to slice 8
5. ✅ `ANOMALY_MEDIAN_DISCOUNT = 0.85`, `DEDUP_DISCOUNT = 0.95` as named constants in `gates.py`
