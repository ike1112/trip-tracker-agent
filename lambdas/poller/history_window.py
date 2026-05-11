"""
Time-windowed FareHistory query for anomaly detection.

The poller's `is_anomaly` gate (gates.py) needs the last 30 days of price
snapshots for each watch. A `Query` with `KeyCondition: watchId = :w AND
timestamp > :since` is exactly the shape FareHistory's PK supports
(partition `watchId` + sort `timestamp`).

The boundary is exclusive (`>` not `>=`). A row written at exactly the
window edge is treated as "older than the window" — including it would
suppress a valid new-low anomaly (the boundary row would tie the new
low and short-circuit the strict `<` test).

Returns rows newest-first so callers reading "the latest N" see them in
the obvious order without an extra sort.
"""

from __future__ import annotations

import os

import boto3
from boto3.dynamodb.conditions import Key

FARE_HISTORY_TABLE_NAME = os.environ.get("FARE_HISTORY_TABLE_NAME")

_dynamodb = boto3.resource("dynamodb")
_fare_history_table = (
    _dynamodb.Table(FARE_HISTORY_TABLE_NAME) if FARE_HISTORY_TABLE_NAME else None
)


def get_window(watch_id: str, since_iso: str) -> list[dict]:
    """All FareHistory rows for `watch_id` strictly newer than `since_iso`.

    Args:
        watch_id: partition key.
        since_iso: ISO 8601 UTC timestamp; rows with `timestamp > since_iso`
            are returned. The composer always writes UTC ISO timestamps
            (snapshot.py) so lexicographic comparison is chronological.

    Returns:
        Rows newest-first (`ScanIndexForward=False`). Paginated: DDB caps
        each Query response at 1 MB. A watch active for the full 90-day
        TTL at a 4-hour cadence accumulates ~540 rows, well over what
        fits in one page once `bestOfferBlob` denormalisation is counted.
        We follow `LastEvaluatedKey` so the anomaly gate sees the full
        window, not a silently truncated prefix.
    """
    if _fare_history_table is None:
        raise EnvironmentError("FARE_HISTORY_TABLE_NAME env var is not set")
    items: list[dict] = []
    kwargs = {
        "KeyConditionExpression": Key("watchId").eq(watch_id) & Key("timestamp").gt(since_iso),
        "ScanIndexForward": False,
    }
    while True:
        resp = _fare_history_table.query(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return items
        kwargs["ExclusiveStartKey"] = last_key
