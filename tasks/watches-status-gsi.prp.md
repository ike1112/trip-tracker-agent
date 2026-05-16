# PRP — Watches table status GSI so the poller Queries (ADR 0007)

## 0. Adversarial findings (authoring-time pass; codex critique folds in here)

These were surfaced while grounding the PRP against the real code.
Severity is the cost of getting it wrong at implementation time. The
downstream codex adversarial pass appends below this line; do not
delete pre-baked entries — they are pinned by gates.

| # | Sev | Finding | Mitigation (where) |
|---|-----|---------|--------------------|
| 1 | HIGH | GSI **must project ALL attributes**. `enumerator.iter_active_watches` returns the *whole* row — the poller reads `preferences`, `dateWindow`, `maxTotalPrice`, `alertStrategy`, `lastAlertedAt/Price` downstream (snapshot/gates/MCP compose). `KEYS_ONLY` or `INCLUDE` synthesises and deploys fine, then silently returns truncated rows → the poller composes broken MCP requests or the gates `KeyError` at runtime, not at synth. | §7 pins `projectionType: ddb.ProjectionType.ALL`. Gate 4 asserts `Projection.ProjectionType === 'ALL'`. §14 Task 3 adds a poller test asserting a full fixture row (with `preferences`/`dateWindow`) round-trips through `iter_active_watches` post-rewrite. |
| 2 | MED | `_create_tables` in `lambdas/poller/tests/conftest.py` is shared by **four** fixtures (`enumerator_module`, `app_module`, `history_window_module`, `writer_module`). The moto `WatchesTable` create must add `status` to `AttributeDefinitions` **and** a `GlobalSecondaryIndexes` block, or `table.query(IndexName=…)` raises `ResourceNotFoundException` in moto. Adding the GSI is backward-compatible (base KeySchema/billing unchanged; only the enumerator queries it). | §14 Task 2 updates `_create_tables`. Gate 3 = **full** poller suite green (all four fixtures), not just `test_enumerator.py`. |
| 3 | MED | **Sparse-index semantics.** DynamoDB only projects an item into a GSI if it carries the GSI partition-key attribute. A Watches row written without a `status` attribute is invisible to the poller — and fails *silently* (no error, just never polled). Every current writer sets `status` (chat CRUD tools + `make_watch`), so this holds today, but it is now a hard invariant, not a convenience. | §14 Task 4 records it in ADR Consequences **and** the `data-stores.js` JSDoc as an explicit invariant: "every Watches row MUST carry a `status` attribute or the poller never sees it." No code guard (the schema already guarantees it; a guard would be untestable defensive cruft). |
| 4 | LOW | After the rewrite `enumerator.py` no longer uses `Attr`; it uses `Key`. Leaving `from boto3.dynamodb.conditions import Attr` is dead code the cleanliness gate / a future lint would flag. | §14 Task 1 swaps the import to `Key` (verify no other `Attr` reference remains — there is none). |
| 5 | LOW | GSI Query needs `dynamodb:Query` on the **index ARN** (`<tableArn>/index/status-index`). `lib/poller-server.js:130` uses `props.watchesTable.grantReadData(pollerFn)`; CDK's `grantReadData` already widens the policy `Resource` to include `<tableArn>/index/*`, so **no IAM change is required**. But a future hand-scoped grant would break GSI Query with `AccessDenied` only at runtime. | §14 Task 1 makes **no** IAM change. Gate 5 (new) synthesises the stack and asserts the poller's DDB policy `Resource` list contains an `index/*`-suffixed ARN, locking the invariant so a future regression fails at synth. |
| 6 | LOW | A GSI cannot be queried with `ConsistentRead=True` (strongly-consistent reads are unsupported on GSIs — boto3 raises `ValidationException`). GSIs are also eventually consistent: a just-created "active" watch may miss the *immediately* following poll tick. | §14 Task 1 must **not** pass `ConsistentRead`. ADR Consequences documents the one-cycle latency as acceptable (the poller is scheduled; a brand-new watch waiting one tick is harmless and strictly cheaper than Scan-all). |
| 7 | LOW | Query still 1 MB-paginates exactly like Scan — the `LastEvaluatedKey`/`ExclusiveStartKey` loop is unchanged. The existing `test_paginates_when_scan_returns_last_evaluated_key` name will be stale ("scan"). Renaming it is a durable-artifact accuracy fix, not optional. | §14 Task 3 renames it to `test_paginates_when_query_returns_last_evaluated_key` and updates the conftest/test docstrings that say "Scan". |
| 8 | INFO (honest ceiling) | A `status`-only GSI partition key has very low cardinality — every active row shares **one** GSI partition. At scale this is a hot partition; it is *less wrong* than Scan-all, not the production-correct design (which would shard the GSI PK or use a different access pattern). | ADR Decision/Consequences states this explicitly and scopes the decision to personal scale — same honesty framing as the original Scan decision in `data-stores.js`. Not a defect; a documented boundary. |

Net: no behaviour change to *what* the poller yields (still every
`status=="active"` row, full attributes, paginated). The change is the
read path (Scan+filter → Query on GSI), its cost profile, and the
honest documentation of the new sparse-index invariant and the
status-PK cardinality ceiling.

---

## 1. Summary

Closes ADR 0007 (`docs/adr/README.md:14`, Status: Planned). The poller
enumerates active watches every tick. Today that is a `Scan` of the
whole `Watches` table with a server-side `FilterExpression` on
`status` — DynamoDB reads (and bills) **every row** every cycle, then
discards non-active ones after the 1 MB page cap. Add a Global
Secondary Index keyed on `status` and rewrite `enumerator.py` to
`Query` `status == "active"` directly. Author `docs/adr/0007-…`, flip
the two forward-references ("planned"/"add a GSI") to backward
references to the built index, and pin the contract with a jest
construct test (GSI shape + poller-grant ARN) and poller pytest
(Query-not-Scan, full-row projection, pagination still correct).

## 2. User story

As the operator of trip-tracker, I want the poller to read only
active watches instead of scanning the entire table each tick, so
that poll cost stays proportional to *active* watches (not total
historical rows) and a table that accumulates paused/archived watches
does not linearly inflate every poll's RCU bill.

## 3. Problem statement (testable)

`enumerator.iter_active_watches` calls `Table.scan(FilterExpression=…)`.
A Scan reads the entire table; the filter is applied *after* the 1 MB
page cap. With N total rows and A active (A ≤ N), cost is O(N) per
tick regardless of A. After this change the read is `Table.query(
IndexName="status-index", KeyConditionExpression=Key("status").eq(
"active"))` — cost O(A), and the test suite proves Query (not Scan) is
the call made, the full row is still returned, and pagination still
follows `LastEvaluatedKey`.

## 4. Solution statement

One `addGlobalSecondaryIndex` on the existing `WatchesTable`
(`indexName: 'status-index'`, PK `status` STRING, **ProjectionType
ALL**, inherits PAY_PER_REQUEST — no throughput block). Rewrite the
enumerator's import (`Attr`→`Key`) and the read loop (`scan`→`query`
with `IndexName`), preserving the exact `LastEvaluatedKey` pagination.
No IAM change (`grantReadData` already covers `index/*`). ADR + doc
flips + tests.

## 5. Metadata

| Field | Value |
|---|---|
| Type | ENHANCEMENT (read-path optimisation; ADR-worthy) |
| Complexity | LOW–MED (one GSI, one ~6-line read-loop rewrite, shared test fixture, ADR) |
| Systems affected | `lib/data-stores.js`, `lambdas/poller/enumerator.py`, `lambdas/poller/tests/conftest.py`, `lambdas/poller/tests/test_enumerator.py`, `docs/adr/`, `docs/adr/README.md` |
| Dependencies | None new. `aws-cdk-lib/aws-dynamodb`, `boto3.dynamodb.conditions.Key`, `moto` GSI support (already a dep) |
| Estimated tasks | 6 |

---

## 6. UX / data-flow transformation

```
BEFORE  (every tick)
  EventBridge → poller.handler → iter_active_watches()
                                   │
                                   ▼
                 Watches.scan(FilterExpression=status=="active")
                  ├─ reads ALL N rows, 1MB pages
                  ├─ filters AFTER the page cap (a page can be empty)
                  └─ loop on LastEvaluatedKey
  cost: O(N total rows)  ── paused/archived rows billed every cycle

AFTER  (every tick)
  EventBridge → poller.handler → iter_active_watches()
                                   │
                                   ▼
   Watches.query(IndexName="status-index",
                 KeyConditionExpression=Key("status").eq("active"))
                  ├─ reads ONLY the A active rows (GSI partition)
                  ├─ full row (Projection=ALL) — no second fetch
                  └─ loop on LastEvaluatedKey  (unchanged)
  cost: O(A active rows)   ── eventual-consistency: new watch may
                              wait one tick (documented, acceptable)
```

No change to the iterable contract: same rows, same full attributes,
same transparent pagination. Only the engine and its cost change.

---

## 7. The exact GSI + query shapes (pin these)

**CDK (`lib/data-stores.js`, after the `WatchesTable` `new ddb.Table`):**

```js
this.watchesTable.addGlobalSecondaryIndex({
    indexName: 'status-index',
    partitionKey: { name: 'status', type: ddb.AttributeType.STRING },
    projectionType: ddb.ProjectionType.ALL,
});
```

No sort key (poll wants *all* active rows, any order). No
`readCapacity`/`writeCapacity` (table is PAY_PER_REQUEST; a capacity
block on an on-demand-table GSI is a synth error).

**Synthesised CloudFormation shape Gate 4 asserts** (`AWS::DynamoDB::Table`,
the WatchesTable logical id):

```
GlobalSecondaryIndexes: [ {
  IndexName: 'status-index',
  KeySchema: [ { AttributeName: 'status', KeyType: 'HASH' } ],
  Projection: { ProjectionType: 'ALL' },
} ]
AttributeDefinitions: must also contain { AttributeName: 'status', AttributeType: 'S' }
```

**enumerator query (`lambdas/poller/enumerator.py`):**

```python
from boto3.dynamodb.conditions import Key   # was: Attr
...
kwargs = {
    "IndexName": "status-index",
    "KeyConditionExpression": Key("status").eq("active"),
}
while True:
    resp = _watches_table.query(**kwargs)        # was: .scan(**kwargs)
    for item in resp.get("Items", []):
        yield item
    last_key = resp.get("LastEvaluatedKey")
    if not last_key:
        return
    kwargs["ExclusiveStartKey"] = last_key
```

No `ConsistentRead` (invalid on a GSI — §0 #6). The `_watches_table is
None` guard and its `EnvironmentError` are unchanged.

**moto fixture (`conftest.py` `_create_tables`, the `WATCHES_TABLE`
`create_table`):** add `{"AttributeName": "status", "AttributeType":
"S"}` to `AttributeDefinitions` and:

```python
GlobalSecondaryIndexes=[{
    "IndexName": "status-index",
    "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
    "Projection": {"ProjectionType": "ALL"},
}],
```

(PAY_PER_REQUEST table → no `ProvisionedThroughput` on the GSI in moto.)

---

## 8. Mandatory reading (implementer reads before touching code)

| P | File | Lines | Why |
|---|------|-------|-----|
| P0 | `lib/data-stores.js` | 1-73 | The table to extend; JSDoc 22-27 is the forward-ref to flip; mirror the comment style. |
| P0 | `lambdas/poller/enumerator.py` | 1-52 | The rewrite target; module + inner docstrings to flip. |
| P0 | `lambdas/poller/tests/conftest.py` | `_create_tables`, `enumerator_module` | Shared fixture; the GSI must be added here (§0 #2). |
| P0 | `lambdas/poller/tests/test_enumerator.py` | all | Existing coverage to extend + the stale "scan" name to rename. |
| P1 | `docs/adr/0005-after-ses-idempotency.md` | all | ADR format to mirror exactly (Date/Status/Context/Decision/Consequences Good·Cost·Not-chosen). |
| P1 | `docs/adr/0006-per-component-jwt-secrets.md` | headers + `## Closes` | Confirms the `## Closes` trailer convention. |
| P1 | `test/observability-dashboard.test.js` | all | Jest `Template.fromStack` + `hasResourceProperties` pattern to mirror for the new construct test. |
| P2 | `lib/poller-server.js` | 100-140 | Confirms `grantReadData(pollerFn)` (§0 #5) — read-only, do not change. |

No external doc research needed: GSI-on-PAY_PER_REQUEST, sparse-index,
GSI-eventual-consistency, and "no ConsistentRead on GSI" are all
captured in §0/§7 from the AWS-documented semantics; the codex pass
may add citations but the behaviour is pinned by tests, not prose.

---

## 9. Locked decisions

1. **`indexName: 'status-index'`.** Fixed, stable name — referenced by
   string in `enumerator.py`, the moto fixture, and Gate 4. A
   CFN-auto-named index would be unreferenceable from the Lambda.
2. **`ProjectionType: ALL`.** The enumerator returns the whole row and
   the poller consumes many fields downstream (§0 #1). ALL avoids a
   second base-table fetch per row, which would defeat the cost win.
   Storage cost of a duplicated projection at personal scale (dozens
   of rows) is trivial.
3. **No sort key on the GSI.** The poll wants every active row in any
   order. A sort key adds nothing and constrains nothing here.
4. **No IAM change.** `grantReadData` (poller-server.js:130) already
   emits `dynamodb:Query` + `<tableArn>/index/*`. Adding a bespoke
   grant would *narrow nothing* and risk drift. Gate 5 locks the
   invariant instead of changing code.
5. **No code guard for the sparse-index invariant.** Every writer
   already sets `status` (schema-guaranteed). A runtime "row missing
   status" guard in the enumerator would be untestable against real
   data and is exactly the defensive cruft the review gate rejects.
   The invariant is documented (ADR + JSDoc), not coded.
6. **ADR Status `Accepted`** on authoring of `0007-watches-status-gsi.md`
   and the `README.md:14` row flipped `Planned`→`Accepted`. (ADR
   status lines are an allowed durable reference per global CLAUDE.md.)

## 10. NOT building (explicit)

- **A sharded / composite GSI partition key.** §0 #8 — the true
  at-scale design. Out of scope; this ADR is explicitly personal-scale
  and documents the ceiling honestly. Re-opening it is a future ADR.
- **Backfill / migration tooling.** The table is `RemovalPolicy.DESTROY`
  personal/dev data; a GSI on an existing table backfills automatically
  on `cdk deploy`. No data migration step.
- **Changing what the poller does with rows.** Pure read-path swap.
  `snapshot`/`gates`/`decision`/`writer` are untouched.
- **Removing the `Scan` capability elsewhere.** Only the enumerator
  used Scan on Watches; nothing else changes.
- **Strongly-consistent reads.** Impossible on a GSI (§0 #6); the
  one-tick latency for a brand-new watch is accepted, not engineered
  around.

---

## 11. Test matrix

| ID | Test | Asserts | File |
|----|------|---------|------|
| P-A | `test_returns_only_active_watches` (existing, must still pass) | active returned, paused/archived hidden — now via GSI Query | `test_enumerator.py` |
| P-B | `test_empty_table_returns_no_rows` (existing) | empty GSI partition → no rows, no raise | `test_enumerator.py` |
| P-C | `test_returns_active_rows_across_users` (existing) | GSI Query spans userIds (no longer userId-partitioned) | `test_enumerator.py` |
| P-D | `test_paginates_when_query_returns_last_evaluated_key` (RENAMED from `…scan…`) | 400 padded rows force ≥2 GSI-query pages; iterator follows `LastEvaluatedKey` | `test_enumerator.py` |
| P-E | `test_missing_table_env_var_raises_clear_error` (existing) | unchanged guard still raises `EnvironmentError` | `test_enumerator.py` |
| P-F | **NEW** `test_query_not_scan_and_full_row_projected` | monkeypatch/spy: `_watches_table.query` is called with `IndexName="status-index"` & `Key("status").eq("active")`; `_watches_table.scan` is **never** called; a returned row still has `preferences` & `dateWindow` (Projection=ALL, §0 #1) | `test_enumerator.py` |
| J-A | **NEW** GSI shape | `AWS::DynamoDB::Table` WatchesTable has `GlobalSecondaryIndexes` = exactly one `status-index`, KeySchema `status`/HASH, `Projection.ProjectionType==ALL`; `AttributeDefinitions` includes `status`/`S` | `test/data-stores.test.js` (new) |
| J-B | **NEW** poller grant covers the index (§0 #5) | full-stack synth: the poller function's DDB IAM policy `Resource` list contains an `index/*`-suffixed ARN (GetAtt/Join shape) | `test/data-stores.test.js` |
| J-C | **NEW** exactly one GSI / no regression | WatchesTable has exactly one GSI; FareHistory unchanged (no GSI) | `test/data-stores.test.js` |

Edge cases covered: empty partition, multi-user span, ≥2-page
pagination, missing-env guard, projection completeness, query-not-scan,
IAM index coverage, single-GSI (no accidental second index).

---

## 12. Validation gates

| Gate | Command (from the right cwd) | Expect |
|------|------------------------------|--------|
| 1 construct loads | `node -e "require('./lib/data-stores.js')"` | no throw |
| 2 enumerator imports | `cd lambdas/poller && ../../.venv-tests/Scripts/python.exe -c "import enumerator"` (with `WATCHES_TABLE_NAME` set) | no throw |
| 3 full poller suite | `cd lambdas/poller && ../../.venv-tests/Scripts/python.exe -m pytest tests/ -q` | all green incl. P-A…P-F; all four conftest fixtures still work |
| 4 GSI synth shape | `npx jest test/data-stores.test.js` | J-A/J-C green: one `status-index`, `Projection.ProjectionType==ALL`, `status` in AttributeDefinitions |
| 5 poller-grant index ARN | (part of `npx jest test/data-stores.test.js`) | J-B green: poller DDB policy Resource includes `index/*` |
| 6 full JS suite | `npx jest test/` | no regression (currently 128 across 6 suites + the new suite) |
| 7 evals/poller regression | `cd lambdas/poller && ../../.venv-tests/Scripts/python.exe -m pytest tests/ -q` then notifier suite | poller + notifier unchanged green |
| 8 cleanliness | `rg -n 'slice[ -_]?\d\|\bT[1-9]\b\|\bTask [1-9]\b\|Checkpoint [A-Z]\b\|phase \d\|\b(basically\|simply\|obviously\|essentially\|clearly\|merely\|kind of)\b' lib/data-stores.js lambdas/poller/enumerator.py lambdas/poller/tests/conftest.py lambdas/poller/tests/test_enumerator.py docs/adr/0007-watches-status-gsi.md test/data-stores.test.js` | zero matches (ADR `**Status:**` line / "ADR 0007" are allowed and not matched by this pattern) |
| 9 doc-flip accuracy | `git diff -- lib/data-stores.js lambdas/poller/enumerator.py` shows the JSDoc/docstrings now describe the **built** GSI (no "planned"/"if this ever grows"/"requires a Scan"); `docs/adr/README.md:14` row reads `Accepted` | manual + grep: `rg -n 'planned\|if this ever grows\|requires a Scan' lib/data-stores.js lambdas/poller/enumerator.py` → zero |

CDK synth in jest needs `'aws:cdk:bundling-stacks': []` context (mirror
`test/observability-dashboard.test.js` / Group C of `budget-alarm.test.js`).

---

## 13. Constraints inherited

- Global CLAUDE.md durable-artifact rule: zero `slice`/`T#`/`Task N`/
  `Checkpoint A-Z`/`phase N` labels and zero filler in every new/edited
  file, comment, docstring, ADR, and commit message. ADR numbers + ADR
  `**Status:**` lines + threat-model anchors are allowed.
- Test runner: `.venv-tests/Scripts/python.exe`; `cd lambdas/poller`
  before `pytest tests/` (memory `project_cdk_test_invocation_gotchas`).
  Jest from repo root.
- Numeric DDB fields use `Decimal(str(value))` — not relevant here (no
  writes), noted so the implementer does not "helpfully" touch
  `make_watch`.
- **Multi-reviewer gate** at the end: code-reviewer five-axis →
  security-auditor → test-engineer → code-reviewer comments.
  Sequential (memory `feedback_subagents_sequential`), different models
  per round (memory `feedback_multi_model_workflow`), fixes inline per
  reviewer pinned by tests, one commit per round.

## 14. Step-by-step

### Task 1: rewrite `lambdas/poller/enumerator.py` (read path)
- **ACTION**: (a) Swap `from boto3.dynamodb.conditions import Attr` →
  `from boto3.dynamodb.conditions import Key` (no other `Attr` use
  remains — verify with grep). (b) In `iter_active_watches`, change
  `kwargs` to `{"IndexName": "status-index", "KeyConditionExpression":
  Key("status").eq("active")}` and `resp = _watches_table.scan(**kwargs)`
  → `_watches_table.query(**kwargs)`. Pagination loop and the
  `_watches_table is None` → `EnvironmentError` guard are **unchanged**.
  (c) No `ConsistentRead` (§0 #6).
- **MIRROR**: the existing loop structure lines 44-51 — same shape,
  `query` instead of `scan`.
- **VALIDATE**: Gate 2.

### Task 2: update `lambdas/poller/tests/conftest.py` `_create_tables`
- **ACTION**: In the `WATCHES_TABLE` `create_table`: add
  `{"AttributeName": "status", "AttributeType": "S"}` to
  `AttributeDefinitions` and the `GlobalSecondaryIndexes` block from §7.
  Do **not** touch the `FARE_HISTORY_TABLE` create or any fixture body —
  the change must be transparent to `app_module`/`history_window_module`/
  `writer_module` (§0 #2).
- **VALIDATE**: Gate 3 (the proof it stayed backward-compatible: all
  four fixtures' tests pass).

### Task 3: extend + correct `lambdas/poller/tests/test_enumerator.py`
- **ACTION**: (a) RENAME `test_paginates_when_scan_returns_last_evaluated_key`
  → `test_paginates_when_query_returns_last_evaluated_key`; update its
  docstring/comments that say "Scan" → "Query" (and the module docstring
  + conftest `enumerator_module` docstring lines that say "Scan"). (b)
  Add **P-F** `test_query_not_scan_and_full_row_projected`: spy on the
  bound table (e.g. wrap/monkeypatch `enumerator._watches_table.query`
  and `.scan`), assert `query` called with `IndexName="status-index"`
  and a `Key("status").eq("active")` condition, assert `scan` never
  called, and assert a yielded row still contains `preferences` and
  `dateWindow` (Projection=ALL). Existing P-A…P-E must pass unchanged.
- **GOTCHA**: the spy must wrap the *same* table object the generator
  uses (`enumerator._watches_table`), set up inside the `mock_aws`
  context the `enumerator_module` fixture provides.
- **VALIDATE**: Gate 3.

### Task 4: add the GSI in `lib/data-stores.js` + flip the JSDoc
- **ACTION**: (a) After the `WatchesTable` `new ddb.Table(...)` block,
  add the `addGlobalSecondaryIndex` call from §7. (b) Rewrite JSDoc
  lines 22-27: drop "If this ever grows, add a GSI…/that requires a
  Scan"; state the built design — "Polling all active watches across
  users is a `Query` on the `status-index` GSI (ADR 0007), not a Scan.
  **Invariant: every Watches row MUST carry a `status` attribute or it
  is invisible to the poller** (sparse GSI)." Keep the existing
  comment voice. (c) Also flip the `enumerator.py` module docstring +
  inner-method docstring (lines ~4-13, ~32-37) from Scan/"planned" to
  the built Query-on-GSI description (this pairs with Task 1; do it
  here so the doc-flip is one reviewable unit).
- **VALIDATE**: Gates 1, 4, 9.

### Task 5: author `docs/adr/0007-watches-status-gsi.md`
- **ACTION**: Mirror `0005`/`0006` exactly: `# ADR 0007 — Watches table
  status GSI for the poller`, `**Date:** 2026-05-16`, `**Status:**
  Accepted`, `## Context` (the O(N)-Scan cost problem; quote the
  original `data-stores.js` Scan rationale it supersedes), `## Decision`
  (the GSI shape from §7; ProjectionType=ALL rationale; no IAM change
  because `grantReadData` covers `index/*`; **honest §0 #8 ceiling** —
  status-only PK is low-cardinality, less-wrong-than-Scan, personal
  scale, sharded PK is the future ADR), `## Consequences` (**Good:**
  O(A) not O(N), full row in one read, no IAM/infra churn; **Cost:**
  GSI eventual consistency → new watch may wait one tick, sparse-index
  invariant is now load-bearing, single hot GSI partition is the
  at-scale ceiling; **Not chosen — and why:** sharded/composite GSI PK,
  base-table redesign, keeping Scan). `## Closes` trailer like 0006.
  Then flip `docs/adr/README.md:14` row Status `Planned` → `Accepted`.
- **GOTCHA**: ADR prose is a durable artefact — no `slice`/`T#`/filler;
  "ADR 0007" and the `**Status:**` line are the *allowed* refs.
- **VALIDATE**: Gates 8, 9.

### Task 6: create `test/data-stores.test.js`
- **ACTION**: New jest suite mirroring `test/observability-dashboard.test.js`
  (`App` with `'aws:cdk:bundling-stacks': []`, `Template.fromStack`,
  `Match`). J-A: instantiate `DataStoresConstruct` in a bare `Stack`;
  `hasResourceProperties('AWS::DynamoDB::Table', …)` asserting the
  WatchesTable's `GlobalSecondaryIndexes` (one `status-index`, KeySchema
  `status`/HASH, `Projection.ProjectionType==='ALL'`) and `status`/`S`
  in `AttributeDefinitions`. J-C: exactly one GSI on WatchesTable,
  FareHistory has none. J-B: synth the full `StrandsAgentOnLambdaStack`
  (Docker-skip context, like `budget-alarm.test.js` Group C) and assert
  the poller function's `AWS::IAM::Policy` has a DDB statement whose
  `Resource` array contains an entry whose last `Fn::Join` segment ends
  `/index/*` (CDK `grantReadData` shape) — locking §0 #5.
- **GOTCHA**: cross-instance `Match` after `jest.resetModules()` —
  re-require `aws-cdk-lib`/`assertions` inside the full-stack test
  (the exact hazard `budget-alarm.test.js` Group C documents). Pin
  numbers as numbers if any appear (none here, but keep `Match` strict).
- **VALIDATE**: Gates 4, 5, 6.

## 15. Risks and mitigations

| Risk | L | I | Mitigation |
|------|---|---|------------|
| Implementer uses `KEYS_ONLY`/`INCLUDE` projection → poller silently truncates rows in prod | LOW | HIGH | §0 #1 + §7 pin ALL; Gate 4 asserts `ProjectionType==='ALL'`; P-F asserts a real row keeps `preferences`/`dateWindow` |
| conftest GSI not added → every poller-fixture test that queries fails, OR added wrong → only `test_enumerator` checked | MED | MED | §0 #2; Gate 3 runs the **full** poller suite (all four fixtures), not just the enumerator file |
| Stale "Scan" wording survives in docstrings/test names → durable-artefact rot | MED | LOW | §0 #7 + Task 3/4 explicitly rename + flip; Gate 9 greps for residual "Scan"/"planned"/"if this ever grows" |
| Someone adds `ConsistentRead=True` "for safety" → boto3 `ValidationException` at runtime | LOW | MED | §0 #6 + §7 explicit "no ConsistentRead"; P-F exercises the real moto query path which would surface it |
| Future hand-scoped poller grant breaks GSI Query (AccessDenied, runtime-only) | LOW | MED | Gate 5 / J-B locks the synthesised policy `Resource` to include `index/*` so the regression fails at synth |
| `addGlobalSecondaryIndex` with a capacity block on a PAY_PER_REQUEST table → synth error | LOW | LOW | §7/§9 #2 explicitly: no `readCapacity`/`writeCapacity`; Gate 1 catches a synth error immediately |

## What "done" looks like

- `lib/data-stores.js` WatchesTable has the `status-index` GSI
  (Projection ALL); JSDoc describes the built design + sparse-index
  invariant; no "planned"/"Scan" forward-ref.
- `enumerator.py` Queries the GSI (`Key`, `IndexName`), pagination +
  env guard unchanged; docstrings flipped.
- `conftest.py` `_create_tables` adds the GSI transparently; all four
  fixtures' suites green.
- `test_enumerator.py`: P-A…P-E pass, P-D renamed, P-F added.
- `test/data-stores.test.js`: J-A/J-B/J-C green.
- `docs/adr/0007-watches-status-gsi.md` authored (Accepted),
  `README.md:14` flipped to Accepted.
- All 9 gates green; full JS suite + full poller/notifier suites no
  regression; cleanliness + doc-flip greps zero.
- No `slice/T#/Task-N/Checkpoint/phase/filler` in any new or edited
  file or in the commits.

## Confidence

**8.5/10** for one-pass implementation. The surface is small and every
line has an exact in-repo precedent (the existing enumerator loop, the
0005/0006 ADR format, the observability/budget-alarm jest patterns, the
conftest fixture). The two real sharp edges — ProjectionType=ALL and
the shared-conftest GSI — are each pinned by a dedicated gate and test.
Point-and-a-half off because moto's GSI-Query pagination fidelity under
PAY_PER_REQUEST is the one behaviour not previously exercised in this
repo; Gate 3's renamed 400-row pagination test (P-D) is the backstop
that proves it before the reviewer gate.
