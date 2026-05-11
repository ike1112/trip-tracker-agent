"""
FareHistory writer.

One responsibility: persist a snapshot built by `snapshot.compose_snapshot`
to the FareHistory DynamoDB table. Idempotent at the `(watchId, timestamp)`
PK so a retried Lambda invocation that re-runs the same poll won't error —
the second `put_item` overwrites cleanly.

Reads `FARE_HISTORY_TABLE_NAME` from env at import (matching the
`lambdas/poller/enumerator.py` pattern). Module-level binding is fine
because the Lambda runtime always has the env var set; tests use the
`_force_reimport` dance in `tests/conftest.py` to swap in moto-backed
tables.
"""

from __future__ import annotations

import os

import boto3

FARE_HISTORY_TABLE_NAME = os.environ.get("FARE_HISTORY_TABLE_NAME")

_dynamodb = boto3.resource("dynamodb")
_fare_history_table = (
    _dynamodb.Table(FARE_HISTORY_TABLE_NAME) if FARE_HISTORY_TABLE_NAME else None
)


def write_snapshot(snapshot: dict) -> None:
    """Persist one FareHistory row.

    The snapshot dict is written as-is — the caller (snapshot.py) is
    responsible for `Decimal` coercion of all numeric fields, since
    DynamoDB's resource API rejects native `float`.
    """
    if _fare_history_table is None:
        raise EnvironmentError("FARE_HISTORY_TABLE_NAME env var is not set")
    _fare_history_table.put_item(Item=snapshot)
