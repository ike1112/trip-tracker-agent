import { test } from 'node:test';
import assert from 'node:assert/strict';
import jwt from 'jsonwebtoken';
import { handler, __seedSecretCacheForTests } from '../index.js';

// These ARNs match tests/setup.js. Seeding the cache under exactly
// these keys means getSecret() returns the fake secret and the AWS
// SDK is never called.
const AGENT_ARN = process.env.AGENT_JWT_SECRET_ARN;
const POLLER_ARN = process.env.POLLER_JWT_SECRET_ARN;
const AGENT_SECRET = 'agent-test-secret';
const POLLER_SECRET = 'poller-test-secret';
const FOREIGN_SECRET = 'some-other-secret';

function seed() {
    __seedSecretCacheForTests({
        [AGENT_ARN]: AGENT_SECRET,
        [POLLER_ARN]: POLLER_SECRET,
    });
}

// Real minters always set exp (poller jwt_signer + agent
// mcp_client_manager); mirror that so the happy-path tests reflect
// production. D8 overrides with a negative expiry; D10 signs without
// this helper to exercise the missing-exp path.
function sign(secret, claims, opts = {}) {
    return jwt.sign({ user_id: 'u1', user_name: 'alice', ...claims }, secret, { expiresIn: '5m', ...opts });
}

async function effectFor(authorizationToken) {
    seed();
    const res = await handler({ authorizationToken, methodArn: 'arn:aws:execute-api:::api/stage/GET/mcp' });
    return res.policyDocument.Statement[0].Effect;
}

test('D1 agent secret + sub=travel-agent => Allow', async () => {
    const tok = sign(AGENT_SECRET, { sub: 'travel-agent' });
    assert.equal(await effectFor(`Bearer ${tok}`), 'Allow');
});

test('D2 poller secret + sub=trip-tracker-poller => Allow', async () => {
    const tok = sign(POLLER_SECRET, { sub: 'trip-tracker-poller' });
    assert.equal(await effectFor(`Bearer ${tok}`), 'Allow');
});

test('D3 agent secret + sub=trip-tracker-poller => Deny (cross-sub forgery)', async () => {
    const tok = sign(AGENT_SECRET, { sub: 'trip-tracker-poller' });
    assert.equal(await effectFor(`Bearer ${tok}`), 'Deny');
});

test('D4 poller secret + sub=travel-agent => Deny (cross-sub forgery)', async () => {
    const tok = sign(POLLER_SECRET, { sub: 'travel-agent' });
    assert.equal(await effectFor(`Bearer ${tok}`), 'Deny');
});

test('D5 foreign secret => Deny', async () => {
    const tok = sign(FOREIGN_SECRET, { sub: 'travel-agent' });
    assert.equal(await effectFor(`Bearer ${tok}`), 'Deny');
});

test('D6 malformed bearer header => Deny', async () => {
    assert.equal(await effectFor('not-a-bearer-token'), 'Deny');
});

test('D7 missing authorization token => Deny', async () => {
    assert.equal(await effectFor(undefined), 'Deny');
});

test('D8 expired token => Deny', async () => {
    const tok = sign(AGENT_SECRET, { sub: 'travel-agent' }, { expiresIn: -10 });
    assert.equal(await effectFor(`Bearer ${tok}`), 'Deny');
});

test('D9 alg=none forged token => Deny (HS256 pinned)', async () => {
    const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
    const forged = `${b64({ alg: 'none', typ: 'JWT' })}.${b64({ sub: 'travel-agent', user_id: 'u', exp: 9999999999 })}.`;
    assert.equal(await effectFor(`Bearer ${forged}`), 'Deny');
});

test('D10 valid secret + sub but no exp claim => Deny (expiry enforced at boundary)', async () => {
    const tok = jwt.sign({ sub: 'travel-agent', user_id: 'u', user_name: 'n' }, AGENT_SECRET); // no expiresIn
    assert.equal(await effectFor(`Bearer ${tok}`), 'Deny');
});
