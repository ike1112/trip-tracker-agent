# Implementation Report — cleanup-and-dashboard

**Plan**: `tasks/cleanup-and-dashboard.prp.md` (committed `c24e2e3`, revised `<this commit>`)
**Implementation commit**: `8e613f8`
**Completed**: 2026-05-13
**Iterations**: 1

## Summary

Executed `tasks/cleanup-and-dashboard.prp.md` end-to-end in a single Ralph iteration. Two bundled work items: hard-delete the legacy bookings-mcp scaffold (13 files), and add a `cloudwatch.Dashboard`-emitting CDK construct (`lib/observability-dashboard.js`) that surfaces the four poller EMF counters, eight Lambda metric sources (five primary + three JWT authorizers), and three API Gateways in a single named CloudWatch dashboard.

Construct-property exposure normalised across all five primary constructs (`this.function` uniformly), three authorizer-bearing constructs (`this.authorizerFunction`), and three API-bearing constructs (`this.api`). Two `return { ...Endpoint };` anti-patterns in flights-mcp + hotels-mcp removed — under ES spec semantics those replaced the construct instance and silently made any `this.foo = ...` assignment unreachable to the stack code that captured `new FooConstruct(...)`.

## Tasks Completed

All 11 atomic tasks from PRP §14 executed in dependency order:

1. Verified bookings-ref location in ADR + design spec.
2. Deleted `lambdas/bookings-mcp/` (12 files) + `lib/mcp-server.js`.
3. Dropped `McpServerConstruct` wiring from the stack.
4. Removed `MCP_ENDPOINT` env var injection from `lib/agent.js`.
5. Updated `lambdas/travel-agent/mcp_client_manager.py` (docstring + endpoint tuple).
6a. Reworked `lib/flights-mcp-server.js` + `lib/hotels-mcp-server.js` to drop the return-object anti-pattern and expose four properties on `this`.
6b. Renamed `this.pollerFn` → `this.function` in `lib/poller-server.js`.
6c. Exposed `this.function` / `this.api` / `this.authorizerFunction` in `lib/agent.js`.
7. Tightened the one remaining bookings reference in the design spec (ADR 0002 was already clean).
8. Created `lib/observability-dashboard.js`.
9. Wired the dashboard into the stack; created `test/observability-dashboard.test.js` (51 tests) and `lambdas/poller/tests/test_metrics_constants_sync.py` (3 tests).

## Validation Results

| Gate | Result | Detail |
|---|---|---|
| 1 — notifier suite | PASS | 126 passing (`cd lambdas/notifier && pytest tests/ -q`) |
| 2 — poller + evals | PASS | 310 passing (307 prior + 3 new constants-sync) |
| 3 — jest | PASS | 76 passing (25 notifier + 51 new dashboard) |
| 4 — cleanliness ripgrep | PASS | zero matches in three new files |
| 5 — full-stack synth + metric-shape | PASS | 8 Lambdas + 3 APIs with correct metric dimensions, no unresolved refs, no literal `undefined` |
| 6 — bookings audit | PASS | zero hits in `lib/` / `lambdas/` / `test/` / production-readiness companion spec |
| 7 — cross-language sync | PASS | covered by Gate 2 |

## Codebase Patterns Discovered

- **CDK construct anti-pattern (load-bearing for ES-spec semantics):** A class constructor that does `return { foo };` silently replaces the constructed instance per the ECMAScript spec. Any `this.foo = ...` assignment becomes unreachable to callers that capture `new FooConstruct(...)`. Both flights and hotels MCP constructs hit this; the fix is to drop the explicit return and put everything on `this`. Worth grepping for `return\s*\{` inside CDK construct files in future audits.
- **`aws:cdk:bundling-stacks: []` context** skips asset bundling for every stack in the App. Makes `app.synth()` runnable on Docker-less machines (e.g. the Windows dev box used here). Necessary for any test or node-eval that synthesises a stack containing a Lambda layer with a `bundling.image` block.
- **CDK `DashboardBody` is an `Fn::Join` of fragments**, not a literal JSON string. To assert metric-dimension shape in a test, concatenate the fragments while substituting Lambda `FunctionName` / API `Name` from the template's own Resources map (plus CFN pseudo-parameters like `AWS::Region`), then `JSON.parse` the result. Substring `body.includes(name)` is insufficient because a widget label can carry the name string while the metric dimension array is wrong.
- **`from tests.conftest import` in lambda packages** requires pytest to be invoked from the package root (`cd lambdas/foo && pytest tests/`), not from the repo root. The repo-root invocation fails at collection with `ModuleNotFoundError: No module named 'tests'`. Worth standardising the test-runner invocation across the repo, or adding a `conftest.py` at the repo root that imports from each package.

## Learnings

- **Two-pass adversarial review caught what a single pass missed.** First Codex pass surfaced four construct-property exposure defects (the `return { ... }` anti-pattern most importantly). Second pass surfaced two more (substring-vs-metric-shape gate weakness; authorizer Lambdas excluded from dashboard scope). A third pass would likely find the implementation-pass defects logged below as preventable in the PRP itself.
- **Author the PRP's validation gates against the actual runtime they will execute in.** Gate 5's fallback was internally inconsistent because it assumed the node-eval would dodge Docker — both `cdk synth` and `app.synth()` go through the same bundler. The PRP author had clearly not run the fallback on a Docker-less machine; the only way to catch this is to run the gate during PRP authoring.
- **`grep -nc` returning a single-line tuple count is a lossy invariant.** Task 1's check was `grep -nc 'bookings' file1 file2` returning `1+1=2`, but the file that was supposed to have 1 hit actually had 0. The grep output was a single number with no per-file breakdown until I split the command. Future Task 1-style validation should grep with per-file lines, not bare counts.

## Deviations from Plan

- ADR 0002 was already bookings-free at execution time. Task 1 + Task 7 narrowed scope to the design spec only (one-line edit).
- Touched two paths NOT in the PRP §8 file-table: `lib/hotels-mcp-server.js` (comment referenced "bookings + flights authorizers") and `docs/superpowers/specs/2026-05-10-trip-tracker-production-readiness.md` (row 8 + slice-8 checklist item both named `bookings-mcp`). Both edits were required by Gate 6's explicit contract.
- Gate 5 invocation deviates from the PRP's documented snippet (added `'aws:cdk:bundling-stacks': []` context + pseudo-parameter substitution). The deviation is captured in `test/observability-dashboard.test.js` Group F's `beforeAll` block; the PRP's §0 third-pass table documents the gap.
- Gate 1 invocation deviates from the PRP's documented command (added `cd lambdas/notifier`). Same documentation gap, same third-pass table entry.
