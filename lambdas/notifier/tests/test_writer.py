"""Behavioural tests for `writer.write_alert_state` — covers happy
path round trip, conditional update branches, Decimal precision, and
field surgicality.

Test groups:
  A: happy path round trip
  B: conditional update branches
  C: Decimal precision and types
  D: field surgicality
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from tests.conftest import (
    freeze_now,
    make_snapshot,
    make_watch,
    read_watch_row,
    seed_watch_row,
)


ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00$")


# ===========================================================================
# Group A — happy path round trip
# ===========================================================================

def test_A1_first_alert_write_populates_lastAlertedAt_as_iso_utc_string(watches_table_fixture):
    writer, table = watches_table_fixture
    watch = make_watch(user_id="u-A1", watch_id="w-A1")
    seed_watch_row(table, watch)
    writer.write_alert_state(watch, "1200.00")
    row = read_watch_row(table, "u-A1", "w-A1")
    assert isinstance(row["lastAlertedAt"], str)
    assert ISO_UTC_RE.match(row["lastAlertedAt"]) is not None


def test_A2_first_alert_write_populates_lastAlertedPrice_as_decimal(watches_table_fixture):
    writer, table = watches_table_fixture
    watch = make_watch(user_id="u-A2", watch_id="w-A2")
    seed_watch_row(table, watch)
    writer.write_alert_state(watch, "1200.00")
    row = read_watch_row(table, "u-A2", "w-A2")
    assert isinstance(row["lastAlertedPrice"], Decimal)
    assert row["lastAlertedPrice"] == Decimal("1200.00")


def test_A3_lastAlertedAt_format_matches_iso_utc_offset(watches_table_fixture):
    writer, table = watches_table_fixture
    watch = make_watch(user_id="u-A3", watch_id="w-A3")
    seed_watch_row(table, watch)
    writer.write_alert_state(watch, "1500.00")
    row = read_watch_row(table, "u-A3", "w-A3")
    assert "+00:00" in row["lastAlertedAt"]


def test_A4_write_returns_None_on_success(watches_table_fixture):
    writer, table = watches_table_fixture
    watch = make_watch(user_id="u-A4", watch_id="w-A4")
    seed_watch_row(table, watch)
    result = writer.write_alert_state(watch, "999.00")
    assert result is None


# ===========================================================================
# Group B — conditional update branches
# ===========================================================================

def test_B1_write_succeeds_when_lastAlertedAt_attribute_absent(watches_table_fixture):
    writer, table = watches_table_fixture
    watch = make_watch(user_id="u-B1", watch_id="w-B1", last_alerted_at=None)
    seed_watch_row(table, watch)
    writer.write_alert_state(watch, "1200.00")
    row = read_watch_row(table, "u-B1", "w-B1")
    assert "lastAlertedAt" in row
    assert row["lastAlertedAt"] is not None


def test_B2_write_succeeds_when_existing_lastAlertedAt_strictly_older(watches_table_fixture, monkeypatch):
    writer, table = watches_table_fixture
    freeze_now(monkeypatch, writer, "2026-10-15T12:00:00+00:00")
    watch = make_watch(user_id="u-B2", watch_id="w-B2",
                       last_alerted_at="2026-10-14T08:00:00+00:00")
    seed_watch_row(table, watch)
    writer.write_alert_state(watch, "1200.00")
    row = read_watch_row(table, "u-B2", "w-B2")
    assert row["lastAlertedAt"] == "2026-10-15T12:00:00+00:00"


def test_B3_write_raises_conflict_when_existing_equals_now(watches_table_fixture, monkeypatch):
    writer, table = watches_table_fixture
    freeze_now(monkeypatch, writer, "2026-10-15T12:00:00+00:00")
    watch = make_watch(user_id="u-B3", watch_id="w-B3",
                       last_alerted_at="2026-10-15T12:00:00+00:00")
    seed_watch_row(table, watch)
    with pytest.raises(writer.WritebackConflictError):
        writer.write_alert_state(watch, "1200.00")


def test_B4_write_raises_conflict_when_existing_in_the_future(watches_table_fixture, monkeypatch):
    writer, table = watches_table_fixture
    freeze_now(monkeypatch, writer, "2026-10-15T12:00:00+00:00")
    watch = make_watch(user_id="u-B4", watch_id="w-B4",
                       last_alerted_at="2026-10-20T08:00:00+00:00")
    seed_watch_row(table, watch)
    with pytest.raises(writer.WritebackConflictError):
        writer.write_alert_state(watch, "1200.00")


def test_B6_write_succeeds_on_legacy_null_valued_lastAlertedAt(watches_table_fixture):
    """Legacy watches created before the create_watch fix carry
    `lastAlertedAt`/`lastAlertedPrice` as DDB NULL-typed attributes
    (boto3 marshals Python `None` that way). `attribute_not_exists`
    returns false on those — the attribute IS there, just NULL. The
    writer must treat NULL as semantically equivalent to "never
    alerted" or the dedup gate never arms for the first alert on any
    legacy row.
    """
    writer, table = watches_table_fixture
    # Bypass the conftest helper (which omits these keys) and put a row
    # that explicitly carries NULL-valued lastAlerted* — exactly the
    # shape produced by the pre-fix create_watch in lambdas/travel-agent.
    legacy_row = make_watch(user_id="u-B6", watch_id="w-B6")
    legacy_row["lastAlertedAt"] = None
    legacy_row["lastAlertedPrice"] = None
    table.put_item(Item=legacy_row)
    writer.write_alert_state({"userId": "u-B6", "watchId": "w-B6"}, "1200.00")
    row = read_watch_row(table, "u-B6", "w-B6")
    assert isinstance(row["lastAlertedAt"], str)
    assert row["lastAlertedPrice"] == Decimal("1200.00")


def test_B5_conflict_error_only_for_conditional_check_failure_code(watches_table_fixture):
    writer, table = watches_table_fixture
    # Forge a different ClientError code by patching update_item.
    from unittest.mock import patch
    from botocore.exceptions import ClientError
    watch = make_watch(user_id="u-B5", watch_id="w-B5")
    seed_watch_row(table, watch)
    fake_err = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
        "UpdateItem",
    )
    with patch.object(table, "update_item", side_effect=fake_err):
        # The writer module's table is captured at import; we need
        # to monkeypatch its `_get_table` instead.
        with patch.object(writer, "_get_table", return_value=table):
            with pytest.raises(ClientError):
                writer.write_alert_state(watch, "1200.00")


# ===========================================================================
# Group C — Decimal precision and types
# ===========================================================================

def test_C1_string_input_coerces_to_decimal(watches_table_fixture):
    writer, table = watches_table_fixture
    watch = make_watch(user_id="u-C1", watch_id="w-C1")
    seed_watch_row(table, watch)
    writer.write_alert_state(watch, "1148.00")
    row = read_watch_row(table, "u-C1", "w-C1")
    assert row["lastAlertedPrice"] == Decimal("1148.00")


def test_C2_float_input_coerces_via_str_not_decimal_float(watches_table_fixture):
    writer, table = watches_table_fixture
    watch = make_watch(user_id="u-C2", watch_id="w-C2")
    seed_watch_row(table, watch)
    writer.write_alert_state(watch, 1148.0)
    row = read_watch_row(table, "u-C2", "w-C2")
    # Decimal(str(1148.0)) -> Decimal("1148.0"), not the float garbage
    # Decimal(1148.0) would produce.
    assert row["lastAlertedPrice"] == Decimal("1148.0")


def test_C3_stored_lastAlertedPrice_is_decimal_not_float(watches_table_fixture):
    writer, table = watches_table_fixture
    watch = make_watch(user_id="u-C3", watch_id="w-C3")
    seed_watch_row(table, watch)
    writer.write_alert_state(watch, "1200.00")
    row = read_watch_row(table, "u-C3", "w-C3")
    assert not isinstance(row["lastAlertedPrice"], float)


def test_C4_lastAlertedPrice_round_trip_preserves_two_decimal_places(watches_table_fixture):
    writer, table = watches_table_fixture
    watch = make_watch(user_id="u-C4", watch_id="w-C4")
    seed_watch_row(table, watch)
    writer.write_alert_state(watch, "1148.00")
    row = read_watch_row(table, "u-C4", "w-C4")
    assert str(row["lastAlertedPrice"]) == "1148.00"


# ===========================================================================
# Group D — field surgicality
# ===========================================================================

def test_D1_status_field_unchanged_after_write(watches_table_fixture):
    writer, table = watches_table_fixture
    watch = make_watch(user_id="u-D1", watch_id="w-D1")
    seed_watch_row(table, watch)
    writer.write_alert_state(watch, "1200.00")
    row = read_watch_row(table, "u-D1", "w-D1")
    assert row["status"] == "active"


def test_D2_maxTotalPrice_field_unchanged_after_write(watches_table_fixture):
    writer, table = watches_table_fixture
    watch = make_watch(user_id="u-D2", watch_id="w-D2", max_total_price=1500.0)
    seed_watch_row(table, watch)
    writer.write_alert_state(watch, "1200.00")
    row = read_watch_row(table, "u-D2", "w-D2")
    assert row["maxTotalPrice"] == Decimal("1500.0")


def test_D3_preferences_dict_unchanged_after_write(watches_table_fixture):
    writer, table = watches_table_fixture
    prefs = {"maxStops": 2, "hotelMinStars": 5}
    watch = make_watch(user_id="u-D3", watch_id="w-D3", preferences=prefs)
    seed_watch_row(table, watch)
    writer.write_alert_state(watch, "1200.00")
    row = read_watch_row(table, "u-D3", "w-D3")
    assert row["preferences"] == prefs


def test_D4_dateWindow_subobject_unchanged_after_write(watches_table_fixture):
    writer, table = watches_table_fixture
    watch = make_watch(user_id="u-D4", watch_id="w-D4")
    expected_window = watch["dateWindow"]
    seed_watch_row(table, watch)
    writer.write_alert_state(watch, "1200.00")
    row = read_watch_row(table, "u-D4", "w-D4")
    assert row["dateWindow"] == expected_window


def test_D5_createdAt_field_unchanged_after_write(watches_table_fixture):
    writer, table = watches_table_fixture
    watch = make_watch(user_id="u-D5", watch_id="w-D5")
    seed_watch_row(table, watch)
    writer.write_alert_state(watch, "1200.00")
    row = read_watch_row(table, "u-D5", "w-D5")
    assert row["createdAt"] == "2026-05-01T00:00:00+00:00"
