"""Tests for `jwt_signer.sign_for_user`.

Pin down the contract that matters operationally:
  - The token verifies under HS256 with the same secret.
  - Claims match what the mcp-authorizer (`lambdas/mcp-authorizer/index.js`)
    enforces: `sub == "travel-agent"`, `user_id` present, `exp` in the future.
  - A token signed with one secret does NOT verify under a different secret.
  - Expired tokens are rejected by `jwt.decode` (so a leak past the TTL
    is harmless).
  - Missing env var raises `EnvironmentError` rather than silently signing
    with `None` as the secret (which pyjwt would happily do).
  - Empty `user_id` raises `ValueError` rather than producing a token whose
    `user_id` claim is the empty string.
"""

import importlib
import os
import sys
import time

import jwt
import pytest


def _fresh_signer(secret: str | None):
    if secret is None:
        os.environ.pop("JWT_SIGNATURE_SECRET", None)
    else:
        os.environ["JWT_SIGNATURE_SECRET"] = secret
    sys.modules.pop("jwt_signer", None)
    return importlib.import_module("jwt_signer")


def test_token_verifies_with_same_secret_and_carries_authorizer_claims():
    secret = "test-secret-32chars-min-aaaaaaaaaaa"
    signer = _fresh_signer(secret)

    token = signer.sign_for_user("user-12345678")

    decoded = jwt.decode(token, secret, algorithms=["HS256"])
    # Mirror what `mcp-authorizer/index.js` actually checks.
    assert decoded["sub"] == "travel-agent"
    assert decoded["user_id"] == "user-12345678"
    assert decoded["user_name"] == "user-12345678"
    assert decoded["exp"] > decoded["iat"]
    assert decoded["exp"] - decoded["iat"] == 5 * 60  # default TTL


def test_token_signed_with_one_secret_fails_verification_under_another():
    signer = _fresh_signer("secret-alpha-aaaaaaaaaaaaaaaaaa")
    token = signer.sign_for_user("u1")

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "secret-beta-bbbbbbbbbbbbbbbbbbbb", algorithms=["HS256"])


def test_expired_token_is_rejected():
    secret = "test-secret-32chars-min-aaaaaaaaaaa"
    signer = _fresh_signer(secret)

    # Sign at a time 10 minutes ago with default 5-min TTL → expired.
    long_ago = time.time() - 600
    token = signer.sign_for_user("u1", now=long_ago)

    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(token, secret, algorithms=["HS256"])


def test_missing_secret_env_var_raises_clear_error():
    signer = _fresh_signer(None)

    with pytest.raises(EnvironmentError, match="JWT_SIGNATURE_SECRET"):
        signer.sign_for_user("u1")


def test_empty_user_id_rejected():
    signer = _fresh_signer("test-secret-aaaaaaaaaaaaaaaaaaaaa")

    with pytest.raises(ValueError, match="user_id"):
        signer.sign_for_user("")


def test_custom_ttl_honored():
    secret = "test-secret-aaaaaaaaaaaaaaaaaaaaa"
    signer = _fresh_signer(secret)

    token = signer.sign_for_user("u1", ttl_seconds=30)
    decoded = jwt.decode(token, secret, algorithms=["HS256"])

    assert decoded["exp"] - decoded["iat"] == 30


def test_alg_none_token_is_rejected_under_hs256_only_decode():
    """Forge a token with `alg=none` and assert the production verify
    path (which the authorizer mirrors: `algorithms=["HS256"]`) rejects
    it. PyJWT enforces this by default — this test pins the invariant
    so a future change can't widen the accepted-algs set silently.
    """
    # Build the unsigned token by hand so we don't have to wrestle
    # with PyJWT's encoder (which refuses to *produce* alg=none unless
    # asked very explicitly).
    import base64
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        b'{"sub":"travel-agent","user_id":"victim","exp":9999999999,"iat":0}'
    ).rstrip(b"=").decode()
    forged = f"{header}.{payload}."  # empty signature

    with pytest.raises(jwt.InvalidAlgorithmError):
        jwt.decode(forged, "any-secret", algorithms=["HS256"])
