"""
Watch enumeration for the trip-tracker poller.

The Watches table is partitioned by `userId` (ideal for per-user chat
CRUD), but the poller needs every active watch across all users on each
tick. That is a `Query` on the `status-index` GSI keyed on `status`
(ADR 0007), not a table `Scan` — read cost is proportional to the number
of *active* watches, not the total row count.

The function below pages through Query results — DynamoDB caps each page
at 1MB, so any deployment with more than a few hundred active watches
will see `LastEvaluatedKey` set and require pagination. We do this
transparently so callers see a single iterable.

Yield order is unspecified (a GSI Query does not guarantee item order
and `lib/poller/app.py` processes each watch independently, so order is
not part of this contract).
"""

import os
from typing import Iterator

import boto3
from boto3.dynamodb.conditions import Key

WATCHES_TABLE_NAME = os.environ.get("WATCHES_TABLE_NAME")

_dynamodb = boto3.resource("dynamodb")
_watches_table = _dynamodb.Table(WATCHES_TABLE_NAME) if WATCHES_TABLE_NAME else None


def iter_active_watches() -> Iterator[dict]:
    """
    Yield every Watches row with `status == "active"`, across all users.

    Implementation notes:
    - Query on the `status-index` GSI (PK `status`). The GSI projects ALL
      attributes, so each row is complete — the poller needs the full row
      (preferences, dateWindow, lastAlerted*, etc.) to compose the MCP
      request and run the gates, with no second base-table fetch.
    - 1MB page cap applies to Query exactly as to Scan; always loop on
      `LastEvaluatedKey`.
    - No `ConsistentRead`: strongly-consistent reads are unsupported on a
      GSI. A just-created "active" watch may miss the immediately
      following tick (GSI is eventually consistent); the next scheduled
      tick picks it up.
    """
    if _watches_table is None:
        # Fail loud: a misconfigured deploy that drops `WATCHES_TABLE_NAME`
        # would otherwise crash with a confusing AttributeError on `.query()`.
        raise EnvironmentError("WATCHES_TABLE_NAME env var is not set")
    kwargs = {
        "IndexName": "status-index",
        "KeyConditionExpression": Key("status").eq("active"),
    }
    while True:
        resp = _watches_table.query(**kwargs)
        for item in resp.get("Items", []):
            yield item
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return
        kwargs["ExclusiveStartKey"] = last_key
