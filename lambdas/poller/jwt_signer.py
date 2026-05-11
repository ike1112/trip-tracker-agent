"""
HS256 JWT signing for the trip-tracker poller.

The MCP authorizer (`lambdas/mcp-authorizer/index.js`) requires a token whose
verified claims satisfy:
    - `sub == "travel-agent"`             (only authorised caller)
    - `user_id` present                   (used for the principal + per-call logging)
    - signed with `JWT_SIGNATURE_SECRET`  (HS256)

The poller is acting on behalf of a specific user (the owner of the watch
being polled), so each per-watch MCP call carries that user's `userId` in
the `user_id` claim. This keeps the MCP server's existing user-aware logging
working in the cron path the same way it does in the chat path — and gives
us a clean audit trail in CloudWatch Logs.

Tokens are short-lived (5 minutes) so a leaked token has limited blast
radius. The poller never persists or returns tokens; they live only in
the per-watch loop and are not logged.
"""

import os
import time
from typing import Any

import jwt

JWT_SIGNATURE_SECRET = os.environ.get("JWT_SIGNATURE_SECRET")
SUBJECT = "travel-agent"
TTL_SECONDS = 5 * 60


def sign_for_user(user_id: str, *, ttl_seconds: int = TTL_SECONDS, now: float | None = None) -> str:
    """
    Return an HS256 JWT the MCP authorizer accepts for `user_id`.

    Args:
        user_id: Cognito `sub` of the watch owner this poll is acting for.
        ttl_seconds: token lifetime; default 5 minutes (long enough for a
            poller invocation, short enough that a leak doesn't matter).
        now: optional injection point for tests.

    Raises:
        EnvironmentError: if `JWT_SIGNATURE_SECRET` is not set. Failing
            loud beats forging a token with the literal string "None".
    """
    if not JWT_SIGNATURE_SECRET:
        raise EnvironmentError("JWT_SIGNATURE_SECRET env var is not set")
    if not user_id:
        raise ValueError("user_id is required")

    issued = int(now if now is not None else time.time())
    payload: dict[str, Any] = {
        "sub": SUBJECT,
        "user_id": user_id,
        # The mcp-authorizer reads `user_name` only for the principalId
        # display string; it doesn't enforce the value. Reuse user_id so
        # the principalId is informative without exposing extra data.
        "user_name": user_id,
        "iat": issued,
        "exp": issued + ttl_seconds,
    }
    return jwt.encode(payload, JWT_SIGNATURE_SECRET, algorithm="HS256")
