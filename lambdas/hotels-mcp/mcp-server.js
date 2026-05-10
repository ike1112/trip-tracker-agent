import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import packageInfo from './package.json' with { type: 'json' };
import searchHotelOffers from './tool-search-hotel-offers.js';
import getHotelDetails from './tool-get-hotel-details.js';

/**
 * Builds a fresh MCP server per request — stateless.
 *
 * Tool names are globally unique across all MCP servers (search_hotel_offers,
 * get_hotel_details). The travel-agent merges every server's tool list into
 * one toolbox; same-named tools would be ambiguous.
 */
export function createMcpServer() {
    const server = new McpServer(
        { name: 'hotels-mcp', version: packageInfo.version },
        {
            capabilities: { tools: {} },
            instructions:
                'Search and detail hotel offers for trip-tracker watches. ' +
                'Wraps the LiteAPI hotel-rates API; supports fixture replay ' +
                'mode for offline review.',
        }
    );

    server.tool(...searchHotelOffers);
    server.tool(...getHotelDetails);

    return server;
}
