/**
 * Client mode selector. Resolved once at cold start based on `MCP_MODE`:
 *
 *   MCP_MODE=fixture  → reads pre-recorded JSON from ./fixtures/
 *   MCP_MODE=live     → calls the real Duffel API (requires DUFFEL_API_KEY)
 *
 * Both clients expose the same shape: { searchOffers, getOfferDetails }.
 * The rest of the Lambda (tools, server) never knows which one it has.
 *
 * Pattern documented in ADR 0002 (fixture replay mode).
 */
import * as live from './client-live.js';
import * as fixture from './client-fixture.js';

const MCP_MODE = (process.env.MCP_MODE ?? 'live').toLowerCase();

export const client = MCP_MODE === 'fixture' ? fixture : live;
export const mode = MCP_MODE;
