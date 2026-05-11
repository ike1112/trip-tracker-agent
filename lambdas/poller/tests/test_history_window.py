"""Tests for `history_window.get_window` — time-bounded FareHistory query.

Boundary semantics matter: the anomaly gate uses min/median over the
window, so including or excluding the boundary row changes alert
behavior. The exclusive `>` is documented in the module docstring.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal


def _row(watch_id: str, ts: datetime, total: float = 1000.0) -> dict:
    return {
        "watchId": watch_id,
        "timestamp": ts.isoformat(),
        "totalPrice": Decimal(str(total)),
        "ttl": int(ts.timestamp()) + 90 * 86400,
    }


def test_returns_rows_newer_than_since_iso(history_window_module):
    history_window, fare = history_window_module
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    fare.put_item(Item=_row("w1", now - timedelta(days=35), 1500))  # outside
    fare.put_item(Item=_row("w1", now - timedelta(days=20), 1200))  # inside
    fare.put_item(Item=_row("w1", now - timedelta(days=5),  1000))  # inside

    cutoff = (now - timedelta(days=30)).isoformat()
    rows = history_window.get_window("w1", cutoff)

    assert len(rows) == 2
    assert {float(r["totalPrice"]) for r in rows} == {1200.0, 1000.0}


def test_excludes_row_at_exact_since_boundary(history_window_module):
    """Strict `>` — a row at exactly the cutoff timestamp is treated as
    older than the window."""
    history_window, fare = history_window_module
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    cutoff = (now - timedelta(days=30)).isoformat()
    fare.put_item(Item=_row("w1", now - timedelta(days=30), 1000))  # exactly at boundary

    rows = history_window.get_window("w1", cutoff)
    assert rows == []


def test_returns_empty_list_when_no_rows_in_window(history_window_module):
    history_window, _fare = history_window_module
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    cutoff = (now - timedelta(days=30)).isoformat()

    assert history_window.get_window("nonexistent-watch", cutoff) == []


def test_rows_returned_in_descending_timestamp_order(history_window_module):
    history_window, fare = history_window_module
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    # Insert out of order; the Query should still return newest-first.
    fare.put_item(Item=_row("w1", now - timedelta(days=20), 1200))
    fare.put_item(Item=_row("w1", now - timedelta(days=5),  1000))
    fare.put_item(Item=_row("w1", now - timedelta(days=10), 1100))

    cutoff = (now - timedelta(days=30)).isoformat()
    rows = history_window.get_window("w1", cutoff)
    timestamps = [r["timestamp"] for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)


def test_window_is_scoped_to_watch_id(history_window_module):
    history_window, fare = history_window_module
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    fare.put_item(Item=_row("w_A", now - timedelta(days=5), 1000))
    fare.put_item(Item=_row("w_B", now - timedelta(days=5), 9999))

    cutoff = (now - timedelta(days=30)).isoformat()
    rows = history_window.get_window("w_A", cutoff)

    assert len(rows) == 1
    assert rows[0]["watchId"] == "w_A"
    assert float(rows[0]["totalPrice"]) == 1000.0


def test_returns_single_row_history(history_window_module):
    history_window, fare = history_window_module
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    fare.put_item(Item=_row("w1", now - timedelta(days=15), 800))

    cutoff = (now - timedelta(days=30)).isoformat()
    rows = history_window.get_window("w1", cutoff)

    assert len(rows) == 1
    assert float(rows[0]["totalPrice"]) == 800.0


def test_missing_table_env_raises_clear_error():
    import importlib
    import os
    import sys
    import pytest

    saved = os.environ.pop("FARE_HISTORY_TABLE_NAME", None)
    try:
        sys.modules.pop("history_window", None)
        history_window = importlib.import_module("history_window")
        with pytest.raises(EnvironmentError, match="FARE_HISTORY_TABLE_NAME"):
            history_window.get_window("w1", "2026-01-01T00:00:00+00:00")
    finally:
        if saved is not None:
            os.environ["FARE_HISTORY_TABLE_NAME"] = saved
        sys.modules.pop("history_window", None)
