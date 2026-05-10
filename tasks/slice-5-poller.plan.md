# Implementation Plan — Slice 5: Poller Lambda

**Date:** 2026-05-10
**Status:** Draft (awaiting human review)
**Companion specs:**
- Design: [`docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md`](../docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md) (§5 polling & alert decision)
- Production-readiness: [`docs/superpowers/specs/2026-05-10-trip-tracker-production-readiness.md`](../docs/superpowers/specs/2026-05-10-trip-tracker-production-readiness.md) (§2 slice 5)

---

## 1. Overview

Slice 5 introduces the **Poller Lambda**: a Python 3.13 function fired on an EventBridge cron (default every 4h) that walks every active `Watches` row, calls `flights-mcp` + `hotels-mcp` for the current best combined price, writes a `FareHistory` snapshot, runs the threshold/anomaly/dedup gates, and asks a *stubbed* Bedrock decision whether to alert. The real Bedrock call lands in slice 6; SES email + the `lastAlertedAt` writeback land in slice 7. This slice ends at "decision made, metric emitted."

Production-readiness adds bundled into this slice (per companion §2 row 5):
- Sequential per-watch loop (ADR 0003).
- Per-watch structured JSON logs with `watch_id` and `user_id_prefix` fields.
- X-Ray active on the new Lambda.
- Four CloudWatch metrics via EMF: `watches_polled`, `watches_errored`, `alerts_sent`, `bedrock_decisions_made`.

---

## 2. Architecture decisions

### 2.1 Language: Python (not Node)
Reuses the watches data layer already in `lambdas/travel-agent/watches.py` patterns (boto3 resource API, moto-backed tests, powertools logger). Bedrock call in slice 6 is `boto3` either way; nothing in slice 5 demands JS. Picking Python now avoids a dual-runtime poller in slice 6.

### 2.2 New Lambda directory, **not** colocated with `travel-agent`
The poller has different IAM (Scan on Watches, full read on FareHistory, write on FareHistory; **no S3 sessions, no JWKS verify, no Strands SDK**), different code surface, and different deploy lifecycle. Sharing the Strands-bearing dependencies layer would inflate cold start for nothing. New directory `lambdas/poller/` with its own `requirements.txt` (powertools, boto3, pyjwt — already in the project's stack).

### 2.3 MCP transport: stdlib `urllib.request` + pyjwt
Two HTTPS POSTs per watch with a small JSON body. `urllib.request` is in the Python stdlib (zero new deps), supports timeouts, and is enough. `pyjwt` is already in `lambdas/travel-agent/requirements.txt` so it's a known, version-pinned dep. No httpx, no aiohttp, no extra layer.

### 2.4 Watch enumeration: `Scan` with `FilterExpression status=active`
The Watches table is partitioned by `userId`, so polling all users requires a Scan. Acceptable at personal scale (≤dozens of items). Already documented inline in `lib/data-stores.js` and called out as a future GSI in the production-readiness companion (ADR 0007 backfill in slice 9). Not re-deciding here — using the existing call.

### 2.5 Date selection from a watch's `dateWindow`
Spec §5 says "search current best total" without prescribing a date sweep. For slice 5: pick `earliestDepart` as `departDate`, compute `returnDate = earliestDepart + nights` days. The flexible-window sweep ("cheapest day in the window") is deferred to v1.5. Documented inline in `snapshot.py` so it's obvious.

### 2.6 Combined total = cheapest flight + cheapest hotel
Each MCP returns a list sorted by price; take `offers[0].totalAmount` and `hotels[0].totalAmount`. If either list is empty for a watch, that watch counts as `watches_errored` and is skipped (no FareHistory row, no decision). Logged with reason.

### 2.7 Decision module: stub-only in this slice
`decision.py` exports one function; in slice 5 it's hardcoded to `{"alert": True, "reason": "stub"}` (only invoked if at least one gate passed). Slice 6 swaps the body for a real Bedrock call without changing the call sites or the metric emission. No selector/env-var indirection yet — premature.

### 2.8 Gates as pure functions
`gates.py` exports three pure functions taking dataclass-like dicts and returning bool. Zero I/O. Tested in isolation with table-driven cases. The orchestration in `app.py` composes them; this lets us cover all gate branches with cheap unit tests and keep the integration tests focused on the I/O seams.

### 2.9 Metrics via aws-lambda-powertools `Metrics` (EMF)
Powertools `Metrics` writes Embedded Metric Format JSON to the log stream; CloudWatch parses it server-side. Zero extra IAM, zero extra API calls, zero extra cost vs raw `PutMetricData`. Namespace `TripTracker/Poller`. Metrics are flushed once per Lambda invocation with totals (not per-watch) so dashboards aggregate cleanly.

### 2.10 No `lastAlertedAt` writeback in this slice
Slice 7's ADR 0005 covers post-SES idempotency. In slice 5 the writeback doesn't happen — so on every poll the dedup gate sees `lastAlertedPrice = null` and lets things through. That's fine: there's no email being sent yet, so no spam. The gate code is implemented now (not stubbed) so slice 7 only has to add the *trigger* of the writeback, not the gate itself.

---

## 3. Dependency graph

```
                              ┌──────────────────────────┐
                              │ lib/poller-server.js     │  CDK construct
                              │  (Lambda + IAM + EB rule)│
                              └────────────┬─────────────┘
                                           │ deploys
                                           ▼
┌────────────────────────────────────────────────────────────────────┐
│                       lambdas/poller/app.py                        │
│  Lambda handler: per-Lambda-invocation orchestration               │
└────┬──────────────┬───────────────┬───────────────┬────────────────┘
     │              │               │               │
     ▼              ▼               ▼               ▼
enumerator.py   mcp_client.py   snapshot.py     gates.py
(DDB Scan)      (HTTPS+JWT)     +writer.py      (pure)
                                (FareHistory)       │
                                    │               ▼
                                    │           decision.py
                                    │           (stub returning
                                    │            {alert,reason})
                                    │               │
                                    └──────┬────────┘
                                           ▼
                                       metrics.py
                                       (EMF, 4 metrics)
```

Build order follows the graph bottom-up so each task lands a runnable layer.

---

## 4. Task list (vertical slices through the pipeline)

### Phase A — Walking skeleton

#### Task 1: Lambda skeleton + DDB enumeration + CDK shell

**Description:** Create the `lambdas/poller/` directory with a Python Lambda handler that scans the `Watches` table and logs each active watch. Wire a CDK construct (`lib/poller-server.js`) that provisions the function with X-Ray, env vars, and least-privilege IAM (DDB Scan on Watches only). EventBridge rule is created **disabled** so accidental cost from an early deploy is impossible.

**Acceptance criteria:**
- [ ] `lambdas/poller/app.py` exports `handler(event, context)` that calls `iter_active_watches()` and emits one structured log per watch (`watch_id`, `user_id_prefix`, `destination`).
- [ ] `lambdas/poller/enumerator.py` exposes `iter_active_watches()` returning items with `status="active"` only (paused/archived are filtered).
- [ ] `lib/poller-server.js` provisions a Python 3.13 ARM64 Lambda with `tracing: ACTIVE`, env vars `WATCHES_TABLE_NAME` / `FARE_HISTORY_TABLE_NAME`, IAM grant `watchesTable.grantReadData(pollerFn)` only.
- [ ] EventBridge rule wired to the Lambda but disabled (`enabled: false`) at this task's end.
- [ ] Stack synthesises (`npx cdk synth --quiet` exits 0).

**Verification:**
- [ ] Unit test `tests/test_enumerator.py`: returns active watches; filters out paused; filters out archived; empty when no watches; multi-user pagination handled (insert >1MB worth of items via moto, assert all are returned).
- [ ] Unit test `tests/test_handler_skeleton.py`: handler logs once per active watch with `watch_id` field present in the JSON record (capture stdout, parse, assert).
- [ ] `pytest lambdas/poller/tests` green.
- [ ] `npx cdk synth --quiet` green.

**Dependencies:** None (foundation task).

**Files likely touched:**
- New: `lambdas/poller/app.py`, `lambdas/poller/enumerator.py`, `lambdas/poller/requirements.txt`, `lambdas/poller/dev-requirements.txt`, `lambdas/poller/tests/__init__.py`, `lambdas/poller/tests/conftest.py`, `lambdas/poller/tests/test_enumerator.py`, `lambdas/poller/tests/test_handler_skeleton.py`, `lib/poller-server.js`
- Modified: `lib/strands-agent-on-lambda-stack.js`

**Estimated scope:** **M** (8 new files, 1 modified)

**Multi-model gate:** After implementation, spawn `agent-skills:code-reviewer` (Sonnet) on the new files for a five-axis review. Address findings before moving to Task 2.

---

### Phase B — Talking to MCPs

#### Task 2: Internal JWT signer + MCP HTTP client

**Description:** Add a per-watch MCP client that signs an internal HS256 JWT (matching the format the existing `lambdas/mcp-authorizer/index.js` validates), POSTs JSON-RPC `tools/call` requests to the flights-mcp and hotels-mcp endpoints, and returns parsed responses. Hook it into the handler so each enumerated watch produces one log entry per MCP call (offer count + source).

**Acceptance criteria:**
- [ ] `lambdas/poller/jwt_signer.py` produces tokens that the existing `mcp-authorizer` accepts (verified by importing and running the authorizer's verify path against a signed token in a test).
- [ ] `lambdas/poller/mcp_client.py` exposes `call_flights(endpoint, jwt, args)` and `call_hotels(endpoint, jwt, args)`. Each performs a JSON-RPC `tools/call` for `search_flight_offers` / `search_hotel_offers`, parses the `text` content block, returns the deserialized payload.
- [ ] Both clients enforce a 15s `timeout=` on `urllib.request.urlopen` (Lambda timeout 60s leaves headroom for two MCPs sequentially per watch).
- [ ] Non-200 responses raise `McpCallError` with status + body excerpt; the handler catches and logs without aborting the loop (per ADR 0003 "one bad watch never blocks the others").
- [ ] Handler converts each watch's `dateWindow` into the MCP arg shape correctly (date math: `returnDate = earliestDepart + nights days`).

**Verification:**
- [ ] Unit test `tests/test_jwt_signer.py`: round-trip with the same secret verifies; tampered payload fails; missing secret raises clearly.
- [ ] Unit test `tests/test_mcp_client.py`: success case returns parsed offers (mock `urllib.request.urlopen`); 5xx raises `McpCallError`; timeout raises a clear error; malformed JSON-RPC envelope raises (no silent empty-list).
- [ ] Integration test `tests/test_handler_with_mcp.py`: a `http.server.HTTPServer` thread serves canned MCP responses, the handler runs against it for 3 active watches and emits 6 successful MCP-call logs (3 watches × 2 MCPs).
- [ ] `pytest` green.

**Dependencies:** Task 1.

**Files likely touched:**
- New: `lambdas/poller/jwt_signer.py`, `lambdas/poller/mcp_client.py`, `lambdas/poller/tests/test_jwt_signer.py`, `lambdas/poller/tests/test_mcp_client.py`, `lambdas/poller/tests/test_handler_with_mcp.py`
- Modified: `lambdas/poller/app.py`, `lambdas/poller/requirements.txt`, `lib/poller-server.js` (add `JWT_SIGNATURE_SECRET`, `FLIGHTS_MCP_ENDPOINT`, `HOTELS_MCP_ENDPOINT` env vars), `lib/strands-agent-on-lambda-stack.js` (pass endpoints in)

**Estimated scope:** **M** (5 new + 4 modified)

**Multi-model gate:** Spawn `agent-skills:security-auditor` (Sonnet) on the JWT signer + MCP client — this is the new trust boundary the poller adds. Address findings before Task 3.

---

### Phase C — Persisting snapshots

#### Task 3: Snapshot composer + FareHistory writer

**Description:** Combine the cheapest flight offer + cheapest hotel offer from MCP responses into a `FareHistory` row matching design-spec §3 schema (incl. `bestOfferBlob` denormalisation and 90-day TTL). Persist via `put_item`. Hook into the handler so each successful watch produces a written row.

**Acceptance criteria:**
- [ ] `lambdas/poller/snapshot.py` exposes `compose_snapshot(watch, flight_response, hotel_response) -> dict | None` returning the FareHistory row shape, or `None` if either side has zero offers.
- [ ] `bestOfferBlob` includes: `airline`, `flightNumber`, `stops`, `departDate`, `returnDate`, `hotelName`, `checkin`, `checkout`, `bookingDeepLink` — exactly the fields named in design-spec §3.
- [ ] `ttl` is set to `int(now.timestamp()) + 90*86400`.
- [ ] `lambdas/poller/writer.py` exposes `write_snapshot(snapshot)` that does the put_item; idempotency at the `(watchId, timestamp)` key.
- [ ] Handler calls compose → write per watch; logs `flight_total`, `hotel_total`, `total_price` per success.
- [ ] CDK grant: `fareHistoryTable.grantReadWriteData(pollerFn)` added.

**Verification:**
- [ ] Unit test `tests/test_snapshot.py`: cheapest-of-list selection (multi-offer ordering); empty offers → returns None; ttl is 90d ahead ±5s of now; bestOfferBlob field-by-field assertion against a synthetic offer pair.
- [ ] Unit test `tests/test_writer.py`: round-trip — compose → write → query, asserts the row materialises with the right `watchId`/`timestamp` PK and the full `bestOfferBlob`.
- [ ] Integration test `tests/test_handler_writes_history.py`: poller runs against moto'd tables + canned MCPs; assert FareHistory has one row per active watch with sane totals.
- [ ] `pytest` green; `npx cdk synth` green.

**Dependencies:** Task 2.

**Files likely touched:**
- New: `lambdas/poller/snapshot.py`, `lambdas/poller/writer.py`, `lambdas/poller/tests/test_snapshot.py`, `lambdas/poller/tests/test_writer.py`, `lambdas/poller/tests/test_handler_writes_history.py`
- Modified: `lambdas/poller/app.py`, `lib/poller-server.js`

**Estimated scope:** **M** (5 new + 2 modified)

**Multi-model gate:** Spawn `agent-skills:test-engineer` (Sonnet) before writing the test files to design the table-driven cases (esp. snapshot edge cases: ties, missing fields in MCP response, currency mismatch defensive guard). Then implement to that design. Optional code-reviewer pass on the writer.

---

#### Checkpoint A — Pipeline through persistence

After Task 3:
- [ ] Handler runs end-to-end against moto + canned MCPs and writes FareHistory rows.
- [ ] All unit + integration tests in `lambdas/poller/tests/` pass.
- [ ] `npx cdk synth --quiet` produces no diff except the expected new construct + IAM grants.
- [ ] **Spawn `agent-skills:code-reviewer` (Sonnet) on the full `lambdas/poller/` tree** for a structural review before adding gate logic. Confirm the I/O seams are testable in isolation, the error handling is consistent, and the structured-log fields match the production-readiness spec (§3.2).
- [ ] Human approval to proceed.

---

### Phase D — The decision

#### Task 4: Gates + decision stub + CloudWatch metrics

**Description:** Add the three gate functions (dedup, threshold, anomaly), the stubbed decision function, and the CloudWatch EMF metric emission. Wire them into the handler so each watch ends with: gate evaluation → (if any gate passes) decision call → metric emission. Slice 5 stops here — no SES, no `lastAlertedAt` writeback.

**Acceptance criteria:**
- [ ] `lambdas/poller/gates.py` exposes three pure functions:
  - `is_dedup_eligible(snapshot, watch) -> bool` — True if `watch["lastAlertedPrice"]` is None, else True iff new total ≤ 0.95 × `lastAlertedPrice`.
  - `passes_threshold(snapshot, watch) -> bool` — True iff `total_price < watch["maxTotalPrice"]`.
  - `is_anomaly(snapshot, history) -> bool` — True iff `total_price ≤ 0.85 × median(history.totalPrice)` OR `total_price < min(history.totalPrice)`. Returns False on empty history.
- [ ] `lambdas/poller/decision.py` exposes `decide(snapshot, watch, history) -> dict` returning `{"alert": True, "reason": "stub"}` when at least one of (threshold, anomaly) passes after dedup, else `{"alert": False, "reason": "no_gate_passed"}`. Stubbed body is documented as slice 6's seam.
- [ ] `lambdas/poller/metrics.py` exports a powertools `Metrics` instance with namespace `TripTracker/Poller` and four metric names: `watches_polled`, `watches_errored`, `alerts_sent`, `bedrock_decisions_made`. Counters are reset per Lambda invocation.
- [ ] Handler increments `bedrock_decisions_made` once per watch reaching `decide()`; `alerts_sent` once per `alert: True`; `watches_polled` once per attempt; `watches_errored` once per skipped watch.
- [ ] A 30-day `FareHistory` window helper is added (Query with `KeyCondition: watchId = :w AND timestamp >= :since`). Used by `decide()`'s `history` argument. Lives in `lambdas/poller/history_window.py` (small, single-purpose).

**Verification:**
- [ ] Unit test `tests/test_gates.py`: table-driven cases for each gate covering: positive, negative, exactly-at-boundary (e.g., total = 0.95 × lastAlertedPrice → False; 0.949× → True), empty history (anomaly False), single-row history (median = that row), missing field (defensive None handling).
- [ ] Unit test `tests/test_decision.py`: decision returns alert when threshold passes; returns no-alert when only dedup blocks; returns no-alert when no gate passes; reason field always present and non-empty.
- [ ] Unit test `tests/test_metrics.py`: capture stdout, parse the EMF JSON, assert all four metric names appear with correct dimensions and counts after a synthetic invocation.
- [ ] Unit test `tests/test_history_window.py`: query returns rows newer than `since`, excludes rows older, sorted descending.
- [ ] Integration test `tests/test_handler_decides.py`: full pipeline with a mocked MCP that returns a low total → assert `alerts_sent` metric = 1, decision logged with `alert=True, reason="stub"`. Variant with high total → `alerts_sent = 0`, `bedrock_decisions_made = 0`.
- [ ] `pytest` green.

**Dependencies:** Task 3.

**Files likely touched:**
- New: `lambdas/poller/gates.py`, `lambdas/poller/decision.py`, `lambdas/poller/metrics.py`, `lambdas/poller/history_window.py`, `lambdas/poller/tests/test_gates.py`, `lambdas/poller/tests/test_decision.py`, `lambdas/poller/tests/test_metrics.py`, `lambdas/poller/tests/test_history_window.py`, `lambdas/poller/tests/test_handler_decides.py`
- Modified: `lambdas/poller/app.py`

**Estimated scope:** **M** (9 new + 1 modified)

**Multi-model gate:** Spawn `agent-skills:test-engineer` (Sonnet) BEFORE writing tests — gate boundaries are exactly the kind of place silent off-by-ones hide. Then `agent-skills:code-reviewer` after implementation.

---

### Phase E — Wiring + ADR + threat model + e2e

#### Task 5: Enable EventBridge schedule + ADR 0003 + threat model + slice e2e

**Description:** Enable the EventBridge rule (every 4h, configurable via CDK context `pollIntervalMinutes`), write ADR 0003 documenting the sequential per-watch loop choice, append a `[5]` boundary to the threat model for the poller, and add one end-to-end test that exercises the whole slice through a single handler invocation.

**Acceptance criteria:**
- [ ] `lib/poller-server.js` enables the EventBridge rule and reads `pollIntervalMinutes` from CDK context with a 240-minute default.
- [ ] `docs/adr/0003-sequential-poll-loop.md` written following the existing Context/Decision/Consequences template (see ADR 0001/0002).
- [ ] `docs/adr/README.md` updated: row 0003 status flipped from "(planned)" to "Accepted" + slice 5.
- [ ] `docs/threat-model.md` adds a `[5] Poller → AWS services + MCPs` section covering: secret reuse (`JWT_SIGNATURE_SECRET` shared with agent — same env-var, same rotation), one-bad-watch-isolation as a security-relevant property (a malicious tool response from one watch can't crash the loop and starve others), no user input reaches the poller (cron-triggered, no event payload trusted).
- [ ] CloudWatch dashboard JSON committed at `infra/dashboards/poller.json` showing the four metrics (per companion §2 row 8 — slice 8 covers the full dashboard, but slice 5 seeds the poller widgets so they exist by the time the dashboard is wired up). Alternative: defer to slice 8. **Plan default: defer; remove this AC.**
- [ ] One e2e test `tests/test_e2e_poll.py` runs the handler with: 3 active + 1 paused watch in moto, in-process HTTP mock MCPs returning fixture-shape data, and asserts:
  - 3 FareHistory rows written
  - 4 metrics emitted with expected values (`watches_polled=3`, `errored=0`, the rest depend on threshold)
  - Handler returns successfully (no raised exception)
- [ ] `npx cdk synth --quiet` shows the `Schedule.rate(Duration.minutes(240))` rule + Lambda target + permission.

**Verification:**
- [ ] All unit + integration + e2e tests in `lambdas/poller/tests/` pass.
- [ ] `npx cdk synth --quiet` exits 0 with no warnings.
- [ ] Manual check: `cat docs/adr/0003-sequential-poll-loop.md` reads as a peer to ADR 0001/0002 (length, structure, depth — not a stub).
- [ ] Manual check: threat model `[5]` section reads as a peer to `[3]` and `[3b]` (table format, real threats, real mitigations).
- [ ] **Final multi-model gate (parallel):**
  - `agent-skills:code-reviewer` (Sonnet) — five-axis review of the slice as a whole.
  - `agent-skills:security-auditor` (Sonnet) — focused on the new MCP trust boundary, secret handling, and the threat-model addition.
  - `agent-skills:test-engineer` (Sonnet) — verifies test suite is meaningful (no placeholders), covers the gate boundaries, and has at least one e2e that proves the alert path end-to-end.
- [ ] Human approval before commit.

**Dependencies:** Task 4.

**Files likely touched:**
- New: `docs/adr/0003-sequential-poll-loop.md`, `lambdas/poller/tests/test_e2e_poll.py`
- Modified: `lib/poller-server.js`, `docs/adr/README.md`, `docs/threat-model.md`

**Estimated scope:** **M** (2 new + 3 modified)

---

#### Checkpoint B — Slice 5 complete

- [ ] All five tasks landed.
- [ ] Three reviewer subagents (code-reviewer, security-auditor, test-engineer) signed off in the final gate.
- [ ] Launch checklist line for slice 5 in production-readiness companion §5 can be ticked.
- [ ] One commit per task (clean history per `git-workflow-and-versioning` skill).

---

## 5. Multi-model workflow summary

Per durable feedback in memory (`feedback_multi_model_workflow.md`):

| Role                | Model / Agent                              | When                                                    |
|---------------------|--------------------------------------------|---------------------------------------------------------|
| Implementation      | Opus 4.7 (this session)                    | Each task                                               |
| Test design         | `agent-skills:test-engineer` (Sonnet)      | Before T3 and T4 (gate boundaries + edge cases)         |
| Code review         | `agent-skills:code-reviewer` (Sonnet)      | After T1, after T3 checkpoint, parallel in T5 final gate |
| Security review     | `agent-skills:security-auditor` (Sonnet)   | After T2 (JWT + new trust boundary), parallel in T5     |

Subagents run in parallel where independent (single message, multiple Agent calls).

Per durable feedback in memory (`feedback_meaningful_tests.md`): every test in this slice must assert real behaviour. Specifically: no `assert True`, no "imports successfully", no e2e test that only checks the handler returned. The acceptance criteria above name the assertion targets.

---

## 6. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| `Scan` on Watches grows unbounded | Med (cost, latency) | Documented in `data-stores.js`; ADR 0007 backfill in slice 9 adds status GSI option. Personal-scale OK. |
| MCP latency × N watches × 2 calls > Lambda timeout | Med (poll silently drops watches) | Sequential loop with per-watch try/except; `urlopen(timeout=15)`; Lambda timeout 60s. At 10 watches × 2 MCPs × 15s worst-case = 300s — a real risk if all hang. Mitigation: pick Lambda timeout = `15 × 2 × N + 30s`, scale with watch count via CDK context. Document in ADR 0003 alternatives. |
| ISO timestamp string ordering ≠ chronological | Low | ISO 8601 with timezone IS lexicographically sortable — use `datetime.now(timezone.utc).isoformat()` (existing `_now_iso()` pattern). Test asserts ordering. |
| `pyjwt` version drift between travel-agent + poller | Low | Pin to the exact version in `travel-agent/requirements.txt` (2.10.1); add a CI check in slice 9 |
| Poller logs leak `JWT_SIGNATURE_SECRET` via stack trace | Low/High | Same env-var hygiene as flights-mcp; never log the secret directly; security-auditor gate in T2 confirms |
| EventBridge invocations overlap if poll runs > schedule interval | Low at 4h cadence | Lambda concurrency reservation = 1 (add to CDK construct); document in ADR 0003 |
| Bedrock IAM grant added prematurely (slice 5 stubs the call) | Low | Do not grant `bedrock:InvokeModel` in slice 5 — slice 6 adds it. Keeps least-privilege real. |

---

## 7. Locked decisions (human-approved 2026-05-10)

1. **Cadence:** `pollIntervalMinutes` CDK context exposed in T1's construct, **default 240**. Dev deploys can override (e.g. `cdk deploy -c pollIntervalMinutes=15`).
2. **Dashboard JSON:** Deferred to slice 8. Not in T5.
3. **Concurrency:** Lambda `reservedConcurrentExecutions = 1` set in T1's construct. Prevents overlapping polls.
4. **`maxTotalPrice` interpretation:** Strict `<`. Documented in `passes_threshold` docstring.
5. **Anomaly thresholds:** named constants in `gates.py` — `ANOMALY_MEDIAN_DISCOUNT = 0.85`, `DEDUP_DISCOUNT = 0.95`. Tunable without touching gate logic.

---

## 8. Out of scope for slice 5 (explicit)

These belong to later slices, do not implement now:

- Real Bedrock decision call (slice 6).
- Eval golden set / `evals/` directory (slice 6).
- Notifier Lambda + SES (slice 7).
- `lastAlertedAt`/`lastAlertedPrice` writeback (slice 7, ADR 0005).
- ADR 0004 (Bedrock decision rationale) (slice 6).
- ADR 0007 (Watches table without status GSI) (slice 9 backfill).
- CloudWatch dashboard JSON (slice 8).
- Cleanup of `bookings-mcp` (slice 8).

---

## 9. Verification before starting implementation

Per the planning skill checklist:
- [x] Every task has acceptance criteria
- [x] Every task has explicit verification steps with named test files / commands
- [x] Task dependencies are identified and ordered correctly (T1 → T2 → T3 → T4 → T5)
- [x] No task touches more than ~10 files
- [x] Checkpoints exist (Checkpoint A after T3, Checkpoint B after T5)
- [x] Open questions surfaced and answered (§7)
- [x] **Human has reviewed and approved this plan** (2026-05-10)
