"""
Test fixtures for the travel-agent Lambda.

The watches module reads its table names from env vars at import time and
constructs boto3 resources eagerly. Tests need:
  1. The env vars set BEFORE watches is imported.
  2. moto's mock_aws active BEFORE the boto3 resource is created.
  3. The actual DynamoDB tables created in the mock account.

The fixture below handles all three by creating tables under mock_aws and
re-importing watches inside the mock context. Tests then receive a fresh
watches module bound to the mocked tables.
"""

import importlib
import os
import sys

import boto3
import pytest
from moto import mock_aws

WATCHES_TABLE = "TestWatches"
FARE_HISTORY_TABLE = "TestFareHistory"


@pytest.fixture
def watches_module():
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["WATCHES_TABLE_NAME"] = WATCHES_TABLE
    os.environ["FARE_HISTORY_TABLE_NAME"] = FARE_HISTORY_TABLE

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=WATCHES_TABLE,
            KeySchema=[
                {"AttributeName": "userId", "KeyType": "HASH"},
                {"AttributeName": "watchId", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "userId", "AttributeType": "S"},
                {"AttributeName": "watchId", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=FARE_HISTORY_TABLE,
            KeySchema=[
                {"AttributeName": "watchId", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "watchId", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Force a fresh import so the module-level boto3 resource binds
        # to the mocked DynamoDB inside this fixture's context.
        sys.modules.pop("watches", None)
        watches = importlib.import_module("watches")
        yield watches
        sys.modules.pop("watches", None)


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
