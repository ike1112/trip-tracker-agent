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
def writer_module():
    """Importable writer bound to a moto'd FareHistory table.

    Returns `(writer, fare_history_table)` so tests can write a snapshot
    and then read it back for round-trip assertions.
    """
    _set_env()
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_tables(ddb)
        (writer,) = _force_reimport("writer")
        fare = ddb.Table(FARE_HISTORY_TABLE)
        try:
            yield writer, fare
        finally:
            sys.modules.pop("writer", None)


def make_flight_offer(
    offer_id: str = "off_1",
    total: float | str = "1148.00",
    currency: str = "USD",
    airline: str = "UA",
    flight_number: str = "874",
    stops: int = 1,
    depart_at: str = "2026-10-15T10:35:00",
    return_depart_at: str = "2026-10-20T17:55:00",
) -> dict:
    """Synthetic Duffel-shaped offer for tests."""
    return {
        "id": offer_id,
        "totalAmount": total,
        "currency": currency,
        "slices": [
            {
                "stops": stops,
                "segments": [
                    {
                        "airline": airline,
                        "flightNumber": flight_number,
                        "departAt": depart_at,
                    },
                ],
            },
            {
                "stops": 0,
                "segments": [
                    {
                        "airline": airline,
                        "flightNumber": "RET",
                        "departAt": return_depart_at,
                    },
                ],
            },
        ],
    }


def make_hotel_offer(
    hotel_id: str = "h_1",
    total: float | str = "485.00",
    currency: str = "USD",
    name: str = "Shibuya Business Hotel",
    checkin: str = "2026-10-15",
    checkout: str = "2026-10-20",
    deep_link: str = "https://example.test/h_1",
) -> dict:
    """Synthetic LiteAPI-shaped hotel offer for tests."""
    return {
        "id": hotel_id,
        "totalAmount": total,
        "currency": currency,
        "hotelName": name,
        "checkin": checkin,
        "checkout": checkout,
        "bookingDeepLink": deep_link,
    }


@pytest.fixture
def app_module():
    """Importable handler bound to mocked tables.

    Returns `(app, watches_tbl, fare_tbl, log_handler)` — the handler's
    `.records` list collects every log record emitted during the call.

    Sets `JWT_SIGNATURE_SECRET` and placeholder MCP endpoints so the
    handler can construct (T2 onward); tests that need to exercise the
    MCP code path override the endpoints to point at a real mock server
    (see `test_handler_with_mcp.py`).
    """
    _set_env()
    os.environ["JWT_SIGNATURE_SECRET"] = "test-secret-aaaaaaaaaaaaaaaaaaaaa"
    os.environ.setdefault("FLIGHTS_MCP_ENDPOINT", "http://127.0.0.1:1/flights")
    os.environ.setdefault("HOTELS_MCP_ENDPOINT", "http://127.0.0.1:1/hotels")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_tables(ddb)
        # `app` imports `enumerator`, `jwt_signer`, `mcp_client`,
        # `snapshot`, `writer`. Force a clean import so module-level
        # boto3 / env-var bindings are fresh.
        for name in ("enumerator", "jwt_signer", "mcp_client", "snapshot", "writer", "app"):
            sys.modules.pop(name, None)
        importlib.import_module("enumerator")
        importlib.import_module("jwt_signer")
        importlib.import_module("mcp_client")
        importlib.import_module("snapshot")
        importlib.import_module("writer")
        app = importlib.import_module("app")
        log_handler = MemoryLogHandler()
        app.logger.addHandler(log_handler)
        watches = ddb.Table(WATCHES_TABLE)
        fare = ddb.Table(FARE_HISTORY_TABLE)
        try:
            yield app, watches, fare, log_handler
        finally:
            app.logger.removeHandler(log_handler)
            for name in ("app", "enumerator", "jwt_signer", "mcp_client", "snapshot", "writer"):
                sys.modules.pop(name, None)


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
