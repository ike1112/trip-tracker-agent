"""
Test fixtures for the travel-agent Lambda.

Two fixture families live here:

1. `watches_module` — boots a moto-mocked DynamoDB and reimports
   watches.py inside the mock context. The module reads its table
   names from env at import time, so the env must be set BEFORE
   import and the boto3 resources must bind to moto's account.

2. `rsa_keypair` / `other_rsa_keypair` / `app_module` — back the
   handler tests. They generate RSA keypairs at session scope, stub
   the `agent` module into sys.modules before importing app.py
   (so the heavy Strands stack never loads), and patch the JWKS
   client to return the test public key.

Plain-function helpers used by tests (`make_token`, `make_event`)
live in `helpers.py`, not here — only pytest fixtures belong here.
"""

import importlib
import os
import sys
from unittest.mock import MagicMock

import boto3
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
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


