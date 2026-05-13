"""
Idempotent writeback to `Watches.lastAlertedAt` + `lastAlertedPrice`.

Owns the single DDB `UpdateItem` that closes the dedup loop:

  - Sets `lastAlertedAt` to a freshly-stamped UTC ISO timestamp.
  - Sets `lastAlertedPrice` to the just-alerted snapshot's total
    (coerced through `Decimal(str(...))` to dodge float imprecision).
  - **Conditional update**: only writes if `lastAlertedAt` is
    absent OR strictly older than the new value. Protects against
    out-of-order Lambda async retries that could otherwise backdate
    the dedup state. A pre-existing `lastAlertedAt` at-or-after the
    new value raises `WritebackConflictError` so the handler can log
    the conflict for debugging without aborting the response.
  - **Field surgicality**: only the two alert-state fields change.
    Every other Watches column (status, maxTotalPrice, preferences,
    dateWindow, …) is untouched — verified by the round-trip test.

The boto3 resource is created lazily at first use so test fixtures
(moto, dropped tables, env overrides) can run their setup before any
client construction.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

_resource = None
_table = None


def _now() -> datetime:
    """Test seam — patch this to freeze the clock in unit tests.

    Returns timezone-aware UTC so `.isoformat()` always ends with
    `+00:00`.
    """
    return datetime.now(timezone.utc)


def _get_table():
    """Lazy DDB resource. The first call binds against whatever env /
    moto state is active at that moment, so tests can rebind by popping
    this module from `sys.modules` between cases."""
    global _resource, _table
    if _table is None:
        import boto3
        _resource = boto3.resource("dynamodb")
        table_name = os.environ.get("WATCHES_TABLE_NAME")
        if not table_name:
            raise EnvironmentError(
                "WATCHES_TABLE_NAME env var is required"
            )
        _table = _resource.Table(table_name)
    return _table


class WritebackConflictError(RuntimeError):
    """Raised when the conditional update's
    `attribute_not_exists(lastAlertedAt) OR lastAlertedAt < :now`
    check fails — i.e., the Watches row already has a `lastAlertedAt`
    at or after the proposed new value. The alert email has already
    been sent at this point; the handler swallows this and returns
    200 so the SES delivery isn't lost."""


def _to_decimal(value: Any) -> Decimal:
    """Float-safe Decimal coercion. Never `Decimal(float)` — always
    `Decimal(str(...))`. Matches `lambdas/poller/snapshot.py:69-73`."""
    if value is None:
        raise ValueError("lastAlertedPrice cannot be None")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def write_alert_state(watch: dict, snapshot_total_price: Any) -> None:
    """Idempotently update `(lastAlertedAt, lastAlertedPrice)` on the
    Watches row keyed by `(userId, watchId)`.

    Raises:
        WritebackConflictError: if a more-recent (or equal) alert
            timestamp already lives on the row. The caller is expected
            to log + continue — the alert email has already been sent.
        botocore.exceptions.ClientError: on any other DDB failure.
            The caller is expected to let this propagate; Lambda's
            async retry takes over.
    """
    table = _get_table()
    now_iso = _now().isoformat()
    price = _to_decimal(snapshot_total_price)
    try:
        table.update_item(
            Key={"userId": watch["userId"], "watchId": watch["watchId"]},
            UpdateExpression=(
                "SET lastAlertedAt = :now, lastAlertedPrice = :price"
            ),
            ConditionExpression=(
                "attribute_not_exists(lastAlertedAt) OR lastAlertedAt < :now"
            ),
            ExpressionAttributeValues={
                ":now": now_iso,
                ":price": price,
            },
        )
    except Exception as e:
        from botocore.exceptions import ClientError
        if isinstance(e, ClientError):
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise WritebackConflictError(
                    f"existing lastAlertedAt is not strictly older than {now_iso}"
                ) from e
        raise
