"""Behavioural tests for `app.handler` — covers happy-path dispatch,
alert=False short-circuit, SES failure policy, writeback-failure-
after-SES-success policy, malformed event payloads, and log
structure / PII posture.

Test groups:
  A: happy path dispatch
  B: alert=False short-circuit
  C: SES failure policy
  D: writeback failure after SES success
  E: malformed event payload
  F: log structure and PII posture
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import (
    MemoryLogHandler,
    _import_notifier_module,
    make_decision,
    make_handler_event,
    make_snapshot,
    make_watch,
)


def _import_handler_with_log_capture():
    """Reimport `app` and attach a MemoryLogHandler to its logger.
    Returns `(app_module, log_handler)`."""
    app = _import_notifier_module("app")
    handler = MemoryLogHandler()
    app.logger.addHandler(handler)
    return app, handler


def _no_writer_call_check(app):
    """Patch writer.write_alert_state with a MagicMock so we can
    assert call counts."""
    return patch.object(app, "write_alert_state")


# ===========================================================================
# Group A — happy path dispatch
# ===========================================================================

def test_A1_handler_with_alert_true_calls_email_template_render_once():
    app, _ = _import_handler_with_log_capture()
    event = make_handler_event()
    with patch.object(app, "write_alert_state"):
        with patch.object(app, "render", wraps=app.render) as spy_render:
            app.handler(event, MagicMock())
    assert spy_render.call_count == 1


def test_A2_handler_with_alert_true_calls_ses_client_send_once():
    app, _ = _import_handler_with_log_capture()
    event = make_handler_event()
    with patch.object(app, "write_alert_state"):
        with patch.object(app, "send", wraps=app.send) as spy_send:
            app.handler(event, MagicMock())
    assert spy_send.call_count == 1


def test_A3_handler_with_alert_true_calls_writer_write_alert_state_once():
    app, _ = _import_handler_with_log_capture()
    event = make_handler_event()
    with patch.object(app, "write_alert_state") as spy_write:
        app.handler(event, MagicMock())
    assert spy_write.call_count == 1


def test_A4_handler_returns_status_code_200_on_full_success():
    app, _ = _import_handler_with_log_capture()
    with patch.object(app, "write_alert_state"):
        response = app.handler(make_handler_event(), MagicMock())
    assert response["statusCode"] == 200


def test_A5_handler_response_includes_message_id_from_ses():
    app, _ = _import_handler_with_log_capture()
    with patch.object(app, "write_alert_state"):
        response = app.handler(make_handler_event(), MagicMock())
    assert response["messageId"].startswith("stub-")


def test_A6_handler_call_order_is_template_then_ses_then_writer():
    app, _ = _import_handler_with_log_capture()
    calls = []
    with patch.object(app, "render", side_effect=lambda *a, **kw: (calls.append("render"), ("s", "b"))[1]):
        with patch.object(app, "send", side_effect=lambda *a, **kw: (calls.append("send"), {"MessageId": "stub-12345678"})[1]):
            with patch.object(app, "write_alert_state", side_effect=lambda *a, **kw: calls.append("write")):
                app.handler(make_handler_event(), MagicMock())
    assert calls == ["render", "send", "write"]


# ===========================================================================
# Group B — alert=False short-circuit
# ===========================================================================

def test_B1_handler_with_alert_false_does_not_call_send():
    app, _ = _import_handler_with_log_capture()
    event = make_handler_event(decision=make_decision(alert=False))
    with patch.object(app, "send") as spy_send:
        with patch.object(app, "write_alert_state") as spy_write:
            app.handler(event, MagicMock())
    assert spy_send.call_count == 0
    assert spy_write.call_count == 0


def test_B2_handler_with_alert_false_emits_warning_log():
    app, log_handler = _import_handler_with_log_capture()
    event = make_handler_event(decision=make_decision(alert=False))
    app.handler(event, MagicMock())
    warn_records = [r for r in log_handler.records
                    if r.msg == "notifier_invoked_without_alert"]
    assert len(warn_records) == 1


def test_B3_handler_with_alert_false_returns_200_with_skipped_status():
    app, _ = _import_handler_with_log_capture()
    event = make_handler_event(decision=make_decision(alert=False))
    response = app.handler(event, MagicMock())
    assert response["statusCode"] == 200
    assert response["status"] == "skipped"


@pytest.mark.parametrize("truthy_non_bool", [1, "True", "yes", [1], {"x": 1}])
def test_B4_handler_with_truthy_non_bool_alert_skips_not_sends(truthy_non_bool):
    """ADR 0005's dispatch uses `decision.get("alert") is not True`,
    not `not decision.get("alert")`. Pin that posture so a regression
    that loosens it (and lets `1`/`"yes"`/etc. trigger SES) trips a
    named test. The notifier is defence-in-depth against an upstream
    that someday emits a non-bool alert value."""
    app, _ = _import_handler_with_log_capture()
    event = make_handler_event(decision={"alert": truthy_non_bool, "reason": "x", "bedrock_called": True})
    with patch.object(app, "send") as spy_send:
        with patch.object(app, "write_alert_state") as spy_write:
            response = app.handler(event, MagicMock())
    assert spy_send.call_count == 0
    assert spy_write.call_count == 0
    assert response["status"] == "skipped"


# ===========================================================================
# Group C — SES failure policy
# ===========================================================================

def test_C1_ses_send_raising_propagates_out_of_handler():
    app, _ = _import_handler_with_log_capture()
    ses_client = sys.modules["ses_client"]
    with patch.object(app, "send", side_effect=ses_client.SesSendError("Throttling")):
        with pytest.raises(ses_client.SesSendError):
            app.handler(make_handler_event(), MagicMock())


def test_C2_ses_failure_means_writer_is_never_called():
    app, _ = _import_handler_with_log_capture()
    ses_client = sys.modules["ses_client"]
    with patch.object(app, "send", side_effect=ses_client.SesSendError("Throttling")):
        with patch.object(app, "write_alert_state") as spy_write:
            with pytest.raises(ses_client.SesSendError):
                app.handler(make_handler_event(), MagicMock())
            assert spy_write.call_count == 0


def test_C3_ses_failure_emits_error_log_named_ses_send_failed():
    app, log_handler = _import_handler_with_log_capture()
    ses_client = sys.modules["ses_client"]
    with patch.object(app, "send", side_effect=ses_client.SesSendError("Throttling")):
        with pytest.raises(ses_client.SesSendError):
            app.handler(make_handler_event(), MagicMock())
    failed = [r for r in log_handler.records if r.msg == "ses_send_failed"]
    assert len(failed) == 1


def test_C4_ses_send_failed_log_does_not_include_recipient_or_error_message():
    """A future SES error class might embed the recipient in its
    message (e.g. `MessageRejected: Email address is not verified:
    user@example.test`). Pin the failure-path log to expose only
    the exception class name — never the message body."""
    app, log_handler = _import_handler_with_log_capture()
    ses_client = sys.modules["ses_client"]
    sentinel_recipient = "user@example.test"
    msg_with_recipient = (
        f"MessageRejected: Email address is not verified: {sentinel_recipient}"
    )
    with patch.object(app, "send", side_effect=ses_client.SesSendError(msg_with_recipient)):
        with pytest.raises(ses_client.SesSendError):
            app.handler(make_handler_event(), MagicMock())
    failed = [r for r in log_handler.records if r.msg == "ses_send_failed"]
    assert len(failed) == 1
    record = failed[0]
    for attr in vars(record).values():
        assert sentinel_recipient not in str(attr), (
            f"recipient leaked into ses_send_failed log: {attr!r}"
        )
        assert "MessageRejected" not in str(attr), (
            "AWS error code message leaked into ses_send_failed log"
        )
    assert getattr(record, "error", None) == "SesSendError"


# ===========================================================================
# Group D — writeback failure after SES success
# ===========================================================================

def test_D1_writer_raising_conflict_does_not_propagate():
    app, _ = _import_handler_with_log_capture()
    writer = sys.modules["writer"]
    with patch.object(app, "write_alert_state",
                      side_effect=writer.WritebackConflictError("future timestamp")):
        # Must not raise
        app.handler(make_handler_event(), MagicMock())


def test_D2_handler_still_returns_200_after_writeback_conflict():
    app, _ = _import_handler_with_log_capture()
    writer = sys.modules["writer"]
    with patch.object(app, "write_alert_state",
                      side_effect=writer.WritebackConflictError("future timestamp")):
        response = app.handler(make_handler_event(), MagicMock())
    assert response["statusCode"] == 200


def test_D3_writeback_conflict_emits_warning_log():
    app, log_handler = _import_handler_with_log_capture()
    writer = sys.modules["writer"]
    with patch.object(app, "write_alert_state",
                      side_effect=writer.WritebackConflictError("future timestamp")):
        app.handler(make_handler_event(), MagicMock())
    conflicts = [r for r in log_handler.records if r.msg == "writeback_conflict"]
    assert len(conflicts) == 1


def test_D4_writeback_conflict_log_includes_message_id():
    app, log_handler = _import_handler_with_log_capture()
    writer = sys.modules["writer"]
    with patch.object(app, "write_alert_state",
                      side_effect=writer.WritebackConflictError("future timestamp")):
        app.handler(make_handler_event(), MagicMock())
    conflicts = [r for r in log_handler.records if r.msg == "writeback_conflict"]
    assert hasattr(conflicts[0], "message_id")
    assert conflicts[0].message_id.startswith("stub-")


def test_D5_writer_raising_unexpected_clienterror_propagates():
    app, _ = _import_handler_with_log_capture()
    from botocore.exceptions import ClientError
    fake_err = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
        "UpdateItem",
    )
    with patch.object(app, "write_alert_state", side_effect=fake_err):
        with pytest.raises(ClientError):
            app.handler(make_handler_event(), MagicMock())


def test_D6_writer_value_error_does_not_propagate_returns_200():
    """SES has already delivered the email when the writer is called.
    A ValueError from `_to_decimal(None)` would otherwise trigger a
    Lambda async retry + duplicate send. Swallow and log; the next
    poll's dedup band handles any duplicate."""
    app, log_handler = _import_handler_with_log_capture()
    with patch.object(app, "write_alert_state",
                      side_effect=ValueError("lastAlertedPrice cannot be None")):
        response = app.handler(make_handler_event(), MagicMock())
    assert response["statusCode"] == 200
    value_errs = [r for r in log_handler.records if r.msg == "writeback_value_error"]
    assert len(value_errs) == 1


# ===========================================================================
# Group E — malformed event payload
# ===========================================================================

def test_E1_event_missing_snapshot_raises_keyerror_naming_snapshot():
    app, _ = _import_handler_with_log_capture()
    event = make_handler_event()
    del event["snapshot"]
    with pytest.raises(KeyError, match="snapshot"):
        app.handler(event, MagicMock())


def test_E2_event_missing_watch_raises_keyerror_naming_watch():
    app, _ = _import_handler_with_log_capture()
    event = make_handler_event()
    del event["watch"]
    with pytest.raises(KeyError, match="watch"):
        app.handler(event, MagicMock())


def test_E3_event_missing_decision_raises_keyerror_naming_decision():
    app, _ = _import_handler_with_log_capture()
    event = make_handler_event()
    del event["decision"]
    with pytest.raises(KeyError, match="decision"):
        app.handler(event, MagicMock())


def test_E4_event_with_non_dict_snapshot_raises_typeerror():
    app, _ = _import_handler_with_log_capture()
    event = make_handler_event()
    event["snapshot"] = "not a dict"
    with pytest.raises(TypeError):
        app.handler(event, MagicMock())


def test_E5_missing_key_emits_error_log_naming_the_missing_key():
    app, log_handler = _import_handler_with_log_capture()
    event = make_handler_event()
    del event["decision"]
    with pytest.raises(KeyError):
        app.handler(event, MagicMock())
    errs = [r for r in log_handler.records if r.msg == "notifier_event_missing_key"]
    assert len(errs) == 1
    assert errs[0].missing_key == "decision"


# ===========================================================================
# Group F — log structure and PII posture
# ===========================================================================

def test_F1_notification_sent_log_includes_watch_id_field():
    app, log_handler = _import_handler_with_log_capture()
    event = make_handler_event(watch=make_watch(watch_id="w-F1"))
    with patch.object(app, "write_alert_state"):
        app.handler(event, MagicMock())
    sent = [r for r in log_handler.records if r.msg == "notification_sent"]
    assert sent and sent[0].watch_id == "w-F1"


def test_F2_notification_sent_log_user_id_prefix_truncated_to_eight():
    app, log_handler = _import_handler_with_log_capture()
    event = make_handler_event(watch=make_watch(user_id="abcd1234extra-pii-stuff"))
    with patch.object(app, "write_alert_state"):
        app.handler(event, MagicMock())
    sent = [r for r in log_handler.records if r.msg == "notification_sent"]
    assert sent[0].user_id_prefix == "abcd1234"


def test_F3_notification_sent_log_includes_message_id():
    app, log_handler = _import_handler_with_log_capture()
    with patch.object(app, "write_alert_state"):
        app.handler(make_handler_event(), MagicMock())
    sent = [r for r in log_handler.records if r.msg == "notification_sent"]
    assert hasattr(sent[0], "message_id")
    assert sent[0].message_id.startswith("stub-")


def test_F4_notification_sent_log_does_not_include_recipient_email():
    app, log_handler = _import_handler_with_log_capture()
    with patch.object(app, "write_alert_state"):
        app.handler(make_handler_event(), MagicMock())
    sent = [r for r in log_handler.records if r.msg == "notification_sent"]
    record = sent[0]
    for attr in vars(record).values():
        assert "user@example.test" != str(attr), "recipient email leaked into log"


def test_F5_notification_sent_log_does_not_include_full_user_id():
    app, log_handler = _import_handler_with_log_capture()
    full_user_id = "u-12345678abcdefSECRET"
    event = make_handler_event(watch=make_watch(user_id=full_user_id))
    with patch.object(app, "write_alert_state"):
        app.handler(event, MagicMock())
    sent = [r for r in log_handler.records if r.msg == "notification_sent"]
    record = sent[0]
    for attr in vars(record).values():
        assert full_user_id != str(attr), "full user_id leaked into log"


def test_F6_notification_sent_log_does_not_include_reason_string():
    app, log_handler = _import_handler_with_log_capture()
    sentinel_reason = "SENSITIVE-REASON-CONTENT-87654"
    event = make_handler_event(decision=make_decision(reason=sentinel_reason))
    with patch.object(app, "write_alert_state"):
        app.handler(event, MagicMock())
    sent = [r for r in log_handler.records if r.msg == "notification_sent"]
    record = sent[0]
    for attr in vars(record).values():
        assert sentinel_reason not in str(attr), "reason leaked into log"


def test_F7_user_id_prefix_for_short_user_id_does_not_index_out_of_range():
    app, log_handler = _import_handler_with_log_capture()
    event = make_handler_event(watch=make_watch(user_id="abc"))
    with patch.object(app, "write_alert_state"):
        app.handler(event, MagicMock())
    sent = [r for r in log_handler.records if r.msg == "notification_sent"]
    assert sent[0].user_id_prefix == "abc"  # entire short id
