"""
Tests for the travel-agent Lambda handler.

The handler authenticates the caller, builds a composite prompt, and calls
agent.prompt. These tests assert the contract at the Lambda boundary:
401 on auth failure, 400 on malformed input, 200 on success. The actual
agent invocation is mocked (see conftest.app_module).
"""

import json

from .helpers import make_token, make_event


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


# ---------------------------------------------------------------------------
# Handler robustness — drive the 400-on-bad-input fix
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
