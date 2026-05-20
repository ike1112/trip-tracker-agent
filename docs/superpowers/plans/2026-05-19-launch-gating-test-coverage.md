# Launch-gating Test Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add launch-gating test coverage to five untested boundaries (Node JWT authorizer, Python Lambda handler, identity data class, custom tools, web OAuth + chat proxy), driven by hybrid TDD. Fix seven latent bugs that the tests surface.

**Architecture:** Five logically independent PRs, each owning one component plus any source fixes that component's tests demand. Each PR adds tests under existing per-package layouts (`lambdas/<pkg>/tests/` for Lambdas, new `web/tests/` for the web app) and wires CI steps that match the repo's `node --test` + per-package `pytest` conventions.

**Tech Stack:** Node 22 + `node --test` + `jsonwebtoken` + `jwks-rsa`. Python 3.12 + `pytest` + `moto` + `PyJWT` + `cryptography` + `starlette.testclient` + `unittest.mock`. GitHub Actions CI.

**Spec reference:** `docs/superpowers/specs/2026-05-19-launch-gating-test-coverage-design.md`

---

## PR 1 — `agent-authorizer` JWT failure matrix

**Files:**
- Modify: `lambdas/agent-authorizer/index.js`
- Modify: `lambdas/agent-authorizer/package.json` (add test script)
- Create: `lambdas/agent-authorizer/tests/setup.js`
- Create: `lambdas/agent-authorizer/tests/handler.test.js`
- Modify: `.github/workflows/ci.yml`

### Task 1.1: Add test seams to authorizer

- [ ] **Step 1: Read the current authorizer**

Run: `cat lambdas/agent-authorizer/index.js`
Expected: 86-line file with `handler`, `getKey`, `generatePolicy`.

- [ ] **Step 2: Add `__setSigningKeyForTests` and `__setSigningKeyErrorForTests` exports**

Edit `lambdas/agent-authorizer/index.js`. Add these test-only seams just below the `const verifyJwt = promisify(jwt.verify);` line and modify `getKey` to consult them. The complete replacement for the existing `getKey` function:

```js
const verifyJwt = promisify(jwt.verify);

// Test seams. Production code paths never touch these (both null in real runs).
// __setSigningKeyForTests injects a fixed public key so tests can verify any
// token without a real JWKS round-trip; __setSigningKeyErrorForTests simulates
// a JWKS lookup failure so the fail-closed branch is testable.
let _testKey = null;
let _testKeyError = null;
export function __setSigningKeyForTests(publicKey) {
    _testKey = publicKey;
    _testKeyError = null;
}
export function __setSigningKeyErrorForTests(err) {
    _testKey = null;
    _testKeyError = err;
}
export function __resetSigningKeyTestSeams() {
    _testKey = null;
    _testKeyError = null;
}

function getKey(header, callback) {
    if (_testKeyError !== null) return callback(_testKeyError);
    if (_testKey !== null) return callback(null, _testKey);
    client.getSigningKey(header.kid, (err, key) => {
        if (err) return callback(err);
        const signingKey = key.getPublicKey();
        callback(null, signingKey);
    });
}
```

- [ ] **Step 3: Commit the test seam in isolation**

```bash
git add lambdas/agent-authorizer/index.js
git commit -m "agent-authorizer: add test seams for signing key + JWKS error"
```

### Task 1.2: Add test runner config

- [ ] **Step 1: Update `lambdas/agent-authorizer/package.json`**

Edit so it matches the existing pattern from `lambdas/mcp-authorizer/package.json`:

```json
{
  "type": "module",
  "author": "Anton Aleksandrov",
  "license": "Apache-2.0",
  "description": "A simple API Gateway authorizer",
  "scripts": {
    "test": "node --import ./tests/setup.js --test --test-reporter=spec \"tests/*.test.js\""
  },
  "dependencies": {
    "jsonwebtoken": "^9.0.2",
    "jwks-rsa": "^3.2.0",
    "promisify": "^0.0.3"
  }
}
```

- [ ] **Step 2: Create `lambdas/agent-authorizer/tests/setup.js`**

```js
// Loaded via `node --import ./tests/setup.js` before any test module.
// Sets a stub COGNITO_JWKS_URL so the jwks-rsa client constructs without
// error. All real key resolution is overridden via __setSigningKeyForTests
// in the test file, so the URL is never actually fetched.
process.env.COGNITO_JWKS_URL ??= 'https://example.invalid/.well-known/jwks.json';
```

### Task 1.3: Write the JWT failure matrix

- [ ] **Step 1: Create `lambdas/agent-authorizer/tests/handler.test.js`**

Complete file:

```js
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
// nothing depends on key bytes, only on the sign→verify round-trip.
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

test('A8 JWKS fetch throws => Deny (fail-closed)', async () => {
    __setSigningKeyErrorForTests(new Error('ECONNREFUSED'));
    const tok = sign({ sub: 'user-1', username: 'alice' });
    const res = await handler(event(`Bearer ${tok}`));
    assert.equal(res.policyDocument.Statement[0].Effect, 'Deny');
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
```

- [ ] **Step 2: Run the suite**

```bash
cd lambdas/agent-authorizer
npm install
npm test
```

Expected: 10 tests, all pass.

- [ ] **Step 3: Commit**

```bash
git add lambdas/agent-authorizer/package.json \
        lambdas/agent-authorizer/tests/setup.js \
        lambdas/agent-authorizer/tests/handler.test.js
git commit -m "agent-authorizer: JWT failure matrix tests (A1..A10)"
```

### Task 1.4: Wire into CI

- [ ] **Step 1: Add a step to `.github/workflows/ci.yml` under `node-tests`**

Insert this block right after the existing `mcp-authorizer tests` step:

```yaml
      - name: agent-authorizer tests
        run: npm install && npm test
        working-directory: lambdas/agent-authorizer
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run agent-authorizer tests in node-tests job"
```

---

## PR 2 — `travel-agent/app.py` handler

**Files:**
- Modify: `lambdas/travel-agent/app.py` (handler robustness fix)
- Modify: `lambdas/travel-agent/tests/conftest.py` (new fixtures)
- Create: `lambdas/travel-agent/tests/test_app.py`

### Task 2.1: Extend `conftest.py` with JWT and app-import fixtures

- [ ] **Step 1: Edit `lambdas/travel-agent/tests/conftest.py`** — append the following at the end (after the existing `watches_module` fixture):

```python
# ---------------------------------------------------------------------------
# JWT + app-import fixtures (used by test_app.py)
# ---------------------------------------------------------------------------

import importlib
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(scope="session")
def rsa_keypair():
    """One RSA keypair for the whole test session. Tests sign tokens with
    the private key; the handler under test verifies with the public key
    we inject via the JWKS client mock."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return {
        "private_pem": private_pem,
        "public_obj": private.public_key(),
        "private_obj": private,
    }


@pytest.fixture(scope="session")
def other_rsa_keypair():
    """A second keypair used to forge tokens the verifier should reject."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return {"private_pem": private_pem}


def make_token(rsa_keypair, claims=None, algorithm="RS256", key=None, **opts):
    """Build a signed JWT. Defaults: RS256, exp=+5min, sub=user-1, username=alice."""
    payload = {"sub": "user-1", "username": "alice"}
    if claims:
        payload.update(claims)
    if "exp" not in payload:
        payload["exp"] = int(
            (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
        )
    signing_key = key if key is not None else rsa_keypair["private_pem"]
    return pyjwt.encode(payload, signing_key, algorithm=algorithm)


@pytest.fixture
def app_module(rsa_keypair, monkeypatch):
    """Import lambdas/travel-agent/app.py with the heavy `agent` import
    replaced by a MagicMock, env vars set, and the JWKS client patched
    to return our test public key."""
    import sys

    # Stub the `agent` module before app imports it. app.py does
    # `import agent` at module scope and calls agent.prompt(user, prompt).
    mock_agent = MagicMock()
    mock_agent.prompt = MagicMock(return_value="mocked agent response")
    monkeypatch.setitem(sys.modules, "agent", mock_agent)

    monkeypatch.setenv("COGNITO_JWKS_URL", "https://example.invalid/.well-known/jwks.json")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    # Reimport app fresh so module-level code (jwks_client construction,
    # logger init) reruns under the patched env.
    sys.modules.pop("app", None)
    app = importlib.import_module("app")

    # Patch the PyJWKClient instance to return our test public key
    # regardless of the token's kid. The handler calls
    # `jwks_client.get_signing_key_from_jwt(token)` and reads `.key`.
    mock_signing_key = MagicMock()
    mock_signing_key.key = rsa_keypair["public_obj"]
    app.jwks_client.get_signing_key_from_jwt = MagicMock(return_value=mock_signing_key)

    try:
        yield app, mock_agent
    finally:
        sys.modules.pop("app", None)


def make_event(token, body=None, source_ip="70.200.50.45"):
    """Build an API Gateway event in the shape app.handler reads."""
    import json
    if body is None:
        body = {"text": "Book me a trip to Tokyo"}
    return {
        "headers": {"Authorization": f"Bearer {token}"},
        "requestContext": {"identity": {"sourceIp": source_ip}},
        "body": json.dumps(body) if isinstance(body, dict) else body,
    }
```

- [ ] **Step 2: Confirm the conftest imports — `cryptography` and `PyJWT` are already in `requirements-test.txt`. No requirement changes.**

### Task 2.2: Write characterization tests (B1–B5) — all green

- [ ] **Step 1: Create `lambdas/travel-agent/tests/test_app.py`**

```python
"""
Tests for the travel-agent Lambda handler.

The handler authenticates the caller, builds a composite prompt, and calls
agent.prompt. These tests assert the contract at the Lambda boundary:
401 on auth failure, 400 on malformed input, 200 on success. The actual
agent invocation is mocked (see conftest.app_module).
"""

import json
from conftest import make_token, make_event


# ---------------------------------------------------------------------------
# Characterization — happy path and basic auth contract
# ---------------------------------------------------------------------------

def test_B1_valid_jwt_returns_200_and_calls_agent_prompt(app_module, rsa_keypair):
    app, mock_agent = app_module
    tok = make_token(rsa_keypair)
    response = app.handler(make_event(tok), None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body == {"text": "mocked agent response"}
    assert mock_agent.prompt.call_count == 1


def test_B2_missing_authorization_header_returns_401(app_module, rsa_keypair):
    app, _ = app_module
    event = make_event(make_token(rsa_keypair))
    event["headers"] = {}  # strip auth
    response = app.handler(event, None)

    assert response["statusCode"] == 401
    assert response["body"] == "Unauthorized"


def test_B3_malformed_authorization_header_returns_401(app_module, rsa_keypair):
    app, _ = app_module
    event = make_event(make_token(rsa_keypair))
    event["headers"]["Authorization"] = "NotBearer something"
    response = app.handler(event, None)

    assert response["statusCode"] == 401


def test_B4_composite_prompt_contains_user_ip_and_text_in_exact_order(
    app_module, rsa_keypair
):
    app, mock_agent = app_module
    tok = make_token(rsa_keypair, claims={"username": "alice"})
    app.handler(make_event(tok, body={"text": "trip to Tokyo"}, source_ip="1.2.3.4"), None)

    _, composite_prompt = mock_agent.prompt.call_args[0]
    assert composite_prompt == (
        "User name: alice\n"
        "User IP: 1.2.3.4\n"
        "User prompt: trip to Tokyo"
    )


def test_B5_user_id_built_from_claims_sub_not_username(app_module, rsa_keypair):
    """Multi-tenancy invariant: identity is bound to the cryptographic 'sub'
    claim, not any human-controlled 'username' field. A token with a forged
    username must not let one user impersonate another."""
    app, mock_agent = app_module
    tok = make_token(rsa_keypair, claims={"sub": "user-real-id", "username": "victim"})
    app.handler(make_event(tok), None)

    user_arg, _ = mock_agent.prompt.call_args[0]
    assert user_arg.id == "user-real-id"
    assert user_arg.name == "victim"  # name is human-display only; id is the trust anchor
```

- [ ] **Step 2: Run B1–B5**

```bash
cd lambdas/travel-agent
python -m pytest tests/test_app.py -v -k "B1 or B2 or B3 or B4 or B5"
```

Expected: 5 passed.

- [ ] **Step 3: Commit**

```bash
git add lambdas/travel-agent/tests/conftest.py lambdas/travel-agent/tests/test_app.py
git commit -m "travel-agent: characterize handler auth + prompt composition"
```

### Task 2.3: Write JWT failure matrix (B6–B8) — all green

- [ ] **Step 1: Append to `test_app.py`**

```python
# ---------------------------------------------------------------------------
# JWT failure matrix (mirrors agent-authorizer A4..A6)
# ---------------------------------------------------------------------------

def test_B6_expired_token_returns_401(app_module, rsa_keypair):
    from datetime import datetime, timezone
    app, _ = app_module
    expired_exp = int(datetime.now(timezone.utc).timestamp()) - 60
    tok = make_token(rsa_keypair, claims={"exp": expired_exp})
    response = app.handler(make_event(tok), None)
    assert response["statusCode"] == 401


def test_B7_token_signed_by_wrong_key_returns_401(app_module, other_rsa_keypair):
    app, _ = app_module
    tok = make_token(other_rsa_keypair, key=other_rsa_keypair["private_pem"])
    response = app.handler(make_event(tok), None)
    assert response["statusCode"] == 401


def test_B8_hs256_token_returns_401(app_module, rsa_keypair):
    """Algorithm-confusion guard: PyJWT.decode(algorithms=['RS256']) must
    reject any token whose header says HS256, regardless of payload."""
    import jwt as pyjwt
    tok = pyjwt.encode(
        {"sub": "user-1", "username": "alice", "exp": 9999999999},
        "any-shared-secret",
        algorithm="HS256",
    )
    app, _ = app_module
    response = app.handler(make_event(tok), None)
    assert response["statusCode"] == 401
```

- [ ] **Step 2: Run B6–B8**

```bash
cd lambdas/travel-agent
python -m pytest tests/test_app.py -v -k "B6 or B7 or B8"
```

Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add lambdas/travel-agent/tests/test_app.py
git commit -m "travel-agent: JWT failure matrix tests (expired, wrong key, HS256)"
```

### Task 2.4: Write B9–B11 (red) — handler robustness tests

- [ ] **Step 1: Append failing tests to `test_app.py`**

```python
# ---------------------------------------------------------------------------
# Handler robustness — these RED tests drive the bug fix in Task 2.5
# ---------------------------------------------------------------------------

def test_B9_malformed_json_body_returns_400(app_module, rsa_keypair):
    app, _ = app_module
    event = make_event(make_token(rsa_keypair), body="{not valid json")
    response = app.handler(event, None)
    assert response["statusCode"] == 400
    assert response["body"] == "Bad Request"


def test_B10_missing_text_key_returns_400(app_module, rsa_keypair):
    app, _ = app_module
    event = make_event(make_token(rsa_keypair), body={"not_text": "oops"})
    response = app.handler(event, None)
    assert response["statusCode"] == 400


def test_B11_missing_source_ip_returns_400(app_module, rsa_keypair):
    app, _ = app_module
    event = make_event(make_token(rsa_keypair))
    event["requestContext"] = {"identity": {}}  # no sourceIp
    response = app.handler(event, None)
    assert response["statusCode"] == 400
```

- [ ] **Step 2: Run them — expect failure**

```bash
cd lambdas/travel-agent
python -m pytest tests/test_app.py -v -k "B9 or B10 or B11"
```

Expected: 3 failed. B9 fails with a `JSONDecodeError`; B10 fails with `KeyError: 'text'`; B11 fails with `KeyError: 'sourceIp'`. Each currently raises uncaught, which pytest reports as ERROR rather than a structured `statusCode: 400` response.

### Task 2.5: Fix handler robustness (green)

- [ ] **Step 1: Modify `lambdas/travel-agent/app.py`**

Find this block (it sits between the `except Exception` block that returns
401 and the `response_text = agent.prompt(...)` call):

```python
    # Capture request metadata separately from the prompt text. The source IP is
    # included in the prompt as contextual signal; policy or auditing logic can
    # use it without needing direct access to the raw API Gateway event later.
    source_ip = event["requestContext"]["identity"]["sourceIp"]
    request_body: dict = json.loads(event["body"])
    prompt_text = request_body["text"]

    # Build a single composite prompt at the boundary. This keeps the agent API
    # simple (one string in, one string out) while still giving the model enough
    # structured context about the user and request.
    composite_prompt = f"User name: {user.name}\n"
    composite_prompt += f"User IP: {source_ip}\n"
    composite_prompt += f"User prompt: {prompt_text}"
    l.info(f"composite_prompt={composite_prompt}")
```

Replace it with:

```python
    try:
        # Extract request metadata after auth succeeds. Wrapped so a malformed
        # body or missing field returns a structured 400 instead of crashing
        # the Lambda invocation (which API Gateway would surface as 502).
        source_ip = event["requestContext"]["identity"]["sourceIp"]
        request_body: dict = json.loads(event["body"])
        prompt_text = request_body["text"]
    except (KeyError, json.JSONDecodeError, TypeError):
        l.error("malformed request body or missing required field", exc_info=True)
        return {
            "statusCode": 400,
            "body": "Bad Request",
        }

    # Build a single composite prompt at the boundary. This keeps the agent API
    # simple (one string in, one string out) while still giving the model enough
    # structured context about the user and request.
    composite_prompt = f"User name: {user.name}\n"
    composite_prompt += f"User IP: {source_ip}\n"
    composite_prompt += f"User prompt: {prompt_text}"
    l.info(f"composite_prompt={composite_prompt}")
```

- [ ] **Step 2: Re-run the full app suite**

```bash
cd lambdas/travel-agent
python -m pytest tests/test_app.py -v
```

Expected: 11 passed (B1–B11 all green).

- [ ] **Step 3: Run the pre-existing watches suite to confirm no regression**

```bash
cd lambdas/travel-agent
python -m pytest tests/ -v
```

Expected: all green (existing `test_watches.py` plus the new `test_app.py`).

- [ ] **Step 4: Commit**

```bash
git add lambdas/travel-agent/app.py lambdas/travel-agent/tests/test_app.py
git commit -m "travel-agent: return 400 on malformed body or missing field"
```

---

## PR 3 — `web/oauth.py` Cognito flow

**Files:**
- Modify: `web/oauth.py` (hoist `oauth = OAuth()`; env-driven URLs)
- Create: `web/.env.example`
- Create: `web/tests/__init__.py`
- Create: `web/tests/conftest.py`
- Create: `web/tests/test_oauth.py`
- Modify: `.github/workflows/ci.yml`

### Task 3.1: Refactor `oauth.py` for testability — hoist `OAuth()` to module scope

This is a behavior-preserving refactor that makes `oauth.cognito` patchable from tests. Run before any test writing because every test depends on it.

- [ ] **Step 1: Edit `web/oauth.py`**

Keep the existing top-of-file comment block (lines 1–6 of the current file —
the four lines starting with `# This module solves the problem…`). Replace
**everything from the `from fastapi import …` line through the end of the
file** with the block below. This hoists `oauth = OAuth()` to module scope
and adds env-driven URL defaults.

```python
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth
import os

# Configuration — all values come from environment variables so the same
# image works in every environment.
COGNITO_SIGNIN_URL = os.getenv("COGNITO_SIGNIN_URL")
COGNITO_LOGOUT_URL = os.getenv("COGNITO_LOGOUT_URL")
COGNITO_WELL_KNOWN_ENDPOINT_URL = os.getenv("COGNITO_WELL_KNOWN_URL")
COGNITO_CLIENT_ID = os.getenv("COGNITO_CLIENT_ID")
COGNITO_CLIENT_SECRET = os.getenv("COGNITO_CLIENT_SECRET")

# OAuth callback and post-logout URLs come from env so the deployed image
# can point at a real host instead of localhost. Tests stub these via
# monkeypatch.setenv before importing this module.
OAUTH_CALLBACK_URI = os.getenv("OAUTH_CALLBACK_URI", "http://localhost:8000/callback")
OAUTH_POST_LOGOUT_URL = os.getenv("OAUTH_POST_LOGOUT_URL", "http://localhost:8000/chat")

# OAuth client hoisted to module scope so tests can patch
# `web.oauth.oauth.cognito.authorize_redirect` and `.authorize_access_token`.
# Authlib reads server_metadata_url lazily, so constructing here does not
# make a network call at import time.
oauth = OAuth()
oauth.register(
    name="cognito",
    client_id=COGNITO_CLIENT_ID,
    client_secret=COGNITO_CLIENT_SECRET,
    client_kwargs={"scope": "openid email profile"},
    server_metadata_url=COGNITO_WELL_KNOWN_ENDPOINT_URL,
    redirect_uri=OAUTH_CALLBACK_URI,
)


def add_oauth_routes(fastapi_app: FastAPI):
    """Register /login, /callback, /logout on the FastAPI app."""

    @fastapi_app.get("/login")
    async def login(req: Request):
        return await oauth.cognito.authorize_redirect(req, OAUTH_CALLBACK_URI)

    @fastapi_app.get("/callback")
    async def callback(req: Request):
        tokens = await oauth.cognito.authorize_access_token(req)
        access_token = tokens["access_token"]
        username = tokens["userinfo"]["cognito:username"]
        req.session["access_token"] = access_token
        req.session["username"] = username
        return RedirectResponse(url="/chat")

    @fastapi_app.get("/logout")
    async def logout(req: Request):
        req.session.clear()
        logout_url = f"{COGNITO_LOGOUT_URL}&logout_uri={OAUTH_POST_LOGOUT_URL}"
        return RedirectResponse(url=logout_url)
```

Note: this step intentionally keeps `os.getenv` defaults at the localhost
values so the existing dev workflow still works. The next task (E4/E5 tests)
will assert the env override path; the defaults stay as a soft fallback for
local-dev ergonomics, but the deployed env always sets them explicitly.

- [ ] **Step 2: Quick sanity check the refactor — module imports cleanly**

```bash
cd web
python -c "import oauth; print('module-level oauth.cognito present:', hasattr(oauth.oauth, 'cognito'))"
```

Expected output: `module-level oauth.cognito present: True`.

- [ ] **Step 3: Commit the refactor in isolation**

```bash
git add web/oauth.py
git commit -m "web/oauth: hoist OAuth() to module scope for testability"
```

### Task 3.2: Create the web test workspace

- [ ] **Step 1: Create `web/tests/__init__.py`** as an empty file.

- [ ] **Step 2: Create `web/.env.example`**

```
# Cognito hosted-UI configuration
COGNITO_SIGNIN_URL=https://your-domain.auth.us-east-1.amazoncognito.com
COGNITO_LOGOUT_URL=https://your-domain.auth.us-east-1.amazoncognito.com/logout?client_id=YOUR_ID&response_type=code
COGNITO_WELL_KNOWN_URL=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_xxx/.well-known/openid-configuration
COGNITO_CLIENT_ID=your-cognito-client-id
COGNITO_CLIENT_SECRET=your-cognito-client-secret

# OAuth redirect endpoints (set to your deployed host, not localhost, in prod)
OAUTH_CALLBACK_URI=https://your-app.example.com/callback
OAUTH_POST_LOGOUT_URL=https://your-app.example.com/chat

# Where the agent Lambda lives
AGENT_ENDPOINT_URL=https://your-api.execute-api.us-east-1.amazonaws.com/prod/chat

# Session-cookie signing key. Must be a strong random string in production.
# Generate with: python -c "import secrets; print(secrets.token_urlsafe(48))"
SESSION_SECRET_KEY=replace-me-with-a-real-random-string
```

- [ ] **Step 3: Create `web/tests/conftest.py`**

```python
"""
Shared fixtures for the web test suite.

The web app reads env vars at module import; tests must set them BEFORE
importing app or oauth. The fixtures below do that under monkeypatch and
reimport the modules per test so module-level state cannot leak.
"""

import importlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _web_env(monkeypatch):
    """Provide a complete env set so module imports succeed.

    Tests that exercise the bug-fix path (F7/F8) override SESSION_SECRET_KEY
    explicitly to assert fail-fast on missing/bad values.
    """
    monkeypatch.setenv("COGNITO_SIGNIN_URL", "https://example.invalid/signin")
    monkeypatch.setenv("COGNITO_LOGOUT_URL", "https://example.invalid/logout?client_id=x&response_type=code")
    monkeypatch.setenv("COGNITO_WELL_KNOWN_URL", "https://example.invalid/.well-known/openid-configuration")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("COGNITO_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("OAUTH_CALLBACK_URI", "https://test.example.com/callback")
    monkeypatch.setenv("OAUTH_POST_LOGOUT_URL", "https://test.example.com/chat")
    monkeypatch.setenv("AGENT_ENDPOINT_URL", "https://agent.example.test/chat")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-key-do-not-use-in-prod")
    yield
    for m in ("app", "oauth"):
        sys.modules.pop(m, None)


@pytest.fixture
def oauth_module():
    """Fresh import of web.oauth with env applied. The autouse fixture sets env;
    we pop and reimport here so each test gets a clean module."""
    sys.modules.pop("oauth", None)
    return importlib.import_module("oauth")


@pytest.fixture
def fastapi_app_with_routes(oauth_module):
    """A FastAPI app with the OAuth routes wired and the Authlib client
    patched. Tests drive it via starlette.testclient.TestClient."""
    from fastapi import FastAPI
    from starlette.middleware.sessions import SessionMiddleware

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-key")

    # Patch the Authlib seams BEFORE registering routes so the route handlers
    # close over the mocked methods.
    oauth_module.oauth.cognito.authorize_redirect = AsyncMock()
    oauth_module.oauth.cognito.authorize_access_token = AsyncMock()

    oauth_module.add_oauth_routes(app)
    return app, oauth_module
```

### Task 3.3: Wire web tests into CI

- [ ] **Step 1: Add a step to `.github/workflows/ci.yml` under `python-tests`**

Insert this block right after the existing `travel-agent` step:

```yaml
      - name: web
        run: python -m pytest tests/ -q
        working-directory: web
```

- [ ] **Step 2: Commit (CI step + scaffold together)**

```bash
git add web/tests/__init__.py web/tests/conftest.py web/.env.example .github/workflows/ci.yml
git commit -m "web: scaffold pytest workspace + CI step + env example"
```

### Task 3.4: Write E1–E3 (characterization)

- [ ] **Step 1: Create `web/tests/test_oauth.py`**

```python
"""
Tests for web/oauth.py — the Cognito OAuth flow.

These tests assert the contract of /login, /callback, /logout against a
FastAPI app with the Authlib client patched. Network calls to Cognito are
never made in tests.
"""

from fastapi.responses import RedirectResponse
from starlette.testclient import TestClient
from unittest.mock import AsyncMock


def test_E1_login_redirects_via_authlib(fastapi_app_with_routes):
    app, oauth_module = fastapi_app_with_routes
    # authorize_redirect returns a RedirectResponse in production; mirror that.
    oauth_module.oauth.cognito.authorize_redirect = AsyncMock(
        return_value=RedirectResponse(url="https://cognito.example/authorize?state=xyz")
    )

    client = TestClient(app)
    response = client.get("/login", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"].startswith("https://cognito.example/authorize")
    assert oauth_module.oauth.cognito.authorize_redirect.await_count == 1


def test_E2_callback_stores_session_and_redirects_to_chat(fastapi_app_with_routes):
    app, oauth_module = fastapi_app_with_routes
    oauth_module.oauth.cognito.authorize_access_token = AsyncMock(
        return_value={
            "access_token": "tkn-abc",
            "userinfo": {"cognito:username": "alice"},
        }
    )

    client = TestClient(app)
    response = client.get("/callback?code=abc", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/chat"
    # Session is signed; we cannot read its values from the cookie directly
    # without the same secret + signer. The next-request assertion below
    # uses the session to confirm it was set.

    # Drive another request to verify the session sticks (TestClient persists cookies).
    # We mount a trivial endpoint to inspect req.session.
    from starlette.requests import Request as StarletteRequest
    @app.get("/_session_dump")
    async def _dump(req: StarletteRequest):
        return dict(req.session)

    inspect = client.get("/_session_dump")
    assert inspect.json() == {"access_token": "tkn-abc", "username": "alice"}


def test_E3_logout_clears_session_and_redirects_with_logout_uri(fastapi_app_with_routes):
    app, oauth_module = fastapi_app_with_routes
    # Seed a session by calling /callback first.
    oauth_module.oauth.cognito.authorize_access_token = AsyncMock(
        return_value={"access_token": "tkn-abc", "userinfo": {"cognito:username": "alice"}}
    )
    client = TestClient(app)
    client.get("/callback?code=abc")

    response = client.get("/logout", follow_redirects=False)
    assert response.status_code == 307
    assert "logout_uri=https://test.example.com/chat" in response.headers["location"]
```

- [ ] **Step 2: Run E1–E3**

```bash
cd web
python -m pytest tests/test_oauth.py -v -k "E1 or E2 or E3"
```

Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add web/tests/test_oauth.py
git commit -m "web/oauth: characterize /login, /callback, /logout flow"
```

### Task 3.5: Write E4 (red) → fix → green

- [ ] **Step 1: Append E4 to `test_oauth.py`**

```python
def test_E4_callback_uri_comes_from_env_not_localhost(fastapi_app_with_routes, monkeypatch):
    """The redirect_uri passed to authorize_redirect must come from
    OAUTH_CALLBACK_URI, not the hard-coded localhost default."""
    app, oauth_module = fastapi_app_with_routes
    from fastapi.responses import RedirectResponse
    oauth_module.oauth.cognito.authorize_redirect = AsyncMock(
        return_value=RedirectResponse(url="https://x/")
    )

    client = TestClient(app)
    client.get("/login")

    # authorize_redirect is called with (req, callback_uri)
    _, callback_uri = oauth_module.oauth.cognito.authorize_redirect.call_args[0]
    assert callback_uri == "https://test.example.com/callback"
    assert "localhost" not in callback_uri
```

- [ ] **Step 2: Run E4 — verify pass (the Task 3.1 refactor already wired this)**

```bash
cd web
python -m pytest tests/test_oauth.py -v -k "E4"
```

Expected: 1 passed.

If E4 fails: the Task 3.1 refactor wasn't applied; revisit `web/oauth.py` and confirm `OAUTH_CALLBACK_URI = os.getenv("OAUTH_CALLBACK_URI", ...)` is at module scope and the `/login` handler passes it to `authorize_redirect`.

### Task 3.6: Write E5 (logout URI from env)

- [ ] **Step 1: Append E5 to `test_oauth.py`**

```python
def test_E5_post_logout_uri_comes_from_env_not_localhost(fastapi_app_with_routes):
    app, oauth_module = fastapi_app_with_routes
    oauth_module.oauth.cognito.authorize_access_token = AsyncMock(
        return_value={"access_token": "t", "userinfo": {"cognito:username": "u"}}
    )
    client = TestClient(app)
    client.get("/callback?code=abc")

    response = client.get("/logout", follow_redirects=False)
    location = response.headers["location"]
    assert "logout_uri=https://test.example.com/chat" in location
    assert "localhost" not in location
```

- [ ] **Step 2: Run E5**

```bash
cd web
python -m pytest tests/test_oauth.py -v -k "E5"
```

Expected: 1 passed (Task 3.1 already wired the env override).

- [ ] **Step 3: Commit**

```bash
git add web/tests/test_oauth.py
git commit -m "web/oauth: assert callback + logout URIs read from env, not localhost"
```

---

## PR 4 — `travel-agent/user.py` + `tools.py`

**Files:**
- Create: `lambdas/travel-agent/tests/test_user.py`
- Create: `lambdas/travel-agent/tests/test_tools.py`

### Task 4.1: Write `test_user.py`

- [ ] **Step 1: Create `lambdas/travel-agent/tests/test_user.py`**

```python
"""
Tests for the User identity data class.

Locks the contract between `claims["sub"]` / `claims["username"]` and
the rest of the agent stack. If this class grows validation later, those
test changes will surface here.
"""

from user import User


def test_C1_user_stores_id_and_name():
    u = User(id="user-abc", name="alice")
    assert u.id == "user-abc"
    assert u.name == "alice"


def test_C2_user_accepts_any_string_no_validation():
    """No validation today. Locked so any future 'add validation' change
    must update this test, surfacing the behavior change in review."""
    u = User(id="", name="")
    assert u.id == ""
    assert u.name == ""
```

- [ ] **Step 2: Run it**

```bash
cd lambdas/travel-agent
python -m pytest tests/test_user.py -v
```

Expected: 2 passed.

### Task 4.2: Write `test_tools.py`

- [ ] **Step 1: Create `lambdas/travel-agent/tests/test_tools.py`**

```python
"""
Tests for the local (non-user-scoped) tools.

get_user_location resolves an IP via ip-api.com. get_todays_date returns
the system date. Tests mock the network and freeze time.
"""

import json
from unittest.mock import MagicMock, patch
import pytest


def _fake_urlopen_response(payload: dict):
    """Return an object shaped like the urlopen return: .read() yields bytes."""
    fake = MagicMock()
    fake.read.return_value = json.dumps(payload).encode("utf-8")
    return fake


def test_D1_get_user_location_formats_city_region_country():
    import tools
    payload = {"city": "Seattle", "region": "Washington", "country": "United States"}
    with patch.object(tools.request, "urlopen", return_value=_fake_urlopen_response(payload)):
        result = tools.get_user_location("203.0.113.42")
    assert result == "Seattle Washington, United States"


def test_D2_get_user_location_propagates_json_decode_error():
    import tools
    fake = MagicMock()
    fake.read.return_value = b"not json"
    with patch.object(tools.request, "urlopen", return_value=fake):
        with pytest.raises(json.JSONDecodeError):
            tools.get_user_location("203.0.113.42")


def test_D3_get_user_location_propagates_missing_field():
    import tools
    payload = {"city": "Seattle"}  # region + country missing
    with patch.object(tools.request, "urlopen", return_value=_fake_urlopen_response(payload)):
        with pytest.raises(KeyError):
            tools.get_user_location("203.0.113.42")


def test_D4_get_todays_date_returns_iso_yyyy_mm_dd(monkeypatch):
    import tools
    from datetime import datetime

    class FrozenDatetime(datetime):
        @classmethod
        def today(cls):
            return cls(2026, 5, 19)

    monkeypatch.setattr(tools, "datetime", FrozenDatetime)
    assert tools.get_todays_date() == "2026-05-19"
```

- [ ] **Step 2: Run it**

```bash
cd lambdas/travel-agent
python -m pytest tests/test_tools.py -v
```

Expected: 4 passed.

- [ ] **Step 3: Run the full travel-agent suite to confirm nothing regressed**

```bash
cd lambdas/travel-agent
python -m pytest tests/ -v
```

Expected: 19 passed (existing watches + new app + new user + new tools).

- [ ] **Step 4: Commit**

```bash
git add lambdas/travel-agent/tests/test_user.py lambdas/travel-agent/tests/test_tools.py
git commit -m "travel-agent: cover User dataclass + local tools"
```

---

## PR 5 — `web/app.py` (session key + chat proxy)

**Files:**
- Modify: `web/app.py` (rename Gradio interface var + read SESSION_SECRET_KEY from env)
- Create: `web/tests/test_app.py`
- Modify: `web/.env.example` (already contains `SESSION_SECRET_KEY` from PR 3 — verify)

### Task 5.0: Testability refactor — stop the Gradio interface from shadowing `chat`

`web/app.py` currently does `chat = gr.ChatInterface(fn=chat, ...)` inside
the `gr.Blocks()` context, rebinding the module-level `chat` from the
function to a Gradio component. After import, `app.chat` is the component,
not the function — tests can't call it. Rename the component so the
function stays accessible.

- [ ] **Step 1: Edit `web/app.py`** — find the Gradio Blocks block and
      rename the local. Find:

```python
    chat = gr.ChatInterface(
        fn=chat,
        type="messages",
        chatbot=gr.Chatbot(
            type="messages",
            label="Track a trip's flight + hotel price over time",
            avatar_images=(user_avatar, bot_avatar),
            placeholder="<b>Trip Tracker</b> — describe a trip and I'll watch its price."
        )
    )
```

Replace with:

```python
    chat_interface = gr.ChatInterface(
        fn=chat,
        type="messages",
        chatbot=gr.Chatbot(
            type="messages",
            label="Track a trip's flight + hotel price over time",
            avatar_images=(user_avatar, bot_avatar),
            placeholder="<b>Trip Tracker</b> — describe a trip and I'll watch its price."
        )
    )
```

Then find:

```python
    gradio_app.load(on_gradio_app_load, inputs=None, outputs=[logout_button, chat.chatbot])
```

Replace with:

```python
    gradio_app.load(on_gradio_app_load, inputs=None, outputs=[logout_button, chat_interface.chatbot])
```

- [ ] **Step 2: Sanity check via pytest collection (cross-platform)**

```bash
cd web
python -m pytest tests/ --collect-only -q
```

Expected: no import errors. (The conftest autouse fixture provides
`SESSION_SECRET_KEY` and the other env vars `app.py` needs at import.)

- [ ] **Step 3: Commit the refactor in isolation**

```bash
git add web/app.py
git commit -m "web/app: rename Gradio interface var so chat function stays importable"
```

### Task 5.1: Write F1–F6 characterization

- [ ] **Step 1: Create `web/tests/test_app.py`**

```python
"""
Tests for web/app.py — the FastAPI host that mounts the Gradio chat UI.

These cover the auth dependency (check_auth) and the chat proxy that
forwards user messages to the agent Lambda. The Gradio mount itself is
not exercised here; it's a thin wrapper over the chat() function.
"""

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException


@pytest.fixture
def app_module():
    """Import web.app with env set; pop after to avoid leakage."""
    sys.modules.pop("app", None)
    return importlib.import_module("app")


def _httpx_response(status=200, body=None):
    """Build a fake httpx.Response shape that chat() reads from."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else {"text": "ok"}
    return resp


# ---------------------------------------------------------------------------
# check_auth dependency
# ---------------------------------------------------------------------------

def test_F1_check_auth_missing_session_raises_302_to_login(app_module):
    req = MagicMock()
    req.session = {}  # no access_token, no username
    with pytest.raises(HTTPException) as exc_info:
        app_module.check_auth(req)
    assert exc_info.value.status_code == 302
    assert exc_info.value.headers["Location"] == "/login"


def test_F2_check_auth_valid_session_returns_username(app_module):
    req = MagicMock()
    req.session = {"access_token": "tkn", "username": "alice"}
    assert app_module.check_auth(req) == "alice"


# ---------------------------------------------------------------------------
# chat() proxy — patch httpx.post directly (responses library only patches
# the `requests` library, which web/app.py does not use).
# ---------------------------------------------------------------------------

def _fake_request(token, username="alice"):
    """Build the gr.Request-like object chat() reads from."""
    request = MagicMock()
    request.username = username
    request.request = MagicMock()
    request.request.session = {"access_token": token}
    return request


def test_F3_chat_returns_agent_text_on_200(app_module):
    with patch.object(app_module.httpx, "post", return_value=_httpx_response(
        status=200, body={"text": "Sure, watching that trip"}
    )):
        result = app_module.chat("Track Tokyo", history=[], request=_fake_request("tkn"))
    assert result == "Sure, watching that trip"


def test_F4_chat_returns_relogin_string_on_401(app_module):
    with patch.object(app_module.httpx, "post", return_value=_httpx_response(status=401)):
        result = app_module.chat("anything", history=[], request=_fake_request("tkn"))
    assert "re-login" in result.lower()


def test_F5_chat_returns_generic_failure_on_500(app_module):
    with patch.object(app_module.httpx, "post", return_value=_httpx_response(status=500)):
        result = app_module.chat("anything", history=[], request=_fake_request("tkn"))
    assert "failed to communicate" in result.lower()


def test_F6_chat_forwards_session_access_token_as_bearer(app_module):
    """Multi-tenancy invariant: the Bearer token sent to the agent comes
    from THIS request's session, not from any other state."""
    with patch.object(
        app_module.httpx, "post", return_value=_httpx_response(status=200, body={"text": "ok"})
    ) as mock_post:
        app_module.chat("hi", history=[], request=_fake_request("session-token-abc"))

    # httpx.post was called as positional URL + kwargs (headers, json, timeout)
    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer session-token-abc"
```

- [ ] **Step 2: Run F1–F6**

```bash
cd web
python -m pytest tests/test_app.py -v -k "F1 or F2 or F3 or F4 or F5 or F6"
```

Expected: 6 passed.

- [ ] **Step 3: Commit**

```bash
git add web/tests/test_app.py
git commit -m "web/app: characterize check_auth + chat() proxy"
```

### Task 5.2: Write F7–F8 (red) — session key from env, fail-fast

- [ ] **Step 1: Append F7–F8 to `test_app.py`**

```python
# ---------------------------------------------------------------------------
# F7/F8 — session-key bug fix
# ---------------------------------------------------------------------------

def test_F7_session_middleware_key_comes_from_env_not_literal_secret(monkeypatch):
    """SessionMiddleware.secret_key must be SESSION_SECRET_KEY, not 'secret'.
    Find the SessionMiddleware in the user_middleware list and read its
    starlette wrapper's secret_key."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "a-strong-random-key-for-tests")
    sys.modules.pop("app", None)
    import app as fresh_app

    from starlette.middleware.sessions import SessionMiddleware
    middlewares = [m for m in fresh_app.fastapi_app.user_middleware if m.cls is SessionMiddleware]
    assert len(middlewares) == 1
    # Starlette stores middleware kwargs in .kwargs; the key is 'secret_key'.
    assert middlewares[0].kwargs["secret_key"] == "a-strong-random-key-for-tests"


def test_F8_import_fails_fast_when_session_secret_missing(monkeypatch):
    monkeypatch.delenv("SESSION_SECRET_KEY", raising=False)
    sys.modules.pop("app", None)
    with pytest.raises(RuntimeError, match="SESSION_SECRET_KEY"):
        importlib.import_module("app")


def test_F8b_import_fails_fast_when_session_secret_is_placeholder(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "secret")
    sys.modules.pop("app", None)
    with pytest.raises(RuntimeError, match="placeholder"):
        importlib.import_module("app")


def test_F8c_import_fails_fast_when_session_secret_is_empty(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "")
    sys.modules.pop("app", None)
    with pytest.raises(RuntimeError, match="SESSION_SECRET_KEY"):
        importlib.import_module("app")
```

- [ ] **Step 2: Run F7–F8 — expect failure**

```bash
cd web
python -m pytest tests/test_app.py -v -k "F7 or F8"
```

Expected: 4 failed. F7 fails because `secret_key="secret"` is hard-coded. F8/F8b/F8c fail because the import succeeds today regardless of env state.

### Task 5.3: Fix the session-key bug (green)

- [ ] **Step 1: Edit `web/app.py`** — replace the `SessionMiddleware` line with the guarded read.

Find this block (around line 27–31):

```python
# Attach session middleware so the app can persist login state across requests.
# After a successful login, data like access_token and username are stored in a
# signed cookie on the user's browser. "Signed" means the cookie is
# cryptographically tied to secret_key, preventing the user from tampering with
# its contents. On every subsequent request, the middleware reads that cookie and
# makes its contents available via req.session (e.g. req.session["access_token"]).
fastapi_app.add_middleware(SessionMiddleware, secret_key="secret")
```

Replace the final line with:

```python
# Read the signing key from env and fail fast on missing, empty, or the
# placeholder value. A weak signing key lets anyone with the source forge
# session cookies and impersonate users.
_session_secret_key = os.environ.get("SESSION_SECRET_KEY", "").strip()
if not _session_secret_key:
    raise RuntimeError(
        "SESSION_SECRET_KEY env var is required and must be a strong random string"
    )
if _session_secret_key == "secret":
    raise RuntimeError(
        "SESSION_SECRET_KEY must not be the placeholder value 'secret'"
    )
fastapi_app.add_middleware(SessionMiddleware, secret_key=_session_secret_key)
```

- [ ] **Step 2: Run F7–F8 — expect pass**

```bash
cd web
python -m pytest tests/test_app.py -v -k "F7 or F8"
```

Expected: 4 passed.

- [ ] **Step 3: Run the full web suite to confirm no regression**

```bash
cd web
python -m pytest tests/ -v
```

Expected: all 13 web tests green (E1–E5 + F1–F6 + F7 + F8 + F8b + F8c).

- [ ] **Step 4: Commit**

```bash
git add web/app.py web/tests/test_app.py
git commit -m "web/app: SESSION_SECRET_KEY from env, fail fast on missing or placeholder"
```

---

## Final verification

### Task F.1: Run every suite the new CI workflow runs

- [ ] **Step 1: Node side**

```bash
cd lambdas/agent-authorizer && npm install && npm test && cd -
cd lambdas/mcp-authorizer && npm test && cd -
cd lambdas/flights-mcp && npm test && cd -
cd lambdas/hotels-mcp && npm test && cd -
npx jest test/
```

Expected: every command exits 0.

- [ ] **Step 2: Python side**

```bash
cd lambdas/poller && python -m pytest tests/ -q && cd -
cd lambdas/notifier && python -m pytest tests/ -q && cd -
cd lambdas/travel-agent && python -m pytest tests/ -q && cd -
cd evals && python -m pytest tests/ -q && cd -
cd web && python -m pytest tests/ -q && cd -
```

Expected: every command exits 0.

- [ ] **Step 3: Count assertions match the spec**

```bash
cd lambdas/agent-authorizer && npm test 2>&1 | grep -c '^ok '  # expect 10
cd lambdas/travel-agent && python -m pytest tests/test_app.py --collect-only -q | tail -1  # expect 11 tests
cd lambdas/travel-agent && python -m pytest tests/test_user.py --collect-only -q | tail -1  # expect 2 tests
cd lambdas/travel-agent && python -m pytest tests/test_tools.py --collect-only -q | tail -1  # expect 4 tests
cd web && python -m pytest tests/test_oauth.py --collect-only -q | tail -1  # expect 5 tests
cd web && python -m pytest tests/test_app.py --collect-only -q | tail -1  # expect 10 tests (F1..F6 + F7 + F8 + F8b + F8c)
```

Total: 10 + 11 + 2 + 4 + 5 + 10 = **42 assertions**. (Spec target was 40 — the extra two come from F8 being implemented as three discrete cases F8/F8b/F8c, which makes the fail-fast contract explicit for missing, placeholder, and empty values.)

### Task F.2: File the three deferred bugs

Spec calls for filing D5 (ip-api timeout), E6 (callback missing code), and F9 (chat timeout) as follow-up issues before the spec is marked complete.

- [ ] **Step 1: Create three GitHub issues (or equivalent tracker entries) titled:**
  - "travel-agent/tools.py: get_user_location lacks urlopen timeout"
  - "web/oauth.py: /callback crashes if `code` query param missing"
  - "web/app.py: chat() crashes on outbound httpx timeout"

Each issue body should link back to this plan and the design spec.

- [ ] **Step 2: Note the issue numbers in a follow-up comment on the closing PR or in this plan.**

---

## Self-review checklist

After implementation, the reviewer should verify:

- [ ] All 42 new test cases live in their per-package `tests/` directories and follow the existing import/reimport patterns.
- [ ] The two new CI steps (`agent-authorizer tests`, `web`) appear in `.github/workflows/ci.yml` and pass on the PR.
- [ ] `requirements-test.txt` has zero added or changed pins.
- [ ] `web/.env.example` documents `SESSION_SECRET_KEY`, `OAUTH_CALLBACK_URI`, `OAUTH_POST_LOGOUT_URL`.
- [ ] The three deferred bugs (D5, E6, F9) are filed as separate issues.
- [ ] `lambdas/agent-authorizer/index.js` exports the three test seam functions but nothing about them is referenced from production code paths (`_testKey` / `_testKeyError` are both `null` in real runs).
- [ ] `web/oauth.py` no longer constructs `oauth = OAuth()` inside `add_oauth_routes` — it lives at module scope.
- [ ] `web/app.py` reads `SESSION_SECRET_KEY` from env at import and raises `RuntimeError` on missing/empty/placeholder values.
- [ ] PR 1 + PR 2 JWT failure matrices test the same threat surfaces (HS256 attack, expired, wrong key) — reviewers can diff them side-by-side.
