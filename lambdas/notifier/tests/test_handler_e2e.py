"""End-to-end tests for the notifier handler: exercises the full
template -> SES client mock -> writer (moto DDB) pipeline in one call with
a realistic poller-output payload.

Test groups:
  A: full pipeline with mocked SES + moto DDB
  B: out-of-order retry guard via seeded future timestamp
  C: idempotency under repeated invocations
"""

from __future__ import annotations

import importlib
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from tests.conftest import (
    MemoryLogHandler,
    WATCHES_TABLE,
    _create_watches_table,
    freeze_now,
    make_decision,
    make_handler_event,
    make_snapshot,
    make_watch,
    mock_ses_client_with_send_email_response,
    read_watch_row,
    seed_watch_row,
)


@pytest.fixture
def e2e_app(monkeypatch):
    """Stand up moto DDB + Watches table, reimport the notifier
    module graph against that environment, yield
    `(app_module, writer_module, table, log_handler)`."""
    os.environ["WATCHES_TABLE_NAME"] = WATCHES_TABLE
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_watches_table(ddb)
        # Pop in dependency order so the freshly imported `app` binds
        # to fresh ses_client / writer / email_template instances.
        for name in ("app", "writer", "email_template", "ses_client"):
            sys.modules.pop(name, None)
        ses_client = importlib.import_module("ses_client")
        mock_ses = mock_ses_client_with_send_email_response("test-msg-id-0001")
        monkeypatch.setattr(ses_client, "_get_client", lambda: mock_ses)
        email_template = importlib.import_module("email_template")
        writer = importlib.import_module("writer")
        app = importlib.import_module("app")
        log = MemoryLogHandler()
        app.logger.addHandler(log)
        table = ddb.Table(WATCHES_TABLE)
        try:
            yield app, writer, table, log
        finally:
            app.logger.removeHandler(log)
            for name in ("app", "writer", "email_template", "ses_client"):
                sys.modules.pop(name, None)


# ===========================================================================
# Group A — full pipeline
# ===========================================================================

def test_A1_e2e_handler_returns_200(e2e_app):
    app, _, table, _ = e2e_app
    watch = make_watch(user_id="u-A1", watch_id="w-A1")
    seed_watch_row(table, watch)
    response = app.handler(
        make_handler_event(snapshot=make_snapshot(watch_id="w-A1"), watch=watch),
        MagicMock(),
    )
    assert response["statusCode"] == 200


def test_A2_e2e_watches_row_lastAlertedPrice_equals_snapshot_total(e2e_app):
    app, _, table, _ = e2e_app
    watch = make_watch(user_id="u-A2", watch_id="w-A2")
    seed_watch_row(table, watch)
    app.handler(
        make_handler_event(snapshot=make_snapshot(watch_id="w-A2", total="987.65"), watch=watch),
        MagicMock(),
    )
    row = read_watch_row(table, "u-A2", "w-A2")
    assert row["lastAlertedPrice"] == Decimal("987.65")


def test_A3_e2e_watches_row_lastAlertedAt_is_recently_written(e2e_app):
    from datetime import datetime, timezone
    app, _, table, _ = e2e_app
    watch = make_watch(user_id="u-A3", watch_id="w-A3")
    seed_watch_row(table, watch)
    before = datetime.now(timezone.utc)
    app.handler(make_handler_event(snapshot=make_snapshot(watch_id="w-A3"), watch=watch), MagicMock())
    after = datetime.now(timezone.utc)
    row = read_watch_row(table, "u-A3", "w-A3")
    written = datetime.fromisoformat(row["lastAlertedAt"])
    assert before <= written <= after


def test_A4_e2e_returns_message_id_from_mocked_ses(e2e_app):
    app, _, table, _ = e2e_app
    watch = make_watch(user_id="u-A4", watch_id="w-A4")
    seed_watch_row(table, watch)
    response = app.handler(
        make_handler_event(snapshot=make_snapshot(watch_id="w-A4"), watch=watch),
        MagicMock(),
    )
    assert response["messageId"] == "test-msg-id-0001"


def test_A5_e2e_notification_sent_log_record_present_exactly_once(e2e_app):
    app, _, table, log = e2e_app
    watch = make_watch(user_id="u-A5", watch_id="w-A5")
    seed_watch_row(table, watch)
    app.handler(make_handler_event(snapshot=make_snapshot(watch_id="w-A5"), watch=watch), MagicMock())
    sent = [r for r in log.records if r.msg == "notification_sent"]
    assert len(sent) == 1


def test_A6_e2e_other_watches_fields_unchanged_post_call(e2e_app):
    app, _, table, _ = e2e_app
    watch = make_watch(user_id="u-A6", watch_id="w-A6", max_total_price=1500.0)
    seed_watch_row(table, watch)
    pre_status = watch["status"]
    pre_pref = watch["preferences"]
    app.handler(make_handler_event(snapshot=make_snapshot(watch_id="w-A6"), watch=watch), MagicMock())
    row = read_watch_row(table, "u-A6", "w-A6")
    assert row["status"] == pre_status
    assert row["preferences"] == pre_pref
    assert row["maxTotalPrice"] == Decimal("1500.0")


# ===========================================================================
# Group B — out-of-order retry guard
# ===========================================================================

def test_B1_e2e_seeded_future_lastAlertedAt_keeps_writer_from_overwriting(e2e_app, monkeypatch):
    app, writer, table, _ = e2e_app
    freeze_now(monkeypatch, writer, "2026-10-15T12:00:00+00:00")
    watch = make_watch(user_id="u-B1", watch_id="w-B1",
                       last_alerted_at="2026-11-01T08:00:00+00:00",
                       last_alerted_price=1100.0)
    seed_watch_row(table, watch)
    app.handler(make_handler_event(snapshot=make_snapshot(watch_id="w-B1"), watch=watch), MagicMock())
    row = read_watch_row(table, "u-B1", "w-B1")
    assert row["lastAlertedAt"] == "2026-11-01T08:00:00+00:00"
    assert row["lastAlertedPrice"] == Decimal("1100.0")


def test_B2_e2e_seeded_future_lastAlertedAt_still_returns_200(e2e_app, monkeypatch):
    app, writer, table, _ = e2e_app
    freeze_now(monkeypatch, writer, "2026-10-15T12:00:00+00:00")
    watch = make_watch(user_id="u-B2", watch_id="w-B2",
                       last_alerted_at="2026-11-01T08:00:00+00:00")
    seed_watch_row(table, watch)
    response = app.handler(make_handler_event(snapshot=make_snapshot(watch_id="w-B2"), watch=watch), MagicMock())
    assert response["statusCode"] == 200


def test_B3_e2e_seeded_future_lastAlertedAt_emits_writeback_conflict_log(e2e_app, monkeypatch):
    app, writer, table, log = e2e_app
    freeze_now(monkeypatch, writer, "2026-10-15T12:00:00+00:00")
    watch = make_watch(user_id="u-B3", watch_id="w-B3",
                       last_alerted_at="2026-11-01T08:00:00+00:00")
    seed_watch_row(table, watch)
    app.handler(make_handler_event(snapshot=make_snapshot(watch_id="w-B3"), watch=watch), MagicMock())
    conflicts = [r for r in log.records if r.msg == "writeback_conflict"]
    assert len(conflicts) == 1


# ===========================================================================
# Group C — idempotency under repeat
# ===========================================================================

def test_C1_two_back_to_back_invocations_yield_same_mock_message_id(e2e_app, monkeypatch):
    """Same mocked SES response across two calls. The second invocation
    hits the WritebackConflictError path because lastAlertedAt now
    equals the frozen `now`."""
    app, writer, table, _ = e2e_app
    freeze_now(monkeypatch, writer, "2026-10-15T12:00:00+00:00")
    watch = make_watch(user_id="u-C1", watch_id="w-C1")
    seed_watch_row(table, watch)
    snap = make_snapshot(watch_id="w-C1")
    r1 = app.handler(make_handler_event(snapshot=snap, watch=watch), MagicMock())
    r2 = app.handler(make_handler_event(snapshot=snap, watch=watch), MagicMock())
    assert r1["messageId"] == r2["messageId"]
    assert r1["statusCode"] == 200
    assert r2["statusCode"] == 200


def test_C2_second_invocation_with_advanced_clock_overwrites_lastAlertedAt(e2e_app, monkeypatch):
    app, writer, table, _ = e2e_app
    # First call at T0.
    freeze_now(monkeypatch, writer, "2026-10-15T12:00:00+00:00")
    watch = make_watch(user_id="u-C2", watch_id="w-C2")
    seed_watch_row(table, watch)
    app.handler(make_handler_event(snapshot=make_snapshot(watch_id="w-C2"), watch=watch), MagicMock())
    # Re-seed `watch` with what's now in the table so the conditional
    # comparison uses the just-written timestamp.
    refreshed = read_watch_row(table, "u-C2", "w-C2")
    # Advance clock.
    freeze_now(monkeypatch, writer, "2026-10-15T13:00:00+00:00")
    app.handler(make_handler_event(snapshot=make_snapshot(watch_id="w-C2"), watch=refreshed), MagicMock())
    row = read_watch_row(table, "u-C2", "w-C2")
    assert row["lastAlertedAt"] == "2026-10-15T13:00:00+00:00"
