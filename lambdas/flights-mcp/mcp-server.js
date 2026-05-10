import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import packageInfo from './package.json' with { type: 'json' };
import searchOffers from './tool-search-offers.js';
import getOfferDetails from './tool-get-offer-details.js';

/**
 * Builds a fresh MCP server per request — stateless. Each Lambda invocation
 * gets its own server bound to the LambdaTransport for that invocation.
 *
 * Tools are registered via the variadic mcpServer.tool() helper, which
 * derives the JSON-Schema for the tool surface from the zod schemas in each
 * tool file. That's the part the LLM sees on tools/list and validates
 * arguments against on tools/call.
 */
export function createMcpServer() {
    const server = new McpServer(
        { name: 'flights-mcp', version: packageInfo.version },
        {
            capabilities: { tools: {} },
            instructions:
                'Search and detail flight offers for trip-tracker watches. ' +
                'Wraps the Duffel API; supports fixture replay mode for offline review.',
        }
    );

    server.tool(...searchOffers);
    server.tool(...getOfferDetails);

    return server;
}
