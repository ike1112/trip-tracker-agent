/**
 * End-to-end handler tests. Builds an API GW event with a valid HS256 token,
 * fires it through, asserts on the JSON-RPC response. Catches transport
 * regressions first.
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
    assert.equal(body.result.serverInfo.name, 'hotels-mcp');
});

test('tools/list returns both registered tools with disambiguated names', async () => {
    const resp = await handler(_event({ method: 'tools/list' }));
    assert.equal(resp.statusCode, 200);
    const body = JSON.parse(resp.body);
    const names = body.result.tools.map((t) => t.name).sort();
    assert.deepEqual(names, ['get_hotel_details', 'search_hotel_offers']);
});

test('tools/call routes to search_hotel_offers and returns fixture data', async () => {
    const resp = await handler(_event({
        method: 'tools/call',
        params: {
            name: 'search_hotel_offers',
            arguments: {
                city: 'Tokyo',
                checkin: '2026-10-15',
                checkout: '2026-10-20',
                pax: 1,
            },
        },
    }));
    assert.equal(resp.statusCode, 200);
    const body = JSON.parse(resp.body);
    assert.ok(body.result.content);
    const payload = JSON.parse(body.result.content[0].text);
    assert.equal(payload.source, 'fixture');
    assert.ok(payload.hotels.length >= 1);
});

test('missing Authorization header returns 401', async () => {
    const resp = await handler({ headers: {}, body: '{}' });
    assert.equal(resp.statusCode, 401);
});

test('forged JWT returns 401', async () => {
    const resp = await handler(_event({ method: 'tools/list', signed: false }));
    assert.equal(resp.statusCode, 401);
});
