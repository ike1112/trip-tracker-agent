"""
HS256 JWT signing for the trip-tracker poller.

Every verifier (the shared MCP authorizer + both MCP server handlers)
requires a token whose verified claims satisfy (ADR 0006):
    - `sub == "trip-tracker-poller"`      (the poller's own component sub)
    - `user_id` present                   (used for the principal + per-call logging)
    - signed with the poller's OWN HS256 secret, fetched from AWS Secrets
      Manager via `POLLER_JWT_SECRET_ARN`

The poller is acting on behalf of a specific user (the owner of the watch
being polled), so each per-watch MCP call carries that user's `userId` in
the `user_id` claim. This keeps the MCP server's existing user-aware logging
working in the cron path the same way it does in the chat path — and gives
us a clean audit trail in CloudWatch Logs.

Tokens are short-lived (5 minutes) so a leaked token has limited blast
radius. The poller never persists or returns tokens; they live only in
the per-watch loop and are not logged.

The signing secret is fetched LAZILY on the first `sign_for_user` call
(not at import) and cached for the warm-Lambda lifetime. Import-time
fetch would fire before a test could replace the Secrets Manager client;
lazy-first-use lets tests set `_secrets` + reset `_cached_secret` before
the first sign (ADR 0006).
"""

import os
import time
from typing import Any

import boto3
import jwt

SUBJECT = "trip-tracker-poller"
TTL_SECONDS = 5 * 60

# Created on first real fetch (not at import) so unit tests that never
# touch AWS don't need a Region/credentials, and can inject a fake.
_secrets = None
_cached_secret: str | None = None


def _get_secret() -> str:
    """
    Return the poller's HS256 signing secret, fetched once and cached.

    Raises:
        EnvironmentError: if `POLLER_JWT_SECRET_ARN` is not set. Failing
            loud beats forging a token with the literal string "None".
    """
    global _secrets, _cached_secret
    if _cached_secret is None:
        arn = os.environ.get("POLLER_JWT_SECRET_ARN")
        if not arn:
            raise EnvironmentError("POLLER_JWT_SECRET_ARN env var is not set")
        if _secrets is None:
            _secrets = boto3.client("secretsmanager")
        _cached_secret = _secrets.get_secret_value(SecretId=arn)["SecretString"]
    return _cached_secret


def sign_for_user(user_id: str, *, ttl_seconds: int = TTL_SECONDS, now: float | None = None) -> str:
    """
    Return an HS256 JWT every verifier accepts for `user_id`.

    Args:
        user_id: Cognito `sub` of the watch owner this poll is acting for.
        ttl_seconds: token lifetime; default 5 minutes (long enough for a
            poller invocation, short enough that a leak doesn't matter).
        now: optional injection point for tests.

    Raises:
        EnvironmentError: if `POLLER_JWT_SECRET_ARN` is not set.
        ValueError: if `user_id` is empty.
    """
    secret = _get_secret()
    if not user_id:
        raise ValueError("user_id is required")

    issued = int(now if now is not None else time.time())
    payload: dict[str, Any] = {
        "sub": SUBJECT,
        "user_id": user_id,
        # The verifier reads `user_name` only for the principalId
        # display string; it doesn't enforce the value. Reuse user_id so
        # the principalId is informative without exposing extra data.
        "user_name": user_id,
        "iat": issued,
        "exp": issued + ttl_seconds,
    }
    return jwt.encode(payload, secret, algorithm="HS256")
