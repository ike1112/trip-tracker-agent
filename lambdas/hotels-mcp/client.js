/**
 * Client mode selector. Resolved once at cold start based on MCP_MODE.
 *   MCP_MODE=fixture → reads pre-recorded JSON from ./fixtures/
 *   MCP_MODE=live    → calls the real LiteAPI (requires LITEAPI_API_KEY)
 *
 * Both clients expose the same shape: { searchHotels, getHotelDetails }.
 * See ADR 0002.
 */
import * as live from './client-live.js';
import * as fixture from './client-fixture.js';

const MCP_MODE = (process.env.MCP_MODE ?? 'live').toLowerCase();

export const client = MCP_MODE === 'fixture' ? fixture : live;
export const mode = MCP_MODE;
