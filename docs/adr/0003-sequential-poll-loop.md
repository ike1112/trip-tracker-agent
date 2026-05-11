# ADR 0003 — Sequential per-watch poll loop

**Date:** 2026-05-10
**Status:** Accepted
**Slice:** 5

## Context

The trip-tracker poller (slice 5) walks every active `Watches` row on an
EventBridge cron and, for each watch, calls `flights-mcp` then `hotels-mcp`,
writes a `FareHistory` snapshot, runs the threshold/anomaly/dedup gates,
and asks `decision.decide` whether to alert.

Two execution shapes were available:

1. **Sequential** — one watch, then the next, then the next. Per-watch wall
   time is `≈ 2 × MCP_TIMEOUT_SECONDS + DDB write + decision`. Total wall
   time is `N × per-watch`.
2. **Parallel** — `asyncio.gather` (or `concurrent.futures`) fans out one
   coroutine per watch. Total wall time approaches `per-watch` regardless
   of `N`, capped only by Lambda memory and downstream rate limits.

At personal scale (≤dozens of watches, 4-hour cadence) the sequential loop
costs at most ~60s per invocation for ~2 watches. The parallel loop would
finish faster but introduces complexity that the slice doesn't need.

## Decision

The poller uses a **plain sequential `for watch in iter_active_watches()`
loop**. One watch at a time. Per-watch failure (`McpCallError`,
`ValueError`, `KeyError`) is caught, logged as `watch_errored`, and the
loop continues to the next watch. The Lambda is configured with
`reservedConcurrentExecutions = 1` so an EventBridge tick cannot fan out
multiple concurrent invocations of the same poller.

```python
for watch in iter_active_watches():
    polled += 1
    metrics.increment(metrics.WATCHES_POLLED)
    try:
        _poll_one(watch)
    except (McpCallError, ValueError, KeyError) as e:
        errored += 1
        metrics.increment(metrics.WATCHES_ERRORED)
        logger.warning("watch_errored", extra={...})
        # continue
```

## Consequences

**Good:**

- **One bad watch never starves the others.** A misbehaving Duffel response
  for watch A — currency mismatch, malformed offer, transport timeout —
  raises a categorised exception, the per-watch try/except catches it, and
  the poller moves to watch B. The `alerts_sent` and
  `bedrock_decisions_made` metrics for the surviving watches still fire.
  In a parallel implementation this property requires explicit isolation
  (per-task try/except inside each future), which is easy to forget.

- **Per-watch latency is bounded and observable.** Each watch's structured
  log carries `watch_id` + `user_id_prefix` + the entry/exit timestamps;
  the gap between `watch_polled` and either `snapshot_written` or
  `watch_errored` is one log query away. With parallel execution the
  per-watch span is more interesting (X-Ray subsegments), but the basic
  observability story is harder.

- **Predictable downstream load.** Sequential calls naturally limit the
  rate at which the poller hits Duffel and LiteAPI: never more than one
  in-flight request per provider at a time. No accidental thundering herd
  if watches grow. `flights-mcp` and `hotels-mcp` API GW endpoints don't
  have to think about poller concurrency.

- **Reserved concurrency = 1 is free defence-in-depth.** Even if a future
  EventBridge change accidentally fires twice in quick succession, the
  Lambda runtime will queue the second invocation rather than spawning a
  parallel poller. Combined with the in-loop sequential guarantee, the
  worst case is one poll's worth of MCP / DDB load at any moment.

- **Cold-start cost is paid once.** The boto3 resource, the powertools
  logger, the JWT signer, and the module-level table refs are constructed
  once and re-used across watches in the same invocation. No per-task
  setup overhead.

**Cost:**

- **Linear wall-time growth.** At 10 watches × 2 MCPs × 15s worst-case
  timeout, an entire invocation can take 5 minutes if every upstream is
  slow. The Lambda timeout (`lambdaTimeoutSeconds` CDK context, default
  60s) must be raised in lockstep with watch count. The construct's
  comment names the formula; the `pollIntervalMinutes` cadence is much
  longer than even the worst case so we never fire a second poll while
  the first is still running.

- **No throughput headroom for v1.5+ work.** When the spec adds the
  flexible-window date sweep (§2.5 of the slice plan) or fans out across
  multiple destinations per watch, the sequential model will get noticeably
  slower. That's the threshold to revisit this decision.

**Not chosen — and why:**

- **`asyncio.gather` over watches.** Faster but adds complexity that doesn't
  pay off at personal scale: per-task error isolation, structured-log
  context propagation across coroutines, and cold-start cost of the
  asyncio event loop. Worth revisiting if `watches_polled` regularly
  approaches 30+ and the tail latency becomes a deploy-cadence concern.

- **Per-watch Lambda invocation (Step Functions or EventBridge fan-out).**
  Decouples per-watch failure even more cleanly, but multiplies the
  Lambda invocation count and the cold-start cost. Overkill for ≤50
  watches; revisit if the system ever takes on multi-tenant scale where
  per-user blast radius matters.

## References

- Production-readiness companion spec §3.4 names "sequential per-watch loop"
  as the explicit choice for slice 5.
- Slice 5 plan `tasks/slice-5-poller.plan.md` §6 risk #2 documents the
  Lambda-timeout-vs-watch-count formula this decision implies.
- `lambdas/poller/app.py` is the only call site.
- `lib/poller-server.js` reservedConcurrentExecutions = 1 — the matching
  defence at the Lambda runtime level.
