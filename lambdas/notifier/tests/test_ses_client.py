"""Behavioural tests for `ses_client.send`.

The notifier has one production behavior: call SES. Tests mock the lazy
SES client so no real AWS call is made.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

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


def _send(message_id="msg-001"):
    ses = _import_notifier_module("ses_client")
    mock_client = mock_ses_client_with_send_email_response(message_id)
    with patch.object(ses, "_get_client", return_value=mock_client):
        result = ses.send(SENDER, RECIPIENT, SUBJECT, BODY)
    return result, mock_client


def test_A1_send_calls_send_email_exactly_once():
    _, mock_client = _send()
    assert mock_client.send_email.call_count == 1


def test_A2_send_passes_sender_as_Source_kwarg():
    _, mock_client = _send()
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Source"] == SENDER


def test_A3_send_passes_recipient_in_destination_to_addresses_list_of_one():
    _, mock_client = _send()
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Destination"] == {"ToAddresses": [RECIPIENT]}


def test_A4_send_passes_subject_text_in_message_subject_data():
    _, mock_client = _send()
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Message"]["Subject"]["Data"] == SUBJECT


def test_A5_send_passes_body_text_in_message_body_text_data():
    _, mock_client = _send()
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Message"]["Body"]["Text"]["Data"] == BODY


def test_A6_send_sets_message_subject_charset_to_utf_8():
    _, mock_client = _send()
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Message"]["Subject"]["Charset"] == "UTF-8"


def test_A7_send_sets_message_body_text_charset_to_utf_8():
    _, mock_client = _send()
    kwargs = mock_client.send_email.call_args.kwargs
    assert kwargs["Message"]["Body"]["Text"]["Charset"] == "UTF-8"


def test_A8_send_does_not_include_html_body_part():
    _, mock_client = _send()
    kwargs = mock_client.send_email.call_args.kwargs
    assert "Html" not in kwargs["Message"]["Body"]


def test_A9_send_returns_dict_with_message_id_from_boto3_response():
    result, _ = _send(message_id="amzn-msg-xyz")
    assert result["MessageId"] == "amzn-msg-xyz"


def _send_with_error(code, message="boom"):
    ses = _import_notifier_module("ses_client")
    mock_client = mock_ses_client_with_error(code=code, message=message)
    with patch.object(ses, "_get_client", return_value=mock_client):
        try:
            ses.send(SENDER, RECIPIENT, SUBJECT, BODY)
            return None, mock_client
        except Exception as e:
            return e, mock_client


def test_B1_throttling_clienterror_raises_SesSendError():
    err, _ = _send_with_error("Throttling")
    ses = sys.modules["ses_client"]
    assert isinstance(err, ses.SesSendError)


def test_B2_message_rejected_clienterror_raises_SesSendError():
    err, _ = _send_with_error("MessageRejected")
    ses = sys.modules["ses_client"]
    assert isinstance(err, ses.SesSendError)


def test_B3_SesSendError_message_contains_aws_error_code():
    err, _ = _send_with_error("MailFromDomainNotVerified")
    assert "MailFromDomainNotVerified" in str(err)


def test_B4_clienterror_is_not_swallowed_propagates_to_caller():
    err, _ = _send_with_error("Throttling")
    assert err is not None


def test_B5_non_clienterror_exception_propagates_unwrapped():
    ses = _import_notifier_module("ses_client")
    mock_client = MagicMock()
    mock_client.send_email.side_effect = RuntimeError("network blowup")
    with patch.object(ses, "_get_client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="network blowup"):
            ses.send(SENDER, RECIPIENT, SUBJECT, BODY)


def test_B6_send_does_not_retry_internally_call_count_is_one_on_failure():
    _, mock_client = _send_with_error("Throttling")
    assert mock_client.send_email.call_count == 1


def test_C1_DEFAULT_CHARSET_constant_is_pinned_to_utf_8():
    ses = _import_notifier_module("ses_client")
    assert ses.DEFAULT_CHARSET == "UTF-8"
