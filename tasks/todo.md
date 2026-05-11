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

- [x] `snapshot.compose_snapshot(watch, flight, hotel)` — cheapest-of-list w/ deterministic id-tiebreaker, USD-only guard (raises on non-USD), zero-price exclusion, 90d ttl from frozen-clock-friendly `_now()` seam
- [x] `bestOfferBlob` per design-spec §3 (airline, flightNumber, stops, departDate, returnDate, hotelName, checkin, checkout, bookingDeepLink)
- [x] `bookingDeepLink` validated: 2KB cap + https-only scheme + null/missing → empty string
- [x] `writer.write_snapshot(snapshot)` — put_item on FareHistory; idempotent at (watchId, timestamp)
- [x] `app.handler()` calls compose → write per watch; logs `snapshot_written` / `snapshot_skipped`; per-watch try/except now also catches ValueError + KeyError
- [x] CDK: `fareHistoryTable.grantReadWriteData(pollerFn)` + new `lambdaTimeoutSeconds` context override
- [x] **Multi-model gate (test design FIRST):** `agent-skills:test-engineer` (Sonnet) designed 17+5 tests with explicit edge cases
- [x] Tests (built to that design — 69/69 total):
  - [x] `test_snapshot.py` (22) — cheapest+tiebreaker / empty / ttl / iso timestamp / sort order / blob field set / stops / non-USD / zero-price / deep-link validation (5 cases) / malformed offer
  - [x] `test_writer.py` (6) — round-trip PK / blob field-by-field / Decimal price round-trip / TTL number type / idempotency via Query / missing-env fail-loud
  - [x] `test_handler_writes_history.py` (3) — 3-watch happy path with exact totals / empty-flights soft skip / non-USD → watch_errored
- [x] `pytest` green (69/69)
- [~] `npx cdk synth` blocked by pre-existing Docker bundling (same as T1)

### → Checkpoint A
- [x] Pipeline runs end-to-end through persistence (verified by `test_three_active_watches_produce_three_fare_history_rows`)
- [x] **Spawned `agent-skills:code-reviewer` (Sonnet) on the full `lambdas/poller/` tree** — verdict: fix-then-checkpoint
- [x] Address findings:
  - BLOCKER 1: stale `_poll_one` docstring → updated to T3 reality
  - BLOCKER 2: `bookingDeepLink` injection path → added `_validate_deep_link` (2KB + https-only) + 5 new tests
  - MAJOR: Lambda timeout vs N watches → new `lambdaTimeoutSeconds` CDK context override
  - MAJOR: idempotency test used wrong scan filter form → switched to Query w/ `Key().eq()`
  - MAJOR: `JWT_SIGNATURE_SECRET` carryover → TODO comment in stack pointing to ADR 0006 + threat model
  - MAJOR: `_now` undocumented test seam → docstring added
  - NIT: vestigial `time` re-export comment in mcp_client.py → removed
  - NIT: `bookingDeepLink: null` could become `None` in DDB → guarded with `or ""`
- [ ] Human approval before Task 4
- [ ] Commit

## Task 4 — Gates + decision stub + CloudWatch metrics

- [x] `gates.py` — three pure functions (dedup strict `<`, threshold strict `<`, anomaly = `≤` median branch OR `<` new-low) + `DEDUP_DISCOUNT=0.95` / `ANOMALY_MEDIAN_DISCOUNT=0.85` constants
- [x] `history_window.py` — `get_window(watch_id, since_iso)` Query helper, exclusive `>` boundary, **paginated for safety**
- [x] `decision.py` — stubbed `decide()` returning `{alert: True, reason: "stub"}` post-gates, otherwise `{alert: False, reason: <gate>}`
- [x] `metrics.py` — powertools Metrics, namespace `TripTracker/Poller`, four metric names + `increment` helper
- [x] `app.handler()` wired through: history fetched BEFORE write (exclusive `>` boundary naturally excludes the new row); per-watch decision logged; metrics flushed at end
- [x] **Multi-model gate (test design FIRST):** `agent-skills:test-engineer` (Sonnet) designed 20+7+6+7+6 = 46 tests with explicit boundary cases
- [x] Tests (built to that design):
  - [x] `test_gates.py` (21) — boundary at exactly 0.95×, 0.85×, equal-to-min, even/odd median, defensive missing-field
  - [x] `test_history_window.py` (7) — boundary exclusivity, descending order, scoped-to-watch, missing-env guard
  - [x] `test_decision.py` (7) — gate routing matrix, parametrized scenarios, no-Bedrock-in-stub
  - [x] `test_metrics.py` (6) — namespace, all four names, counts, reset-between-flush, omit-zero behaviour
  - [x] `test_handler_decides.py` (6) — low/high totals, empty history, dedup blocks, count semantics, MCP error increments errored
- [x] `pytest` green (120/120)
- [x] **Multi-model gate:** `agent-skills:code-reviewer` (Sonnet) — verdict request changes
- [x] Address findings: paginated `get_window`, restructured handler so `cutoff` is computed before write (eliminates fragile equality filter), clarified comment in `passes_threshold` about None handling, fixed misleading test comment
- [ ] Commit

## Task 5 — EventBridge enable + ADR 0003 + threat model + e2e

- [x] `lib/poller-server.js` — EventBridge rule `enabled: true`; `pollIntervalMinutes` context default 240 (clamped [15,1440]); `lambdaTimeoutSeconds` clamped [30,300]; Lambda concurrency reservation = 1
- [x] `docs/adr/0003-sequential-poll-loop.md` — Context/Decision/Consequences peer to ADR 0001/0002
- [x] `docs/adr/README.md` — 0003 row flipped to Accepted + slice 5
- [x] `docs/threat-model.md` — `[5] Poller → AWS services + MCPs` section appended; change-log updated; shared-sub framing made explicit per security-auditor
- [x] `tests/test_e2e_poll.py` — full slice exercised through one handler invocation; asserts FareHistory rows, all four metrics with exact counts, per-watch decision logs, structured-log fields, dedup-blocked + anomaly + threshold + no-gate paths
- [x] `pytest` green (129/129)
- [~] `npx cdk synth --quiet` blocked by pre-existing Docker bundling for the agent's deps layer (unrelated to T5); construct loads + stack constructs cleanly via direct node test

### → Checkpoint B (Slice 5 complete)
- [x] **Multi-model final gate** — sequential after parallel attempt failed at network/watchdog layer:
  - [x] `agent-skills:code-reviewer` (Sonnet) — APPROVE; addressed `bedrock_decisions_made` semantic mismatch (now uses `decision["bedrock_called"]` flag), ADR typo, deep-link byte/char message
  - [x] `agent-skills:security-auditor` (Sonnet) — fix-then-ship; addressed CDK context value clamps, missing-currency strict failure, threat-model framing
  - [x] `agent-skills:test-engineer` (Sonnet) — solid; added 5 constant-pinning tests (`DEDUP_DISCOUNT`, `ANOMALY_MEDIAN_DISCOUNT`, `TTL_DAYS`, `MAX_DEEP_LINK_BYTES`, `MAX_RESPONSE_BYTES`, `MCP_TIMEOUT_SECONDS`, `ANOMALY_WINDOW_DAYS`)
- [x] Address findings (all in-scope; HIGH carryover for shared JWT secret + MED-3 carryover for agent JWT iat/exp remain deferred to ADR 0006 in slice 9)
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
