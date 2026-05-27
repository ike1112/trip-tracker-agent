"""
Amazon SES wrapper for the trip-tracker alert notifier.

Owns:
  - The boto3 `ses` client (lazily constructed, cached after first use).
  - `SesSendError` raised on any `botocore.exceptions.ClientError`
    from `send_email`. Carries the AWS error code in its message so
    the handler can log it as a structured field. Non-ClientError
    exceptions propagate unwrapped; Lambda's async runtime owns the
    retry policy and unexpected errors should surface as such.

There is intentionally no runtime stub mode: if the poller decides a
notification should be sent, this module attempts a real SES send. Tests
mock `_get_client()` directly so they stay offline without creating a
second production behavior.

Plain-text only. We never construct an HTML body; the model's `reason`
string already passes the strict-JSON parser's HTML / control / bidi
rejection at `lambdas/poller/bedrock_decide.py`, and plain text sidesteps
the HTML-escape class of bugs altogether.
"""

from __future__ import annotations

DEFAULT_CHARSET = "UTF-8"

_client = None


def _get_client():
    """Lazy boto3 SES client. Tests patch this function, not boto3."""
    global _client
    if _client is None:
        import boto3
        _client = boto3.client("ses")
    return _client


class SesSendError(RuntimeError):
    """Raised on any `botocore.exceptions.ClientError` from
    `ses.send_email`. The message names the AWS error code so the
    caller can include it as a structured log field."""


def send(sender: str, recipient: str, subject: str, body: str) -> dict:
    """Send an email via SES.

    Returns `{"MessageId": ...}`. On `botocore.exceptions.ClientError`,
    raises `SesSendError` with the AWS error code in the message.
    """
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
        # Imported locally so normal cold starts do not pay for botocore's
        # exception module unless SES actually raises.
        from botocore.exceptions import ClientError
        if isinstance(e, ClientError):
            code = e.response.get("Error", {}).get("Code", "Unknown")
            raise SesSendError(
                f"SES send_email failed with AWS error code: {code}"
            ) from e
        raise
    return {"MessageId": response["MessageId"]}
