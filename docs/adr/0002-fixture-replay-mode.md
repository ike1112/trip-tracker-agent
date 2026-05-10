# ADR 0002 — Fixture replay mode for external-API MCP servers

**Date:** 2026-05-10
**Status:** Accepted
**Slice:** 3

## Context

The `flights-mcp` Lambda wraps the Duffel API. The `hotels-mcp` Lambda
(slice 4) will wrap LiteAPI. Both are paid third-party APIs.

A forking reviewer reading this repo cold needs to be able to:
1. Read the code and understand how it fits together.
2. Run the unit tests and have them pass.
3. (Stretch goal) Deploy the stack and exercise it end-to-end.

If every flight search hits Duffel for real, none of those three are
possible without credentials the reviewer doesn't have — credentials that
also require account approval and per-request charges. The result is
"looks impressive on GitHub, but I can't actually run it." That's a weak
portfolio signal.

We also need the same Lambda binary to work three ways: with real Duffel
calls in production, against recorded responses in CI, and against
recorded responses for a reviewer running the stack locally. Forking the
code path doesn't help — you'd be debugging "is it the real code or the
test code?" forever.

## Decision

Both MCP-server Lambdas pick a client implementation **at cold start**
based on an `MCP_MODE` environment variable:

- `MCP_MODE=live` — real Duffel / LiteAPI calls (`client-live.js`).
- `MCP_MODE=fixture` — read pre-recorded JSON from `fixtures/` (`client-fixture.js`).

Both clients implement the same module interface
(`searchOffers`, `getOfferDetails`). A thin `client.js` selector picks one
at module load:

```js
// client.js
export const client = MCP_MODE === 'fixture' ? fixture : live;
```

Everything downstream — the tool handlers, the MCP server, the Lambda
handler — sees one client interface and has no idea which mode it's in.
That's the test for whether the seam is in the right place.

Layout, per MCP server:

```
lambdas/flights-mcp/
  index.js               # Lambda handler (mode-agnostic)
  mcp-server.js          # tool registration (mode-agnostic)
  client.js              # mode selector
  client-live.js         # real HTTP client
  client-fixture.js      # same interface, reads fixtures/
  fixtures/
    SFO-NRT-2026-10-15.json
    LHR-CDG-2026-12-20.json
```

CDK default is `MCP_MODE=fixture` so a `cdk deploy` with no parameters
produces a stack that runs without external API keys. Switching to live
is a deploy-time context flag:

```
cdk deploy -c mcpMode=live -c duffelApiKey=sk_live_...
```

Fixtures are recorded once via a one-shot script (`tools/record-fixtures.py`,
not in this slice) with real keys, then committed. They're treated as
test data: small, hand-curated, illustrative of the cases the agent
actually exercises.

## Consequences

**Good:**
- Repo is forkable and end-to-end runnable for a reviewer with no Duffel
  or LiteAPI accounts. That's the single strongest production-readiness
  signal in the whole project.
- Unit tests run without network or external dependencies. CI is cheap
  and deterministic.
- Loom recording / live demo is reliable — no API outages mid-take.
- Forces a clean seam between "wraps an external API" and "delivers MCP
  tool semantics." That same seam is what makes the system mockable at all.
- The live client is honest about its absence — `DUFFEL_API_KEY` missing
  in live mode throws loudly, not silently. No mystery failures.

**Cost:**
- Two client implementations to keep aligned. Mitigated by a clear
  interface contract (the two exported function names + their result
  shape) and by the tool handlers being mode-agnostic — drift shows up
  immediately in the fixture tests.
- Fixtures rot. The recorded JSON can drift from Duffel's current schema.
  Acceptable for v1; long-term, the fixture file's `recordedAt` field
  makes it auditable, and the recording script can be re-run.
- Fixture lookup is by deterministic filename
  (`{origin}-{destination}-{departDate}.json`). Reviewers can read the
  fixture dir and predict what the tool will return. Trades flexibility
  for legibility.

**What we're explicitly not doing:**
- No "mock server" running alongside the real one. The fixture client is
  the in-process implementation — adding a network hop just to talk to a
  mock would defeat the cold-start and deploy-package wins.
- No randomisation in fixtures. A reviewer reading SFO-NRT-2026-10-15.json
  should see exactly what the agent will get back. Predictability beats
  realism for a portfolio piece.

## References
- Production-readiness companion spec §3.1
- `lambdas/flights-mcp/client.js`
- `lambdas/flights-mcp/client-fixture.js`
- `lambdas/flights-mcp/client-live.js`
- `lambdas/flights-mcp/tests/client-fixture.test.js`
- `lambdas/flights-mcp/tests/handler.test.js`
