"""
Amazon SES wrapper for the trip-tracker alert notifier.

Owns:
  - The boto3 `ses` client (lazily constructed, cached after first use).
  - Stub vs live mode selection at module load time via `SES_MODE`.
  - `SesSendError` — raised on any `botocore.exceptions.ClientError`
    from `send_email`. Carries the AWS error code in its message so
    the handler can log it as a structured field. Non-ClientError
    exceptions propagate unwrapped — Lambda's async runtime owns the
    retry policy and unexpected errors should surface as such.

Modes:
  - `live` (production default): real `ses.send_email` call.
  - `stub`: deterministic local response shaped like a real SES
    return: `{"MessageId": "stub-<sha8>"}`. The sha8 derives from
    `(sender, recipient, subject, body)` so two identical inputs
    produce the same MessageId, but any change in any field flips it.
    Used by every notifier test in this codebase so no test ever
    burns a real SES quota.

Plain-text only. We never construct an HTML body — the model's
`reason` string already passes the strict-JSON parser's HTML / control
/ bidi rejection at `lambdas/poller/bedrock_decide.py`, and plain text
sidesteps the HTML-escape class of bugs altogether.
"""

from __future__ import annotations

import hashlib
import os

DEFAULT_CHARSET = "UTF-8"

_MODE_LIVE = "live"
_MODE_STUB = "stub"
_VALID_MODES = (_MODE_LIVE, _MODE_STUB)


def _resolve_mode() -> str:
    raw = os.environ.get("SES_MODE", "").strip()
    if not raw:
        return _MODE_LIVE
    if raw not in _VALID_MODES:
        raise ImportError(
            f"unsupported SES_MODE: {raw!r} — expected one of {_VALID_MODES}"
        )
    return raw


SES_MODE = _resolve_mode()

_client = None


def _get_client():
    """Lazy boto3 client. Created only when first needed in live mode so
    stub-mode tests never construct a real client."""
    global _client
    if _client is None:
        import boto3
        _client = boto3.client("ses")
    return _client


class SesSendError(RuntimeError):
    """Raised on any `botocore.exceptions.ClientError` from
    `ses.send_email`. The message names the AWS error code so the
    caller can include it as a structured log field."""


def _stub_message_id(sender: str, recipient: str, subject: str, body: str) -> str:
    """Deterministic stub message id. Same inputs always produce the
    same id; any change flips it. Tests rely on the determinism."""
    blob = f"{sender}|{recipient}|{subject}|{body}".encode("utf-8")
    return "stub-" + hashlib.sha256(blob).hexdigest()[:8]


def send(sender: str, recipient: str, subject: str, body: str) -> dict:
    """Send an email via SES (or a stub thereof).

    Returns the AWS response dict (live mode) or a stub-shaped
    `{"MessageId": "stub-<sha8>"}` dict (stub mode). On
    `botocore.exceptions.ClientError`, raises `SesSendError` with the
    AWS error code in the message.
    """
    if SES_MODE == _MODE_STUB:
        return {"MessageId": _stub_message_id(sender, recipient, subject, body)}

    try:
        response = _get_client().send_email(
            Source=sender,
            Destination={"ToAddresses": [recipient]},
            Message={
                "Subject": {"Data": subject, "Charset": DEFAULT_CHARSET},
                "Body": {"Text": {"Data": body, "Charset": DEFAULT_CHARSET}},
            },
        )
    except Exception as e:
        # Imported locally so the stub-mode test that asserts boto3 is
        # never imported can still pass — this branch only executes in
        # live mode after `_get_client()` has run.
        from botocore.exceptions import ClientError
        if isinstance(e, ClientError):
            code = e.response.get("Error", {}).get("Code", "Unknown")
            raise SesSendError(
                f"SES send_email failed with AWS error code: {code}"
            ) from e
        raise
    return {"MessageId": response["MessageId"]}
