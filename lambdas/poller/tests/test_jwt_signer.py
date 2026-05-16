"""Tests for `jwt_signer.sign_for_user` (ADR 0006 per-component signer).

Pin down the contract that matters operationally:
  - The token verifies under HS256 with the same secret.
  - Claims match what every verifier enforces: `sub == "trip-tracker-poller"`,
    `user_id` present, `exp` in the future.
  - A token signed with one secret does NOT verify under a different secret.
  - Expired tokens are rejected by `jwt.decode` (so a leak past the TTL
    is harmless).
  - The secret is fetched LAZILY on first sign, not at import.
  - Missing `POLLER_JWT_SECRET_ARN` raises `EnvironmentError` rather than
    silently signing with `None`.
  - A failing Secrets Manager fetch surfaces (no silent fallback).
  - Empty `user_id` raises `ValueError`.
"""

import importlib
import os
import sys
import time

import jwt
import pytest


class _FakeSecrets:
    """Stand-in for the boto3 secretsmanager client. Records calls so a
    test can assert the fetch is lazy."""

    def __init__(self, value, *, fail=False):
        self._value = value
        self._fail = fail
        self.calls = 0

    def get_secret_value(self, SecretId=None):
        self.calls += 1
        if self._fail:
            raise RuntimeError("secrets manager unavailable")
        return {"SecretString": self._value}


def _signer_with_secret(secret: str, *, fail: bool = False):
    """Import jwt_signer, point it at a fake Secrets Manager client, and
    reset the cache so the next sign performs a (fake) lazy fetch."""
    sys.modules.pop("jwt_signer", None)
    signer = importlib.import_module("jwt_signer")
    os.environ["POLLER_JWT_SECRET_ARN"] = "arn:aws:secretsmanager:us-east-1:000000000000:secret:poller-test"
    signer._secrets = _FakeSecrets(secret, fail=fail)
    signer._cached_secret = None
    return signer


def _signer_without_arn():
    sys.modules.pop("jwt_signer", None)
    signer = importlib.import_module("jwt_signer")
    os.environ.pop("POLLER_JWT_SECRET_ARN", None)
    signer._secrets = _FakeSecrets("unused")
    signer._cached_secret = None
    return signer


def test_E1_token_verifies_and_carries_trip_tracker_poller_sub():
    secret = "test-secret-32chars-min-aaaaaaaaaaa"
    signer = _signer_with_secret(secret)

    token = signer.sign_for_user("user-12345678")

    decoded = jwt.decode(token, secret, algorithms=["HS256"])
    # The poller's component sub is trip-tracker-poller, NOT travel-agent.
    assert decoded["sub"] == "trip-tracker-poller"
    assert decoded["sub"] != "travel-agent"
    assert decoded["user_id"] == "user-12345678"
    assert decoded["user_name"] == "user-12345678"
    assert decoded["exp"] > decoded["iat"]
    assert decoded["exp"] - decoded["iat"] == 5 * 60  # default TTL


def test_E2_secret_fetched_lazily_on_first_sign_not_at_import():
    signer = _signer_with_secret("lazy-secret-aaaaaaaaaaaaaaaaaaaaa")
    # Importing + resetting must NOT have fetched yet.
    assert signer._secrets.calls == 0
    signer.sign_for_user("u1")
    assert signer._secrets.calls == 1
    # Cached: a second sign does not refetch.
    signer.sign_for_user("u2")
    assert signer._secrets.calls == 1


def test_token_signed_with_one_secret_fails_verification_under_another():
    signer = _signer_with_secret("secret-alpha-aaaaaaaaaaaaaaaaaa")
    token = signer.sign_for_user("u1")

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "secret-beta-bbbbbbbbbbbbbbbbbbbb", algorithms=["HS256"])


def test_expired_token_is_rejected():
    secret = "test-secret-32chars-min-aaaaaaaaaaa"
    signer = _signer_with_secret(secret)

    long_ago = time.time() - 600
    token = signer.sign_for_user("u1", now=long_ago)

    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(token, secret, algorithms=["HS256"])


def test_E3_missing_arn_env_var_raises_clear_error():
    signer = _signer_without_arn()

    with pytest.raises(EnvironmentError, match="POLLER_JWT_SECRET_ARN"):
        signer.sign_for_user("u1")


def test_E4_secrets_manager_failure_surfaces():
    signer = _signer_with_secret("unused", fail=True)

    with pytest.raises(RuntimeError, match="secrets manager unavailable"):
        signer.sign_for_user("u1")


def test_empty_user_id_rejected():
    signer = _signer_with_secret("test-secret-aaaaaaaaaaaaaaaaaaaaa")

    with pytest.raises(ValueError, match="user_id"):
        signer.sign_for_user("")


def test_custom_ttl_honored():
    secret = "test-secret-aaaaaaaaaaaaaaaaaaaaa"
    signer = _signer_with_secret(secret)

    token = signer.sign_for_user("u1", ttl_seconds=30)
    decoded = jwt.decode(token, secret, algorithms=["HS256"])

    assert decoded["exp"] - decoded["iat"] == 30


def test_alg_none_token_is_rejected_under_hs256_only_decode():
    """Forge a token with `alg=none` and assert the production verify
    path (which the verifier mirrors: `algorithms=["HS256"]`) rejects
    it. PyJWT enforces this by default — this test pins the invariant
    so a future change can't widen the accepted-algs set silently.
    """
    import base64
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        b'{"sub":"trip-tracker-poller","user_id":"victim","exp":9999999999,"iat":0}'
    ).rstrip(b"=").decode()
    forged = f"{header}.{payload}."  # empty signature

    with pytest.raises(jwt.InvalidAlgorithmError):
        jwt.decode(forged, "any-secret", algorithms=["HS256"])
