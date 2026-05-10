"""
Test fixtures for the trip-tracker poller Lambda.

Mirrors the pattern in `lambdas/travel-agent/tests/conftest.py`:
the modules under test eagerly bind a `boto3.resource(...)` at import time,
so the moto mock and the env vars must be in place BEFORE the import
happens. We achieve this by pop+re-importing inside the moto context.
"""

import importlib
import logging
import os
import sys
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws


class MemoryLogHandler(logging.Handler):
    """In-memory log handler used by tests to assert structured-log content.

    pytest's `caplog` is unreliable with `aws_lambda_powertools.Logger` (which
    sets `propagate=False` and binds its own StreamHandler at construction),
    so tests attach this handler directly to the module's logger and inspect
    `.records` after the call. Each LogRecord retains the `extra={...}` keys
    as attributes, which is what we want to pin down — the *fields* the
    poller emits, not the exact JSON serialisation (powertools owns that).
    """

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


WATCHES_TABLE = "TestWatches"
FARE_HISTORY_TABLE = "TestFareHistory"


def _create_tables(ddb):
    ddb.create_table(
        TableName=WATCHES_TABLE,
        KeySchema=[
            {"AttributeName": "userId", "KeyType": "HASH"},
            {"AttributeName": "watchId", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "userId", "AttributeType": "S"},
            {"AttributeName": "watchId", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName=FARE_HISTORY_TABLE,
        KeySchema=[
            {"AttributeName": "watchId", "KeyType": "HASH"},
            {"AttributeName": "timestamp", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "watchId", "AttributeType": "S"},
            {"AttributeName": "timestamp", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _set_env():
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["WATCHES_TABLE_NAME"] = WATCHES_TABLE
    os.environ["FARE_HISTORY_TABLE_NAME"] = FARE_HISTORY_TABLE


def _force_reimport(*module_names):
    """Drop cached imports so module-level boto3 resources rebind to moto."""
    for name in module_names:
        sys.modules.pop(name, None)
    return [importlib.import_module(name) for name in module_names]


@pytest.fixture
def enumerator_module():
    _set_env()
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_tables(ddb)
        (enumerator,) = _force_reimport("enumerator")
        # Hand the test the live mocked table too — saves boilerplate when
        # populating fixtures.
        watches = ddb.Table(WATCHES_TABLE)
        yield enumerator, watches
        sys.modules.pop("enumerator", None)


@pytest.fixture
def app_module():
    """Importable handler bound to mocked tables.

    Returns `(app, watches_tbl, fare_tbl, log_handler)` — the handler's
    `.records` list collects every log record emitted during the call.
    """
    _set_env()
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_tables(ddb)
        # `app` imports `enumerator`, so reimport both in the right order.
        sys.modules.pop("enumerator", None)
        sys.modules.pop("app", None)
        importlib.import_module("enumerator")
        app = importlib.import_module("app")
        log_handler = MemoryLogHandler()
        app.logger.addHandler(log_handler)
        watches = ddb.Table(WATCHES_TABLE)
        fare = ddb.Table(FARE_HISTORY_TABLE)
        try:
            yield app, watches, fare, log_handler
        finally:
            app.logger.removeHandler(log_handler)
            sys.modules.pop("app", None)
            sys.modules.pop("enumerator", None)


def _dec(value):
    """DDB does not accept native Python floats — coerce to Decimal."""
    if value is None:
        return None
    return Decimal(str(value))


def make_watch(
    user_id: str,
    watch_id: str,
    *,
    status: str = "active",
    destination: str = "Tokyo",
    earliest_depart: str = "2026-10-15",
    nights: int = 5,
    pax: int = 1,
    max_total_price: float = 1500.0,
    last_alerted_price: float | None = None,
    last_alerted_at: str | None = None,
    preferences: dict | None = None,
    origin="SFO",
) -> dict:
    """Synthetic Watches row matching design-spec §3 schema."""
    return {
        "userId": user_id,
        "watchId": watch_id,
        "type": "specific",
        "origin": origin,
        "destination": destination,
        "dateWindow": {
            "earliestDepart": earliest_depart,
            "latestDepart": earliest_depart,
            "nights": nights,
        },
        "pax": pax,
        "maxTotalPrice": _dec(max_total_price),
        "alertStrategy": "both",
        "preferences": preferences or {},
        "status": status,
        "lastAlertedAt": last_alerted_at,
        "lastAlertedPrice": _dec(last_alerted_price),
        "createdAt": "2026-05-01T00:00:00+00:00",
        "updatedAt": "2026-05-01T00:00:00+00:00",
    }
