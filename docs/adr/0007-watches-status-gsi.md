# ADR 0007 — Watches table status GSI for the poller

**Date:** 2026-05-16
**Status:** Accepted

## Context

The poller enumerates every active watch on each scheduled tick. The
`Watches` table partitions by `userId` (right for per-user chat CRUD),
so "all active watches across all users" had no key path. The original
`data-stores.js` design accepted a table `Scan` with a
`FilterExpression` on `status == "active"`:

> Acceptable while watch counts are small (personal use, dozens of
> items). If this ever grows, add a GSI keyed on `status`.

A Scan reads — and on a provisioned-cost model, bills — every item in
the table on every tick. The `FilterExpression` is applied *after*
DynamoDB's 1MB page cap, so paused and archived rows still consume read
throughput before being discarded. Cost is O(total rows) per tick
regardless of how many watches are actually active. As the table
accumulates archived history, every poll gets linearly more expensive
for no added work. This ADR closes that forward-reference.

## Decision

Add one Global Secondary Index to the existing `WatchesTable` and
rewrite the poller's enumerator to Query it.

- **Index:** `status-index`, partition key `status` (STRING), **no sort
  key**. The poll wants every active row in any order; a sort key would
  constrain nothing useful here.
- **`ProjectionType: ALL`.** `iter_active_watches` returns the whole
  row and the poller consumes many fields downstream (`preferences`,
  `dateWindow`, `maxTotalPrice`, `alertStrategy`, `lastAlerted*`) to
  compose the MCP request and run the gates. Projecting ALL avoids a
  second base-table fetch per row, which would defeat the cost win. At
  personal scale the duplicated projection storage is trivial.
- **Read path:** `enumerator.py` Queries
  `IndexName="status-index", KeyConditionExpression=Key("status").eq("active")`,
  keeping the existing transparent `LastEvaluatedKey` pagination loop.
  No `ConsistentRead` — strongly-consistent reads are unsupported on a
  GSI.
- **No IAM change.** `lib/poller-server.js` grants the poller
  `grantReadData` on the Watches table. The grant call itself is
  unchanged; because the table now has a GSI, CDK's `grantReadData`
  automatically extends the synthesised `Resource` to include
  `<tableArn>/index/*` and adds `dynamodb:Query` to the action set —
  the GSI causes the widening; it was not pre-existing. A bespoke
  grant would narrow nothing and invite drift; a synth test locks the
  invariant instead.
- **Honest scope ceiling.** A `status`-only partition key is very low
  cardinality — every active row lands in one GSI partition. This is
  *less wrong* than Scan-all, not the production-correct design. At
  scale the correct answer is a sharded or composite (`userId`+`status`)
  partition key. This ADR is explicitly scoped to personal scale, the
  same framing the original Scan decision used; a sharded redesign is a
  future ADR, not this one.

## Consequences

**Good:**

- Poll read cost is O(active watches), not O(total rows). Paused and
  archived rows no longer cost anything every tick. A table that
  accumulates history stops inflating every poll.
- The full row arrives in one read (Projection ALL) — no second fetch,
  no partial-row class of bug.
- Zero infra/IAM churn: one `addGlobalSecondaryIndex` call; the existing
  `grantReadData` call is unchanged, and CDK automatically widens its
  synthesised policy to cover the index because the GSI is now present.

**Costs / limits:**

- **Sparse-index invariant is now load-bearing.** A GSI only projects
  items that carry its partition-key attribute. A Watches row written
  without a `status` attribute is invisible to the poller — and fails
  *silently* (never polled, no error). Every writer sets `status` today
  (chat CRUD + the test factory), so this holds, but it is now an
  invariant, not a convenience. Documented in `data-stores.js`.
- **GSI is eventually consistent.** A just-created "active" watch may
  miss the immediately following tick. The poller is scheduled and
  idempotent, so the next tick picks it up — a one-cycle latency for a
  brand-new watch, harmless and far cheaper than Scan-all.
- **One-time backfill window.** A single `cdk deploy` adds the index and
  flips the code to Query. While the GSI backfills, a Query against it
  raises `ValidationException` until the index is `ACTIVE`. A fresh
  `RemovalPolicy.DESTROY` personal table with dozens of rows backfills
  near-instantly; a scheduled tick that lands in that window fails that
  tick only and self-heals on the next. For a populated table the
  zero-miss path is a two-step deploy (add the GSI, wait for `ACTIVE`,
  then deploy the enumerator change). No in-enumerator try/except is
  added — a one-time deploy window does not justify untestable
  defensive code in the hot path.
- **Low-cardinality partition.** All active rows share one GSI
  partition (the scope ceiling above). Fine at personal scale; the
  documented boundary, not a defect.
- **Yield order is unspecified.** Scan never guaranteed order and a GSI
  Query yields differently. Safe because the poller processes each
  watch independently with no cross-watch ordering dependency.

**Not chosen — and why:**

- **Sharded / composite (`userId`+`status`) GSI partition key.** The
  true at-scale design. Out of scope; this decision is personal-scale
  and says so. A future ADR revisits it if scale demands.
- **Base-table redesign (status in the key).** Would break the
  per-user chat CRUD access pattern the table is correctly shaped for.
- **Keeping the Scan.** The cost problem this ADR exists to fix.
- **In-enumerator backfill retry/try-except.** Untestable cruft for a
  one-time deploy window; the scheduled-retry self-heal and the
  two-step-deploy option cover it without hot-path code.

## Closes

- `docs/adr/README.md` row 0007 (`Planned` → `Accepted`).
- The `lib/data-stores.js` forward-reference ("If this ever grows, add
  a GSI keyed on `status`").
- The `lambdas/poller/enumerator.py` "ADR 0007 (planned)"
  forward-reference and its Scan implementation.
