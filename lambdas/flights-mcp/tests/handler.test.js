/**
 * End-to-end test for the Lambda handler.
 *
 * Builds a real API Gateway-style event with a valid HS256 token, fires it
 * through the handler, and asserts on the JSON-RPC response. This is the
 * test that catches transport regressions — if `LambdaTransport` ever
 * drifts from the MCP SDK's interface, this test breaks first.
 *
 * Covers:
 *   - initialize handshake returns a server identity
 *   - tools/list returns both registered tools
 *   - tools/call routes to search_offers and returns fixture data
 *   - unauthorized event (no Bearer) returns 401
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import jwt from 'jsonwebtoken';
import { handler } from '../index.js';

function _event({ method, params = {}, id = 1, signed = true }) {
    const claims = { sub: 'travel-agent', user_id: 'test-user', user_name: 'Tester' };
    const token = signed
        ? jwt.sign(claims, process.env.JWT_SIGNATURE_SECRET)
        : 'not.a.real.token';
    return {
        headers: { Authorization: `Bearer ${token}` },
        body: JSON.stringify({ jsonrpc: '2.0', method, params, id }),
    };
}

test('initialize handshake returns server identity', async () => {
    const resp = await handler(_event({
        method: 'initialize',
        params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 't', version: '0' } },
    }));
    assert.equal(resp.statusCode, 200);
    const body = JSON.parse(resp.body);
    assert.equal(body.jsonrpc, '2.0');
    assert.equal(body.result.serverInfo.name, 'flights-mcp');
});

test('tools/list returns both registered tools with disambiguated names', async () => {
    const resp = await handler(_event({ method: 'tools/list' }));
    assert.equal(resp.statusCode, 200);
    const body = JSON.parse(resp.body);
    const names = body.result.tools.map((t) => t.name).sort();
    assert.deepEqual(names, ['get_flight_offer_details', 'search_flight_offers']);
});

test('tools/call routes to search_flight_offers and returns fixture data', async () => {
    const resp = await handler(_event({
        method: 'tools/call',
        params: {
            name: 'search_flight_offers',
            arguments: {
                origin: 'SFO',
                destination: 'NRT',
                departDate: '2026-10-15',
                pax: 1,
            },
        },
    }));
    assert.equal(resp.statusCode, 200);
    const body = JSON.parse(resp.body);
    assert.ok(body.result.content);
    const payload = JSON.parse(body.result.content[0].text);
    assert.equal(payload.source, 'fixture');
    assert.ok(payload.offers.length >= 1);
});

test('missing Authorization header returns 401', async () => {
    const resp = await handler({ headers: {}, body: '{}' });
    assert.equal(resp.statusCode, 401);
});

test('forged JWT returns 401', async () => {
    const resp = await handler(_event({ method: 'tools/list', signed: false }));
    assert.equal(resp.statusCode, 401);
});
