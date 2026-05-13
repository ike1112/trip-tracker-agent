"""Tests for `writer.write_snapshot` — DDB round-trip + idempotency.

Every test reads the written row back from the (moto-mocked) table and
asserts on observable attributes. No "does not raise" smoke tests — a
silent write that drops a field would pass that bar but corrupt the
FareHistory time series.
"""

from decimal import Decimal

import pytest

from tests.conftest import make_flight_offer, make_hotel_offer, make_watch


def _build_snapshot(snapshot_module) -> dict:
    """Compose a known snapshot using the same composer the writer ingests."""
    return snapshot_module.compose_snapshot(
        make_watch("u1", "w-roundtrip"),
        {"source": "fixture", "offers": [
            make_flight_offer("off_UA", total="1148.00", airline="UA", flight_number="874", stops=1),
        ]},
        {"source": "fixture", "hotels": [
            make_hotel_offer("h_shibuya", total="485.00", name="Shibuya Business Hotel"),
        ]},
    )


def test_written_row_has_correct_pk(writer_module):
    writer, fare = writer_module
    import snapshot as snap_mod
    snap = _build_snapshot(snap_mod)

    writer.write_snapshot(snap)

    fetched = fare.get_item(
        Key={"watchId": snap["watchId"], "timestamp": snap["timestamp"]}
    ).get("Item")
    assert fetched is not None
    assert fetched["watchId"] == "w-roundtrip"
    assert fetched["timestamp"] == snap["timestamp"]


def test_blob_survives_round_trip_field_by_field(writer_module):
    writer, fare = writer_module
    import snapshot as snap_mod
    snap = _build_snapshot(snap_mod)

    writer.write_snapshot(snap)

    fetched = fare.get_item(
        Key={"watchId": snap["watchId"], "timestamp": snap["timestamp"]}
    )["Item"]
    blob = fetched["bestOfferBlob"]

    assert blob["airline"] == "UA"
    assert blob["flightNumber"] == "874"
    assert blob["stops"] == 1
    assert blob["hotelName"] == "Shibuya Business Hotel"
    assert blob["checkin"] == "2026-10-15"
    assert blob["checkout"] == "2026-10-20"
    assert blob["bookingDeepLink"].startswith("https://example.test")


def test_prices_round_trip_as_decimal(writer_module):
    writer, fare = writer_module
    import snapshot as snap_mod
    snap = _build_snapshot(snap_mod)

    writer.write_snapshot(snap)

    fetched = fare.get_item(
        Key={"watchId": snap["watchId"], "timestamp": snap["timestamp"]}
    )["Item"]

    assert fetched["flightPrice"] == Decimal("1148.00")
    assert fetched["hotelPrice"] == Decimal("485.00")
    assert fetched["totalPrice"] == Decimal("1633.00")
    # Sanity: not a float that DDB would have rejected anyway.
    assert isinstance(fetched["totalPrice"], Decimal)


def test_ttl_is_written_as_number(writer_module):
    writer, fare = writer_module
    import snapshot as snap_mod
    snap = _build_snapshot(snap_mod)

    writer.write_snapshot(snap)

    fetched = fare.get_item(
        Key={"watchId": snap["watchId"], "timestamp": snap["timestamp"]}
    )["Item"]

    # DDB TTL feature only honours number-type attrs; if this lands as a
    # string the rows never expire silently.
    assert isinstance(fetched["ttl"], (int, Decimal))
    assert int(fetched["ttl"]) > 0


def test_write_is_idempotent_on_same_pk(writer_module):
    """Same `(watchId, timestamp)` overwrites cleanly — no
    ConditionExpression that would reject duplicates."""
    from boto3.dynamodb.conditions import Key

    writer, fare = writer_module
    import snapshot as snap_mod
    snap = _build_snapshot(snap_mod)

    writer.write_snapshot(snap)
    writer.write_snapshot(snap)  # second write — no exception
    writer.write_snapshot(snap)  # third write

    # Query (not scan) so this exercises the real PK semantics. If the
    # writer ever started using a ConditionExpression that rejected
    # duplicates, the second put_item would have raised — so reaching
    # this assertion is itself part of the contract. The Query proves
    # there's exactly one row at the PK after three writes.
    items = fare.query(
        KeyConditionExpression=Key("watchId").eq("w-roundtrip")
    ).get("Items", [])
    assert len(items) == 1
    assert items[0]["timestamp"] == snap["timestamp"]


def test_write_fails_loud_when_table_env_var_missing(monkeypatch):
    """Misconfigured deploy → clear EnvironmentError, not NoneType.put_item."""
    import importlib
    import os
    import sys

    saved = os.environ.pop("FARE_HISTORY_TABLE_NAME", None)
    try:
        sys.modules.pop("writer", None)
        writer = importlib.import_module("writer")
        with pytest.raises(EnvironmentError, match="FARE_HISTORY_TABLE_NAME"):
            writer.write_snapshot({"watchId": "w", "timestamp": "t"})
    finally:
        if saved is not None:
            os.environ["FARE_HISTORY_TABLE_NAME"] = saved
        sys.modules.pop("writer", None)
