"""
Lambda entrypoint for the trip-tracker alert notifier.

Invoked asynchronously by the poller (`lambdas/poller/app.py`) when
`decision.alert` is True. Receives a payload shaped
`{snapshot, watch, decision}` — the poller already has the data, so we
don't re-query DDB on the read path.

Flow:
  1. Validate the event shape (`snapshot`, `watch`, `decision` all
     present and dict-typed). Missing key -> structured-log + raise
     `KeyError`. Lambda async runtime then handles retry / DLQ.
  2. If `decision.alert` is not True, log a WARNING (the poller
     should not have invoked us) and return early with a `skipped`
     payload. No SES call, no DDB write.
  3. `email_template.render(...)` -> `(subject, body)`. Pure function;
     no side effects.
  4. `ses_client.send(...)` -> `{"MessageId": ...}`. On `SesSendError`
     we re-raise — Lambda async retry takes over.
  5. `writer.write_alert_state(watch, snapshot.totalPrice)` -> writes
     the dedup state. On `WritebackConflictError` we LOG and continue
     — the alert email has already been delivered; the conflict means
     another poll already wrote a newer timestamp.
  6. Return `{"statusCode": 200, "messageId": ..., "watchId": ...}`.

Log conventions (mirrors the poller's powertools-Logger structured-log
posture): every event has a stable event-name string in `msg` plus
typed fields in `extra`. We log `user_id_prefix` (first 8 chars) only
— never the full Cognito sub. We never log the recipient email or the
model's `reason` string — both could reach PII or a future-untrusted
content channel.
"""

from __future__ import annotations

import os
from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

from email_template import render
from ses_client import SesSendError, send
from writer import WritebackConflictError, write_alert_state


logger = Logger(service="trip-tracker-notifier")

_USER_ID_PREFIX_CHARS = 8


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise EnvironmentError(f"{name} env var is required")
    return value


def _user_id_prefix(user_id: str) -> str:
    """First N chars of the Cognito sub — enough for log correlation
    across the poller / notifier pair, not enough to identify the user
    from logs alone."""
    return (user_id or "")[:_USER_ID_PREFIX_CHARS]


def _validate_event(event: dict) -> tuple[dict, dict, dict]:
    """Pull the three expected sub-payloads out of the event. Raises
    KeyError naming the offending key on absence; TypeError on
    wrong-shaped sub-payloads."""
    for key in ("snapshot", "watch", "decision"):
        if key not in event:
            logger.error(
                "notifier_event_missing_key",
                extra={"missing_key": key},
            )
            raise KeyError(key)
        if not isinstance(event[key], dict):
            logger.error(
                "notifier_event_wrong_shape",
                extra={"key": key, "actual_type": type(event[key]).__name__},
            )
            raise TypeError(
                f"event[{key!r}] must be a dict, got {type(event[key]).__name__}"
            )
    return event["snapshot"], event["watch"], event["decision"]


def handler(event: dict, context: LambdaContext) -> dict:
    """Per-invocation entrypoint. Returns a JSON-serialisable status
    dict for CloudWatch / manual invocation; the real "did it happen"
    signal is the structured logs and the lastAlertedAt writeback on
    the Watches row."""
    sender = _require_env("NOTIFIER_SENDER_EMAIL")
    recipient = _require_env("NOTIFIER_RECIPIENT_EMAIL")

    snapshot, watch, decision = _validate_event(event)
    watch_id = watch.get("watchId", "")
    log_extra = {
        "watch_id": watch_id,
        "user_id_prefix": _user_id_prefix(watch.get("userId", "")),
    }

    if decision.get("alert") is not True:
        logger.warning(
            "notifier_invoked_without_alert",
            extra={**log_extra, "decision_alert": decision.get("alert")},
        )
        return {
            "statusCode": 200,
            "status": "skipped",
            "watchId": watch_id,
        }

    subject, body = render(snapshot, watch, decision)

    try:
        send_response = send(sender, recipient, subject, body)
    except SesSendError as e:
        # Log only the exception class name. A future error class
        # that includes the recipient in its message (e.g.
        # MessageRejected) would otherwise leak it via this path.
        logger.error(
            "ses_send_failed",
            extra={**log_extra, "error": type(e).__name__},
        )
        raise

    message_id = send_response["MessageId"]

    try:
        write_alert_state(watch, snapshot.get("totalPrice"))
    except WritebackConflictError:
        logger.warning(
            "writeback_conflict",
            extra={**log_extra, "message_id": message_id},
        )
    except ValueError:
        # The writer rejects None / non-numeric prices with
        # ValueError. SES has already delivered the email at this
        # point; raising would trigger an async retry and a duplicate
        # send. Log and continue — the dedup band catches the
        # next-poll duplicate.
        logger.warning(
            "writeback_value_error",
            extra={**log_extra, "message_id": message_id},
        )

    logger.info(
        "notification_sent",
        extra={**log_extra, "message_id": message_id},
    )
    return {
        "statusCode": 200,
        "status": "sent",
        "watchId": watch_id,
        "messageId": message_id,
    }
