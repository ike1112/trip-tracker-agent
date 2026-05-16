/**
 * End-to-end test for the Lambda handler.
 *
 * Builds a real API Gateway-style event with a valid HS256 token, fires it
 * through the handler, and asserts on the JSON-RPC response. This is the
 * test that catches transport regressions — if `LambdaTransport` ever
 * drifts from the MCP SDK's interface, this test breaks first.
 *
 * Also pins the ADR 0006 in-handler two-secret + sub-coupling verifier:
 * agent-secret/travel-agent and poller-secret/trip-tracker-poller pass;
 * every cross-sub or foreign-secret combination is rejected 401.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import jwt from 'jsonwebtoken';
import { handler, __seedSecretCacheForTests } from '../index.js';

const AGENT_ARN = process.env.AGENT_JWT_SECRET_ARN;
const POLLER_ARN = process.env.POLLER_JWT_SECRET_ARN;
const AGENT_SECRET = 'agent-test-secret';
const POLLER_SECRET = 'poller-test-secret';
const FOREIGN_SECRET = 'some-other-secret';

function seed() {
    __seedSecretCacheForTests({ [AGENT_ARN]: AGENT_SECRET, [POLLER_ARN]: POLLER_SECRET });
}

// Default: the agent's happy path (agent secret + sub=travel-agent).
function _event({ method, params = {}, id = 1, signed = true, secret = AGENT_SECRET, sub = 'travel-agent' }) {
    seed();
    const token = signed
        ? jwt.sign({ sub, user_id: 'test-user', user_name: 'Tester' }, secret)
        : 'not.a.real.token';
    return {
        headers: { Authorization: `Bearer ${token}` },
        body: JSON.stringify({ jsonrpc: '2.0', method, params, id }),
    };
}

const SEARCH = {
    method: 'tools/call',
    params: {
        name: 'search_flight_offers',
        arguments: { origin: 'SFO', destination: 'NRT', departDate: '2026-10-15', pax: 1 },
    },
};

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

test('F1 agent secret + sub=travel-agent: tools/call returns fixture data', async () => {
    const resp = await handler(_event(SEARCH));
    assert.equal(resp.statusCode, 200);
    const body = JSON.parse(resp.body);
    const payload = JSON.parse(body.result.content[0].text);
    assert.equal(payload.source, 'fixture');
    assert.ok(payload.offers.length >= 1);
});

test('F2 poller secret + sub=trip-tracker-poller passes the handler', async () => {
    const resp = await handler(_event({ method: 'tools/list', secret: POLLER_SECRET, sub: 'trip-tracker-poller' }));
    assert.equal(resp.statusCode, 200);
});

test('F3 agent secret + sub=trip-tracker-poller rejected 401 (cross-sub forgery)', async () => {
    const resp = await handler(_event({ method: 'tools/list', secret: AGENT_SECRET, sub: 'trip-tracker-poller' }));
    assert.equal(resp.statusCode, 401);
});

test('F4 poller secret + sub=travel-agent rejected 401 (cross-sub forgery)', async () => {
    const resp = await handler(_event({ method: 'tools/list', secret: POLLER_SECRET, sub: 'travel-agent' }));
    assert.equal(resp.statusCode, 401);
});

test('F5 foreign secret rejected 401', async () => {
    const resp = await handler(_event({ method: 'tools/list', secret: FOREIGN_SECRET, sub: 'travel-agent' }));
    assert.equal(resp.statusCode, 401);
});

test('F6 poller-signed valid token still reaches the MCP path (post-verify regression)', async () => {
    const resp = await handler(_event({ ...SEARCH, secret: POLLER_SECRET, sub: 'trip-tracker-poller' }));
    assert.equal(resp.statusCode, 200);
    const payload = JSON.parse(JSON.parse(resp.body).result.content[0].text);
    assert.equal(payload.source, 'fixture');
});

test('missing Authorization header returns 401', async () => {
    seed();
    const resp = await handler({ headers: {}, body: '{}' });
    assert.equal(resp.statusCode, 401);
});

test('forged JWT returns 401', async () => {
    const resp = await handler(_event({ method: 'tools/list', signed: false }));
    assert.equal(resp.statusCode, 401);
});
