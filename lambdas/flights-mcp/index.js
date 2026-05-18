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
import { SecretsManagerClient, GetSecretValueCommand } from '@aws-sdk/client-secrets-manager';
import { Logger } from '@aws-lambda-powertools/logger';
import { createMcpServer } from './mcp-server.js';
import { LambdaTransport } from './lambda-transport.js';
import { mode as mcpMode } from './client.js';

/*
 * ADR 0006 two-secret + sub-coupling JWT verifier.
 *
 * This block is COPIED VERBATIM into lambdas/mcp-authorizer/index.js,
 * lambdas/flights-mcp/index.js and lambdas/hotels-mcp/index.js. It is
 * intentionally triplicated, not a shared module: a cross-Lambda-package
 * shared verifier would need a Lambda layer or a monorepo symlink, both
 * larger than this hardening change and a new failure surface. Each
 * package's tests pin the identical cross-sub-forgery + foreign-secret
 * invariant. EDIT ALL THREE COPIES TOGETHER.
 */
let secretsClient = new SecretsManagerClient();
const _secretCache = {};

// Lazy: fetch on first verify, not at module load, so tests can seed
// _secretCache before any AWS SDK call fires (ADR 0006).
async function getSecret(envVar) {
    const arn = process.env[envVar];
    if (!arn) throw new Error(`${envVar} env var is required`);
    if (_secretCache[arn] == null) {
        const out = await secretsClient.send(new GetSecretValueCommand({ SecretId: arn }));
        _secretCache[arn] = out.SecretString;
    }
    return _secretCache[arn];
}

// Each signing secret may mint exactly one sub. A token must verify
// under a secret AND carry that secret's allowed sub — without the
// coupling a leaked agent token would also pass as a poller token.
const SECRET_SUBS = [
    ['AGENT_JWT_SECRET_ARN',  'travel-agent'],
    ['POLLER_JWT_SECRET_ARN', 'trip-tracker-poller'],
];

async function verifyTwoSecret(token) {
    for (const [envVar, allowedSub] of SECRET_SUBS) {
        // Fetch OUTSIDE the verify try: a Secrets Manager / KMS failure
        // is an infra error, not a bad signature. It still fails closed
        // (the handler's catch denies), but it surfaces with its own
        // error message instead of being laundered into the generic
        // "no candidate secret verified" deny — so the two alarm apart.
        const secret = await getSecret(envVar);
        let claims;
        try {
            // Pin HS256 explicitly — don't rely on the library default
            // to block alg=none / RS-HS confusion (defense in depth).
            claims = jwt.verify(token, secret, { algorithms: ['HS256'] });
        } catch {
            continue; // wrong secret / expired / malformed — try the next
        }
        // Enforce expiry at the boundary: a token minted without exp
        // must not be treated as eternal just because a minter slipped.
        if (claims.exp === undefined) continue;
        if (claims.sub === allowedSub) return claims;
        // Verified under this secret but wrong sub: cross-sub forgery.
        // Do not echo the attacker-influenced sub into the log line.
        throw new Error('sub not allowed for this secret');
    }
    throw new Error('no candidate secret verified the token');
}

// Test seam: seed the cache so getSecret never calls AWS in unit tests.
export function __seedSecretCacheForTests(map) {
    for (const k of Object.keys(_secretCache)) delete _secretCache[k];
    for (const [arn, value] of Object.entries(map)) _secretCache[arn] = value;
}
/* end ADR 0006 verifier block */

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
        claims = await verifyTwoSecret(token);
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

    // MCP Streamable HTTP: a JSON-RPC *notification* (a message with a
    // `method` but no `id`, e.g. the `notifications/initialized` the
    // streamable-http client sends right after `initialize`) expects no
    // reply — the spec says ack with 202 Accepted and an empty body.
    // The MCP Protocol layer only calls transport.send() for requests,
    // so awaiting transport.dispatch() on a notification never settles
    // and the Lambda is killed mid-promise (Runtime.NodeJsExit -> API
    // Gateway 502). The server is stateless per invocation, so an
    // inbound notification needs no processing — ack and return.
    const _hasId = Object.prototype.hasOwnProperty.call(rpc, 'id')
        && rpc.id !== null && rpc.id !== undefined;
    if (typeof rpc.method === 'string' && !_hasId) {
        logger.info('mcp_notification_ack', { method: rpc.method });
        return { statusCode: 202, body: '' };
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
