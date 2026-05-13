"""
Test fixtures for the trip-tracker notifier Lambda.

Mirrors the choreography in `lambdas/poller/tests/conftest.py`:
modules under test bind boto3 resources at first call (writer.py
lazily), and stub vs live mode is selected at module import. We
manage both with a per-test reimport fixture so each case starts from
clean module state.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws


os.environ.setdefault("SES_MODE", "stub")
os.environ.setdefault("NOTIFIER_SENDER_EMAIL", "alerts@example.test")
os.environ.setdefault("NOTIFIER_RECIPIENT_EMAIL", "user@example.test")
os.environ.setdefault("WATCHES_TABLE_NAME", "TestWatches")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")


class MemoryLogHandler(logging.Handler):
    """In-memory log handler — the same pattern poller tests use, since
    powertools' Logger sets `propagate=False` and binds its own
    StreamHandler at construction (so `caplog` / `capsys` can't see
    its output). Tests attach this handler to the module's `.logger`
    and inspect `.records` for both `.msg` and the `extra` fields."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# ---------------------------------------------------------------------------
# Per-test module-state reset
# ---------------------------------------------------------------------------

_MODULES_TO_POP = ("ses_client", "email_template", "writer", "app")
_ENV_TO_PRESERVE = (
    "SES_MODE",
    "NOTIFIER_SENDER_EMAIL",
    "NOTIFIER_RECIPIENT_EMAIL",
    "WATCHES_TABLE_NAME",
)


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Pop notifier modules and save/restore env so reimports never
    leak across tests."""
    saved = {k: os.environ.get(k) for k in _ENV_TO_PRESERVE}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    for name in _MODULES_TO_POP:
        sys.modules.pop(name, None)


def _import_notifier_module(name: str, *, ses_mode: str | None = "stub"):
    """Reimport a notifier module with the given SES_MODE.

    Pops the requested module + every notifier module that imports it
    so we get a clean dependency graph. Returns the freshly-loaded
    module instance.
    """
    if ses_mode is None:
        os.environ.pop("SES_MODE", None)
    else:
        os.environ["SES_MODE"] = ses_mode
    # Pop in reverse dependency order: app -> writer/ses_client/email_template.
    for m in reversed(_MODULES_TO_POP):
        sys.modules.pop(m, None)
    return importlib.import_module(name)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _dec(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def make_snapshot(
    *,
    watch_id: str = "w-001",
    total: Any = "1200.00",
    flight: Any = "900.00",
    hotel: Any = "300.00",
    hotel_name: str = "Park Central",
    airline: str = "UA",
    booking_deep_link: str = "https://example.test/h1",
    depart_date: str = "2026-10-15T10:00:00",
    return_date: str = "2026-10-20T17:00:00",
    timestamp: str = "2026-10-15T12:00:00+00:00",
) -> dict:
    return {
        "watchId": watch_id,
        "timestamp": timestamp,
        "totalPrice": _dec(total),
        "flightPrice": _dec(flight),
        "hotelPrice": _dec(hotel),
        "bestOfferBlob": {
            "airline": airline,
            "flightNumber": "100",
            "stops": 0,
            "departDate": depart_date,
            "returnDate": return_date,
            "hotelName": hotel_name,
            "checkin": "2026-10-15",
            "checkout": "2026-10-20",
            "bookingDeepLink": booking_deep_link,
        },
    }


def make_watch(
    *,
    user_id: str = "u-12345678abcdef",
    watch_id: str = "w-001",
    destination: str = "Tokyo",
    origin: str = "SFO",
    max_total_price: float = 1500.0,
    preferences: dict | None = None,
    last_alerted_at: str | None = None,
    last_alerted_price: float | None = None,
) -> dict:
    """Build a Watches row in the same shape `watches.py` produces.

    Fields like `lastAlertedAt` / `lastAlertedPrice` are omitted
    entirely when their input is None — DDB stores absent attributes
    as truly absent (not `NULL`), so the writer's
    `attribute_not_exists(lastAlertedAt)` branch evaluates cleanly.
    """
    row = {
        "userId": user_id,
        "watchId": watch_id,
        "type": "specific",
        "origin": origin,
        "destination": destination,
        "dateWindow": {
            "earliestDepart": "2026-10-15",
            "latestDepart": "2026-10-15",
            "nights": 5,
        },
        "pax": 1,
        "maxTotalPrice": _dec(max_total_price),
        "alertStrategy": "both",
        "preferences": preferences if preferences is not None else {"maxStops": 1, "hotelMinStars": 4},
        "status": "active",
        "createdAt": "2026-05-01T00:00:00+00:00",
        "updatedAt": "2026-05-01T00:00:00+00:00",
    }
    if last_alerted_at is not None:
        row["lastAlertedAt"] = last_alerted_at
    if last_alerted_price is not None:
        row["lastAlertedPrice"] = _dec(last_alerted_price)
    return row


def make_decision(alert: bool = True, reason: str = "default reason", bedrock_called: bool = True) -> dict:
    return {"alert": alert, "reason": reason, "bedrock_called": bedrock_called}


def make_handler_event(
    *,
    snapshot: dict | None = None,
    watch: dict | None = None,
    decision: dict | None = None,
) -> dict:
    return {
        "snapshot": snapshot if snapshot is not None else make_snapshot(),
        "watch": watch if watch is not None else make_watch(),
        "decision": decision if decision is not None else make_decision(),
    }


# ---------------------------------------------------------------------------
# SES mocking helpers
# ---------------------------------------------------------------------------


def mock_ses_client_with_send_email_response(message_id: str = "test-msg-id-0001") -> MagicMock:
    """A MagicMock shaped like a boto3 SES client whose `send_email`
    returns a real-shape response with the given MessageId. Tests
    patch `ses_client._get_client` (not `boto3.client`, since the
    module-level singleton bypasses it)."""
    client = MagicMock()
    client.send_email.return_value = {
        "MessageId": message_id,
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }
    return client


def mock_ses_client_with_error(code: str = "Throttling", message: str = "Rate exceeded") -> MagicMock:
    """A MagicMock that raises a botocore ClientError with the given
    Error Code / Message when `send_email` is called."""
    from botocore.exceptions import ClientError
    client = MagicMock()
    client.send_email.side_effect = ClientError(
        {"Error": {"Code": code, "Message": message}},
        "SendEmail",
    )
    return client


# ---------------------------------------------------------------------------
# DDB Watches table fixture
# ---------------------------------------------------------------------------


WATCHES_TABLE = "TestWatches"


def _create_watches_table(ddb):
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


@pytest.fixture
def watches_table_fixture():
    """Stand up a moto-backed Watches table and yield `(writer_module,
    table)`. Resets the writer module per-test so its lazy
    `boto3.resource("dynamodb")` rebinds to moto's region."""
    os.environ["WATCHES_TABLE_NAME"] = WATCHES_TABLE
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_watches_table(ddb)
        writer = _import_notifier_module("writer")
        table = ddb.Table(WATCHES_TABLE)
        try:
            yield writer, table
        finally:
            sys.modules.pop("writer", None)


def seed_watch_row(table, watch: dict) -> None:
    table.put_item(Item=watch)


def read_watch_row(table, user_id: str, watch_id: str) -> dict:
    return table.get_item(Key={"userId": user_id, "watchId": watch_id})["Item"]


def freeze_now(monkeypatch, writer_module, iso_str: str) -> None:
    """Replace the writer module's `_now` test seam so `lastAlertedAt`
    is fully deterministic in tests."""
    from datetime import datetime
    fixed_dt = datetime.fromisoformat(iso_str)
    monkeypatch.setattr(writer_module, "_now", lambda: fixed_dt)
