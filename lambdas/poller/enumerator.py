"""
Watch enumeration for the trip-tracker poller.

The Watches table is partitioned by `userId`, so polling all users requires a
`Scan` with a `FilterExpression` on `status = "active"`. This is acceptable
at personal scale (≤dozens of items, see `lib/data-stores.js` and
production-readiness companion §2 row 1). When watch counts grow, ADR 0007
(planned, slice 9) covers adding a status GSI so this becomes a `Query`.

The function below pages through Scan results — DynamoDB caps each page at
1MB regardless of `FilterExpression`, so any deployment with more than a
few hundred watches will see `LastEvaluatedKey` set and require pagination.
We do this transparently so callers see a single iterable.
"""

import os
from typing import Iterator

import boto3
from boto3.dynamodb.conditions import Attr

WATCHES_TABLE_NAME = os.environ.get("WATCHES_TABLE_NAME")

_dynamodb = boto3.resource("dynamodb")
_watches_table = _dynamodb.Table(WATCHES_TABLE_NAME) if WATCHES_TABLE_NAME else None


def iter_active_watches() -> Iterator[dict]:
    """
    Yield every Watches row with `status == "active"`, across all users.

    Implementation notes:
    - Scan + FilterExpression. Filter runs server-side AFTER the 1MB page cap,
      so a page can be empty (all items filtered out) without meaning we're
      done. Always loop on `LastEvaluatedKey`.
    - No projection — the poller needs the full row (preferences, dateWindow,
      lastAlerted*, etc.) to compose the MCP request and run the gates.
    """
    if _watches_table is None:
        # Fail loud: a misconfigured deploy that drops `WATCHES_TABLE_NAME`
        # would otherwise crash with a confusing AttributeError on `.scan()`.
        raise EnvironmentError("WATCHES_TABLE_NAME env var is not set")
    kwargs = {"FilterExpression": Attr("status").eq("active")}
    while True:
        resp = _watches_table.scan(**kwargs)
        for item in resp.get("Items", []):
            yield item
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return
        kwargs["ExclusiveStartKey"] = last_key
