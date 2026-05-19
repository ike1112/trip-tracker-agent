# hotels-mcp

Hotels MCP server for Trip Tracker.

This Lambda exposes hotel search tools through MCP JSON-RPC, with two execution modes:
- `fixture`: offline, deterministic responses from local JSON fixtures
- `live`: calls LiteAPI using `LITEAPI_API_KEY`

The server is intentionally stateless: one Lambda invocation handles one JSON-RPC request.

## What This Service Does

- Registers and serves two MCP tools:
  - `search_hotel_offers`
  - `get_hotel_details`
- Validates inbound internal JWTs in-handler (defense in depth)
- Dispatches JSON-RPC requests through a Lambda transport adapter
- Returns MCP-compatible JSON-RPC responses to API Gateway callers

## Files At A Glance

- `index.js`: Lambda entrypoint, auth verification, JSON-RPC dispatch
- `mcp-server.js`: MCP server construction + tool registration
- `lambda-transport.js`: MCP transport bridge for Lambda request/response lifecycle
- `tool-search-hotel-offers.js`: tool schema + handler for hotel search
- `tool-get-hotel-details.js`: tool schema + handler for hotel details
- `client.js`: mode selector (`MCP_MODE` => fixture/live)
- `client-fixture.js`: local canned responses for zero-cost testing
- `client-live.js`: LiteAPI integration
- `fixtures/`: fixture payloads used in `fixture` mode
- `tests/`: node test suite, including handler and tool tests

## Tool Surface (MCP)

### `search_hotel_offers`

Purpose: search top hotel offers for a city/date window.

Arguments:
- `city`: `string`, e.g. `"Tokyo"`
- `checkin`: `string` in `YYYY-MM-DD`
- `checkout`: `string` in `YYYY-MM-DD`
- `pax`: positive integer, default `1`
- `minStars` (optional): integer in `1..5`

Returns:
- MCP text payload with JSON body: `{ source, hotels }`

### `get_hotel_details`

Purpose: fetch full details for one hotel.

Arguments:
- `hotelId`: `string`

Returns:
- MCP text payload with normalized hotel details
- error payload (`isError: true`) when hotel is not found

## Authentication Model

This service uses ADR 0006 two-secret verification with strict `sub` coupling.

Accepted token combinations:
- `AGENT_JWT_SECRET_ARN` + `sub=travel-agent`
- `POLLER_JWT_SECRET_ARN` + `sub=trip-tracker-poller`

Rejected:
- cross-sub combinations
- foreign secrets
- missing `exp`
- non-HS256 signatures

Notes:
- API Gateway authorizer validates first
- this handler re-validates JWT in-handler for defense in depth

## Runtime Environment Variables

Required for all modes:
- `AGENT_JWT_SECRET_ARN`
- `POLLER_JWT_SECRET_ARN`

Mode selector:
- `MCP_MODE=fixture|live` (resolved at cold start)

Required only in `live` mode:
- `LITEAPI_API_KEY`

Behavior notes:
- `MCP_MODE` defaults to `live` in code
- tests force `MCP_MODE=fixture` in `tests/setup.js`

## Live Client Behavior (Important)

`client-live.js` enforces several guardrails:
- request timeout: 20 seconds per LiteAPI call
- currency strictness: only USD accepted; non-USD responses raise errors
- two-step retrieval:
  1. `/data/hotels` candidates
  2. `/hotels/rates` pricing
- output truncation to top 5 cheapest normalized offers

These choices protect latency budget and avoid silent currency conversion errors in downstream price history.

## Request/Response Behavior

Input expectation (API Gateway proxied Lambda):
- `headers.Authorization` or `headers.authorization`: `Bearer <jwt>`
- `body`: JSON-RPC object as JSON string or object

Special handling:
- JSON-RPC notification (`method` present, no `id`) => `202` with empty body

Common status codes:
- `200`: success with JSON-RPC response
- `202`: accepted notification, no response body
- `400`: invalid JSON / empty body
- `401`: auth failure
- `500`: MCP dispatch failure (`-32603` JSON-RPC error)

## Local Development

Install deps:

```bash
cd lambdas/hotels-mcp
npm ci
```

Run tests:

```bash
npm test
```

The tests:
- run in fixture mode
- seed in-memory secrets cache to avoid AWS calls
- verify transport behavior and auth hardening matrix (F1..F9)

## Live Mode Notes

To run live LiteAPI-backed behavior:

```bash
export MCP_MODE=live
export LITEAPI_API_KEY=<your-key>
```

Keep `AGENT_JWT_SECRET_ARN` and `POLLER_JWT_SECRET_ARN` set in runtime environments, since JWT verification still depends on them.

## Observability

Powertools logger service name:
- `hotels-mcp`

Notable logs:
- `cold_start` (includes `mcpMode`)
- `mcp_notification_ack`
- `mcp_request` (method/tool/userIdPrefix/latencyMs)
- `mcp_dispatch_error`
- `unauthorized`, `bad_request`

## Troubleshooting

### `401 Unauthorized`

Check:
- Bearer token exists
- token is signed by one of the two configured secrets
- `sub` matches the secret’s allowed principal
- token has `exp` and is not expired
- token algorithm is HS256

### `400 invalid_json` / `400 empty_body`

Check:
- API Gateway passed JSON string or object body
- JSON-RPC payload is valid JSON object

### `500 Internal Server Error`

Check:
- MCP method/tool name is valid
- tool arguments satisfy schema
- fixture files are present (fixture mode)
- LiteAPI key/network/quota/timeout (live mode)

## Design References

- ADR 0002: fixture replay mode
- ADR 0006: per-component JWT secrets with subject coupling
