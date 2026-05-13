/**
 * Lambda entrypoint for the hotels MCP server. Same shape as
 * lambdas/flights-mcp/index.js (see ADR 0002 for the direct-handler
 * reasoning) with one observability addition: a `latency_ms` field on
 * every mcp_request log so a slow-LiteAPI incident is one CloudWatch
 * query away.
 *
 * Identical-but-duplicated to flights-mcp:
 *   - LambdaTransport adapter
 *   - JWT re-verify in-handler
 *   - 401 / 400 / 500 helpers
 *
 * See lambda-transport.js for why duplication is the right call until
 * a third MCP server appears.
 */
import jwt from 'jsonwebtoken';
import { Logger } from '@aws-lambda-powertools/logger';
import { createMcpServer } from './mcp-server.js';
import { LambdaTransport } from './lambda-transport.js';
import { mode as mcpMode } from './client.js';

const JWT_SIGNATURE_SECRET = process.env.JWT_SIGNATURE_SECRET;
const logger = new Logger({ serviceName: 'hotels-mcp' });

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

    // Defense in depth — API GW authorizer has already validated, but the
    // function does not blindly trust the upstream integration.
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
