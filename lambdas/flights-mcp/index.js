/**
 * Lambda entrypoint for the flights MCP server.
 *
 * Shape: one Lambda invocation per JSON-RPC request from the agent.
 *
 *   1. API Gateway's Token Authorizer (the mcp-authorizer Lambda) has
 *      already validated the HS256 JWT before we get here.
 *   2. We re-verify the JWT in-handler as defense in depth — the function
 *      doesn't blindly trust an upstream integration and rejects requests
 *      if it's ever invoked through a different path or misconfigured GW.
 *   3. Parse the JSON-RPC body, dispatch through the in-memory
 *      LambdaTransport into the MCP server, return the response.
 *
 * No Express, no Lambda Web Adapter, no log4js. ~3 runtime deps. The cold
 * start cost is parsing a few JS modules; the deploy package is ~5MB.
 */
import jwt from 'jsonwebtoken';
import { Logger } from '@aws-lambda-powertools/logger';
import { createMcpServer } from './mcp-server.js';
import { LambdaTransport } from './lambda-transport.js';
import { mode as mcpMode } from './client.js';

const JWT_SIGNATURE_SECRET = process.env.JWT_SIGNATURE_SECRET;
const logger = new Logger({ serviceName: 'flights-mcp' });

logger.info('cold_start', { mcpMode });

function _unauthorized(reason) {
    logger.warn('unauthorized', { reason });
    return {
        statusCode: 401,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ error: 'Unauthorized' }),
    };
}

function _badRequest(reason) {
    logger.warn('bad_request', { reason });
    return {
        statusCode: 400,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ error: reason }),
    };
}

export const handler = async (event) => {
    const t0 = Date.now();

    // Defense in depth: re-verify the JWT here even though the API GW
    // authorizer already did. Same pattern as travel-agent/app.py.
    const authHeader = event.headers?.Authorization ?? event.headers?.authorization;
    if (!authHeader) return _unauthorized('missing_auth_header');
    let claims;
    try {
        const token = authHeader.split(' ')[1];
        claims = jwt.verify(token, JWT_SIGNATURE_SECRET);
    } catch (e) {
        return _unauthorized(`jwt_verify_failed:${e.message}`);
    }

    let rpc;
    try {
        rpc = typeof event.body === 'string' ? JSON.parse(event.body) : event.body;
    } catch (e) {
        return _badRequest(`invalid_json:${e.message}`);
    }
    if (!rpc || typeof rpc !== 'object') {
        return _badRequest('empty_body');
    }

    // Build a fresh server + transport pair per invocation. Stateless.
    const server = createMcpServer();
    const transport = new LambdaTransport();
    await server.connect(transport);

    try {
        const response = await transport.dispatch(rpc);
        logger.info('mcp_request', {
            method: rpc.method,
            tool: rpc.params?.name,
            userIdPrefix: claims.user_id ? String(claims.user_id).slice(0, 8) : null,
            latencyMs: Date.now() - t0,
        });
        return {
            statusCode: 200,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(response),
        };
    } catch (e) {
        logger.error('mcp_dispatch_error', {
            error: e.message,
            method: rpc.method,
            latencyMs: Date.now() - t0,
        });
        return {
            statusCode: 500,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                jsonrpc: '2.0',
                error: { code: -32603, message: 'Internal Server Error' },
                id: rpc.id ?? null,
            }),
        };
    } finally {
        await transport.close();
        await server.close();
    }
};
