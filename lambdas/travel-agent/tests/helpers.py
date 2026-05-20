"""
Plain-function test helpers for the travel-agent suite.

These live outside conftest.py because they are not pytest fixtures —
fixtures get auto-injected, but builders need explicit `import`. Splitting
them this way keeps conftest.py for fixture wiring and avoids forcing
sys.path tricks to import non-fixture names in package-mode pytest.
"""

import json
from datetime import datetime, timedelta, timezone

import jwt as pyjwt


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


def make_event(token, body=None, source_ip="70.200.50.45"):
    """Build an API Gateway event in the shape app.handler reads."""
    if body is None:
        body = {"text": "Book me a trip to Tokyo"}
    return {
        "headers": {"Authorization": f"Bearer {token}"},
        "requestContext": {"identity": {"sourceIp": source_ip}},
        "body": json.dumps(body) if isinstance(body, dict) else body,
    }
