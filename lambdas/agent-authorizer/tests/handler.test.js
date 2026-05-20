import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import jwt from 'jsonwebtoken';
import { generateKeyPairSync } from 'node:crypto';
import {
    handler,
    __setSigningKeyForTests,
    __setSigningKeyErrorForTests,
    __resetSigningKeyTestSeams,
} from '../index.js';

// One RSA keypair for the whole suite. Generating per test is wasteful;
// nothing depends on key bytes, only on the sign->verify round-trip.
const { privateKey, publicKey } = generateKeyPairSync('rsa', {
    modulusLength: 2048,
    publicKeyEncoding: { type: 'spki', format: 'pem' },
    privateKeyEncoding: { type: 'pkcs8', format: 'pem' },
});

// A second keypair so we can prove tokens signed by the WRONG private
// key are rejected even when the verifier has a valid public key on hand.
const { privateKey: otherPrivateKey } = generateKeyPairSync('rsa', {
    modulusLength: 2048,
    publicKeyEncoding: { type: 'spki', format: 'pem' },
    privateKeyEncoding: { type: 'pkcs8', format: 'pem' },
});

function sign(claims, opts = {}) {
    return jwt.sign(claims, privateKey, {
        algorithm: 'RS256',
        expiresIn: '5m',
        keyid: 'test-kid',
        ...opts,
    });
}

function event(authorizationToken) {
    return {
        authorizationToken,
        methodArn: 'arn:aws:execute-api:us-east-1:000000000000:abc/prod/POST/chat',
    };
}

beforeEach(() => {
    __resetSigningKeyTestSeams();
    __setSigningKeyForTests(publicKey);
});

test('A1 valid RS256 token (matching kid) => Allow with composite principalId', async () => {
    const tok = sign({ sub: 'user-1', username: 'alice' });
    const res = await handler(event(`Bearer ${tok}`));
    assert.equal(res.policyDocument.Statement[0].Effect, 'Allow');
    assert.equal(res.principalId, 'user-1|alice');
    assert.equal(res.policyDocument.Statement[0].Resource, event('').methodArn);
});

test('A2 missing authorizationToken => Deny', async () => {
    const res = await handler(event(undefined));
    assert.equal(res.policyDocument.Statement[0].Effect, 'Deny');
});

test('A3 authorization header without Bearer prefix => Deny', async () => {
    const tok = sign({ sub: 'user-1', username: 'alice' });
    const res = await handler(event(tok)); // no "Bearer " prefix
    assert.equal(res.policyDocument.Statement[0].Effect, 'Deny');
});

test('A4 expired token => Deny', async () => {
    const tok = sign({ sub: 'user-1', username: 'alice' }, { expiresIn: -10 });
    const res = await handler(event(`Bearer ${tok}`));
    assert.equal(res.policyDocument.Statement[0].Effect, 'Deny');
});

test('A5 alg=none forged token => Deny', async () => {
    const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
    const forged =
        `${b64({ alg: 'none', typ: 'JWT', kid: 'test-kid' })}.` +
        `${b64({ sub: 'user-1', username: 'alice', exp: 9999999999 })}.`;
    const res = await handler(event(`Bearer ${forged}`));
    assert.equal(res.policyDocument.Statement[0].Effect, 'Deny');
});

test('A6 HS256 token signed with public key as HMAC secret => Deny', async () => {
    // Classic algorithm-confusion attack: attacker takes the verifier's
    // public key and uses it as an HMAC secret. The RS256 pin must block this.
    const tok = jwt.sign(
        { sub: 'user-1', username: 'alice' },
        publicKey,
        { algorithm: 'HS256', expiresIn: '5m', keyid: 'test-kid' },
    );
    const res = await handler(event(`Bearer ${tok}`));
    assert.equal(res.policyDocument.Statement[0].Effect, 'Deny');
});

test('A7 token kid not in JWKS => Deny', async () => {
    // Simulate jwks-rsa returning "no key found" by injecting an error.
    __setSigningKeyErrorForTests(new Error('Unable to find a signing key that matches kid "unknown"'));
    const tok = sign({ sub: 'user-1', username: 'alice' }, { keyid: 'unknown' });
    const res = await handler(event(`Bearer ${tok}`));
    assert.equal(res.policyDocument.Statement[0].Effect, 'Deny');
});

test('A8 JWKS fetch throws => Deny (fail-closed) with a distinct ECONNREFUSED reason', async () => {
    // A7 covers "kid not found"; A8 covers "JWKS unreachable". The production
    // code denies for both, but the two failure modes must surface distinct
    // console.error reasons so an operator (or alarm) can tell them apart.
    __setSigningKeyErrorForTests(new Error('ECONNREFUSED'));
    const tok = sign({ sub: 'user-1', username: 'alice' });

    const errs = [];
    const origErr = console.error;
    console.error = (m) => errs.push(String(m));
    try {
        const res = await handler(event(`Bearer ${tok}`));
        assert.equal(res.policyDocument.Statement[0].Effect, 'Deny');
        assert.ok(
            errs.some((e) => e.includes('ECONNREFUSED')),
            `expected ECONNREFUSED reason distinct from A7's "kid" reason; got: ${errs.join(' | ')}`,
        );
    } finally {
        console.error = origErr;
    }
});

test('A9 token signed by a different RSA keypair => Deny', async () => {
    const tok = jwt.sign(
        { sub: 'user-1', username: 'alice' },
        otherPrivateKey,
        { algorithm: 'RS256', expiresIn: '5m', keyid: 'test-kid' },
    );
    const res = await handler(event(`Bearer ${tok}`));
    assert.equal(res.policyDocument.Statement[0].Effect, 'Deny');
});

test('A10 Deny response has no principalId field', async () => {
    // generatePolicy("Deny", arn) is called without a principalId argument.
    // Don't leak attacker-supplied identity into IAM logs on rejection.
    const res = await handler(event(undefined));
    assert.equal(res.policyDocument.Statement[0].Effect, 'Deny');
    assert.equal(res.principalId, undefined);
});
