# Implementation Report — Watches status GSI (ADR 0007)

**Plan:** `tasks/watches-status-gsi.prp.md`
**Completed:** 2026-05-16
**Iterations:** 1 (Ralph, max 10)
**Impl commit:** `a81497f`

## Summary

The poller enumerated active watches with a table `Scan` +
`FilterExpression` on `status` — O(total rows) per tick, billing
paused/archived rows every cycle. Added a `status`-keyed GSI
(`status-index`, Projection ALL, inherits PAY_PER_REQUEST) to
`WatchesTable` and rewrote `enumerator.py` to
`Query(status=="active")` on it — O(active) per tick, full row in one
read. Pagination loop and the missing-env guard are unchanged; no IAM
change (CDK `grantReadData` already covers `index/*`). Closes ADR 0007
(index row flipped `Planned`→`Accepted`).

## Tasks completed (6)

1. `lambdas/poller/enumerator.py` — `Attr`→`Key`, `scan`→`query(IndexName="status-index")`; module + method docstrings flipped to describe the built GSI; loop + `EnvironmentError` guard byte-unchanged; no `ConsistentRead` (invalid on a GSI).
2. `lambdas/poller/tests/conftest.py` — `_create_tables` adds `status` AttributeDefinition + the `status-index` GSI to the moto WatchesTable. Backward-compatible: FareHistory and all four fixtures untouched (full poller suite still 208).
3. `lambdas/poller/tests/test_enumerator.py` — renamed `…scan…`→`…query…`, flipped Scan prose, added **P-F** (asserts `query` with `IndexName`/`Key("status").eq("active")`, `scan` never called, full row keeps `preferences`/`dateWindow`) and **P-G** (deterministic stubbed-`query` pagination — proves the `LastEvaluatedKey` loop independent of moto GSI fidelity, §0 #10).
4. `lib/data-stores.js` — `addGlobalSecondaryIndex` (Projection ALL, no capacity block); JSDoc flipped + records the sparse-index invariant. `lambdas/travel-agent/watches.py` — corrected the stale "switch to a status GSI" comment (this index is poll-only, cross-user; the per-user path needs a separate composite — §0 #15).
5. `docs/adr/0007-watches-status-gsi.md` — authored mirroring 0005/0006 (Context / Decision / Consequences Good·Costs·Not-chosen / Closes; honest low-cardinality + backfill-window ceilings). `docs/adr/README.md:14` flipped `Planned`→`Accepted` with link.
6. `test/data-stores.test.js` — J-A GSI shape, J-C one-GSI/FareHistory-none, J-B full-stack synth proving the **poller role** has a `dynamodb:Query` statement whose `Resource` covers both the table ARN and `/index/*` (locks §0 #5/#12).

## Validation results (all 9 gates)

| Gate | Result |
|------|--------|
| 1 data-stores module loads | PASS |
| 2 enumerator imports (configured env) | PASS |
| 3 full poller suite | PASS — 208 passed (all 4 conftest fixtures) |
| 4 GSI synth shape | PASS — J-A/J-C |
| 5 poller-grant index ARN | PASS — J-B (Query + table ARN + /index/* co-occur on poller role) |
| 6 full JS suite | PASS — 7 suites / 132 (128 prior + 4 new) |
| 7 poller + notifier regression | PASS — poller 208, notifier 126 |
| 8 cleanliness | PASS — zero |
| 9 doc-flip accuracy | PASS — zero stale `planned`/`Scan`/`switch to a status GSI` |

## Codebase patterns discovered

- `aws-cdk-lib/assertions` `findResources()` returns `{logicalId:{Type,Properties}}` — assertions must read `.Properties.X`. A helper that returned the raw resource cost the only red on first jest run; fixed same iteration.
- CDK `Table.grantReadData(fn)` auto-emits `dynamodb:Query` + `<tableArn>/index/*` for the grantee role — verified by full-stack synth (J-B). §0 #5 held; no IAM code change.
- Gate 1 (`node -e require`) only loads the module; the real GSI-config synth proof is Gate 4 (`Template.fromStack`), exactly as §0 #13 predicted.

## Deviations from plan

- One test-helper bug (`.Properties` indirection) surfaced and fixed within iteration 1; no production-code or plan deviation. All §0 pre-baked + codex-folded findings held as written.

## Outstanding

PRP §13 sequential 4-reviewer gate (code five-axis → security →
test-engineer → comments) — separate phase, not a Ralph iteration.
