"""Behavioural tests for `ses_client.send` — covers stub-mode
determinism, live-mode boto3 wiring fidelity, error mapping, and mode
selection at import.

Test groups:
  A: stub-mode determinism
  B: live-mode wiring fidelity
  C: error mapping
  D: mode selection at import
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

from tests.conftest import (
    _import_notifier_module,
    mock_ses_client_with_error,
    mock_ses_client_with_send_email_response,
)


SENDER = "alerts@example.test"
RECIPIENT = "user@example.test"
SUBJECT = "test subject"
BODY = "test body"


# ===========================================================================
# Group A — stub-mode determinism
# ===========================================================================

def test_A1_stub_send_returns_message_id_with_stub_prefix():
    ses = _import_notifier_module("ses_client", ses_mode="stub")
    r = ses.send(SENDER, RECIPIENT, SUBJECT, BODY)
    assert r["MessageId"].startswith("stub-")


def test_A2_stub_send_message_id_is_stub_dash_then_eight_hex_chars():
    ses = _import_notifier_module("ses_client", ses_mode="stub")
    r = ses.send(SENDER, RECIPIENT, SUBJECT, BODY)
    mid = r["MessageId"]
    assert len(mid) == len("stub-") + 8
    hex_suffix = mid[len("stub-"):]
    assert all(c in "0123456789abcdef" for c in hex_suffix)


def test_A3_stub_send_same_inputs_yield_identical_message_id_across_calls():
    ses = _import_notifier_module("ses_client", ses_mode="stub")
    r1 = ses.send(SENDER, RECIPIENT, SUBJECT, BODY)
    r2 = ses.send(SENDER, RECIPIENT, SUBJECT, BODY)
    assert r1["MessageId"] == r2["MessageId"]


def test_A4_stub_send_different_sender_yields_different_message_id():
    ses = _import_notifier_module("ses_client", ses_mode="stub")
    r1 = ses.send(SENDER, RECIPIENT, SUBJECT, BODY)
    r2 = ses.send("different@example.test", RECIPIENT, SUBJECT, BODY)
    assert r1["MessageId"] != r2["MessageId"]


def test_A5_stub_send_different_recipient_yields_different_message_id():
    ses = _import_notifier_module("ses_client", ses_mode="stub")
    r1 = ses.send(SENDER, RECIPIENT, SUBJECT, BODY)
    r2 = ses.send(SENDER, "different@example.test", SUBJECT, BODY)
    assert r1["MessageId"] != r2["MessageId"]


def test_A6_stub_send_different_subject_yields_different_message_id():
    ses = _import_notifier_module("ses_client", ses_mode="stub")
    r1 = ses.send(SENDER, RECIPIENT, SUBJECT, BODY)
    r2 = ses.send(SENDER, RECIPIENT, "different subject", BODY)
    assert r1["MessageId"] != r2["MessageId"]


def test_A7_stub_send_different_body_yields_different_message_id():
    ses = _import_notifier_module("ses_client", ses_mode="stub")
    r1 = ses.send(SENDER, RECIPIENT, SUBJECT, BODY)
    r2 = ses.send(SENDER, RECIPIENT, SUBJECT, "different body")
    assert r1["MessageId"] != r2["MessageId"]


def test_A8_stub_send_never_constructs_boto3_client():
    ses = _import_notifier_module("ses_client", ses_mode="stub")
    with patch("boto3.client", side_effect=AssertionError("must not be called")):
        ses.send(SENDER, RECIPIENT, SUBJECT, BODY)


# ===========================================================================
# Group B — live-mode wiring fidelity
# ===========================================================================

def _live_send(message_id="msg-001"):
    ses = _import_notifier_module("ses_client", ses_mode="live")
    mock_client = mock_ses_client_with_send_email_response(message_id)
    with patch.object(ses, "_get_client", return_value=mock_client):
        result = ses.send(SENDER, RECIPIENT, SUBJECT, BODY)
    return result, mock_client


def test_B1_live_send_calls_send_email_exactly_once():
    _, mock_client = _live_send()
    assert mock_client.send_email.call_count == 1


def test_B2_live_send_passes_sender_as_Source_kwarg():
    _, mock_client = _live_send()
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Source"] == SENDER


def test_B3_live_send_passes_recipient_in_destination_to_addresses_list_of_one():
    _, mock_client = _live_send()
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Destination"] == {"ToAddresses": [RECIPIENT]}


def test_B4_live_send_passes_subject_text_in_message_subject_data():
    _, mock_client = _live_send()
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Message"]["Subject"]["Data"] == SUBJECT


def test_B5_live_send_passes_body_text_in_message_body_text_data():
    _, mock_client = _live_send()
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Message"]["Body"]["Text"]["Data"] == BODY


def test_B6_live_send_sets_message_subject_charset_to_utf_8():
    _, mock_client = _live_send()
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Message"]["Subject"]["Charset"] == "UTF-8"


def test_B7_live_send_sets_message_body_text_charset_to_utf_8():
    _, mock_client = _live_send()
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Message"]["Body"]["Text"]["Charset"] == "UTF-8"


def test_B8_live_send_does_not_include_html_body_part():
    _, mock_client = _live_send()
    kwargs = mock_client.send_email.call_args.kwargs
    assert "Html" not in kwargs["Message"]["Body"]


def test_B9_live_send_returns_dict_with_message_id_from_boto3_response():
    result, _ = _live_send(message_id="amzn-msg-xyz")
    assert result["MessageId"] == "amzn-msg-xyz"


# ===========================================================================
# Group C — error mapping
# ===========================================================================

def _live_send_with_error(code, message="boom"):
    ses = _import_notifier_module("ses_client", ses_mode="live")
    mock_client = mock_ses_client_with_error(code=code, message=message)
    with patch.object(ses, "_get_client", return_value=mock_client):
        try:
            ses.send(SENDER, RECIPIENT, SUBJECT, BODY)
            return None, mock_client
        except Exception as e:
            return e, mock_client


def test_C1_throttling_clienterror_raises_SesSendError():
    err, _ = _live_send_with_error("Throttling")
    ses = sys.modules["ses_client"]
    assert isinstance(err, ses.SesSendError)


def test_C2_message_rejected_clienterror_raises_SesSendError():
    err, _ = _live_send_with_error("MessageRejected")
    ses = sys.modules["ses_client"]
    assert isinstance(err, ses.SesSendError)


def test_C3_SesSendError_message_contains_aws_error_code():
    err, _ = _live_send_with_error("MailFromDomainNotVerified")
    assert "MailFromDomainNotVerified" in str(err)


def test_C4_clienterror_is_not_swallowed_propagates_to_caller():
    err, _ = _live_send_with_error("Throttling")
    assert err is not None


def test_C5_non_clienterror_exception_propagates_unwrapped():
    ses = _import_notifier_module("ses_client", ses_mode="live")
    from unittest.mock import MagicMock
    mock_client = MagicMock()
    mock_client.send_email.side_effect = RuntimeError("network blowup")
    with patch.object(ses, "_get_client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="network blowup"):
            ses.send(SENDER, RECIPIENT, SUBJECT, BODY)


def test_C6_send_does_not_retry_internally_call_count_is_one_on_failure():
    _, mock_client = _live_send_with_error("Throttling")
    assert mock_client.send_email.call_count == 1


# ===========================================================================
# Group D — mode selection at import
# ===========================================================================

def test_D1_import_with_SES_MODE_unset_resolves_to_live():
    os.environ.pop("SES_MODE", None)
    for m in ("ses_client",):
        sys.modules.pop(m, None)
    import ses_client
    assert ses_client.SES_MODE == "live"


def test_D2_import_with_SES_MODE_stub_resolves_to_stub():
    ses = _import_notifier_module("ses_client", ses_mode="stub")
    assert ses.SES_MODE == "stub"


def test_D3_import_with_SES_MODE_live_resolves_to_live():
    ses = _import_notifier_module("ses_client", ses_mode="live")
    assert ses.SES_MODE == "live"


def test_D4_import_with_unknown_SES_MODE_raises_importerror_naming_value():
    os.environ["SES_MODE"] = "carrier_pigeon"
    sys.modules.pop("ses_client", None)
    with pytest.raises(ImportError, match="carrier_pigeon"):
        import ses_client  # noqa: F401


def test_D5_mode_is_read_once_changing_env_after_import_has_no_effect():
    ses = _import_notifier_module("ses_client", ses_mode="stub")
    os.environ["SES_MODE"] = "live"
    assert ses.SES_MODE == "stub"  # unchanged


def test_D6_DEFAULT_CHARSET_constant_is_pinned_to_utf_8():
    ses = _import_notifier_module("ses_client", ses_mode="stub")
    assert ses.DEFAULT_CHARSET == "UTF-8"
