"""Tests for the poller-side notifier-invoke path in `app.py`:

  - `_decimal_to_str` recursively converts Decimal -> str so the
    JSON payload sent to the notifier is serialisable.
  - `_async_invoke_notifier` skips with a WARNING when
    `NOTIFIER_FUNCTION_NAME` is empty / unset.
  - `_async_invoke_notifier` calls boto3 lambda.invoke with the
    expected FunctionName + InvocationType + Payload bytes.
  - boto3 invoke errors are caught + logged, never raised back into
    the poll loop.

Test groups:
  A: Decimal-to-string serialisation
  B: missing-env short-circuit
  C: invoke wiring fidelity
  D: invoke error containment
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import MemoryLogHandler, make_watch


def _import_app():
    """Reimport `app` against current env. Pops dependent modules so
    the lazy boto3 lambda client rebinds and env-var-at-handler reads
    pick up overrides."""
    for name in ("app", "decision", "bedrock_decide", "metrics"):
        sys.modules.pop(name, None)
    return importlib.import_module("app")


def _snapshot_with_decimals() -> dict:
    return {
        "watchId": "w-001",
        "timestamp": "2026-10-15T12:00:00+00:00",
        "totalPrice": Decimal("1200.00"),
        "flightPrice": Decimal("900.00"),
        "hotelPrice": Decimal("300.00"),
        "bestOfferBlob": {
            "airline": "UA",
            "stops": 0,
            "hotelName": "Park Central",
            "bookingDeepLink": "https://example.test/h1",
        },
    }


# ===========================================================================
# Group A — Decimal-to-string serialisation
# ===========================================================================

def test_A1_decimal_to_str_converts_top_level_decimal_to_string():
    app = _import_app()
    out = app._decimal_to_str(Decimal("1200.00"))
    assert out == "1200.00"
    assert isinstance(out, str)


def test_A2_decimal_to_str_recurses_into_dict_values():
    app = _import_app()
    out = app._decimal_to_str({"a": Decimal("1.50"), "b": "leave_me"})
    assert out["a"] == "1.50"
    assert out["b"] == "leave_me"


def test_A3_decimal_to_str_recurses_into_nested_dict():
    app = _import_app()
    out = app._decimal_to_str({"outer": {"inner": Decimal("99.99")}})
    assert out["outer"]["inner"] == "99.99"


def test_A4_decimal_to_str_recurses_into_lists_and_tuples():
    app = _import_app()
    out = app._decimal_to_str([Decimal("1"), Decimal("2"), "three"])
    assert out == ["1", "2", "three"]


def test_A5_full_snapshot_round_trips_through_json_dumps_without_error():
    """The contract `_decimal_to_str` exists for: the converted
    payload must be JSON-serialisable. A regression that adds a new
    Decimal field deeper in the snapshot would surface as a
    TypeError here."""
    app = _import_app()
    snapshot = _snapshot_with_decimals()
    watch = make_watch("u-001", "w-001", max_total_price=1500.0)
    payload = {
        "snapshot": app._decimal_to_str(snapshot),
        "watch": app._decimal_to_str(watch),
        "decision": {"alert": True, "reason": "good price", "bedrock_called": True},
    }
    # Must not raise TypeError on any Decimal that escaped conversion.
    serialised = json.dumps(payload)
    assert "Decimal" not in serialised


def test_A6_decimal_to_str_leaves_non_decimal_primitives_intact():
    app = _import_app()
    assert app._decimal_to_str(42) == 42
    assert app._decimal_to_str("text") == "text"
    assert app._decimal_to_str(True) is True
    assert app._decimal_to_str(None) is None


# ===========================================================================
# Group B — missing-env short-circuit
# ===========================================================================

def test_B1_empty_notifier_function_name_skips_invoke_and_logs_warning():
    os.environ["NOTIFIER_FUNCTION_NAME"] = ""
    app = _import_app()
    log = MemoryLogHandler()
    app.logger.addHandler(log)
    with patch("boto3.client", side_effect=AssertionError("must not be called")):
        app._async_invoke_notifier(
            _snapshot_with_decimals(),
            make_watch("u-1", "w-1"),
            {"alert": True, "reason": "x", "bedrock_called": True},
            {"watch_id": "w-1"},
        )
    missing = [r for r in log.records if r.msg == "notifier_function_name_missing"]
    assert len(missing) == 1


def test_B2_unset_notifier_function_name_skips_invoke():
    os.environ.pop("NOTIFIER_FUNCTION_NAME", None)
    app = _import_app()
    log = MemoryLogHandler()
    app.logger.addHandler(log)
    with patch("boto3.client", side_effect=AssertionError("must not be called")):
        app._async_invoke_notifier(
            _snapshot_with_decimals(),
            make_watch("u-2", "w-2"),
            {"alert": True, "reason": "x", "bedrock_called": True},
            {"watch_id": "w-2"},
        )
    missing = [r for r in log.records if r.msg == "notifier_function_name_missing"]
    assert len(missing) == 1


# ===========================================================================
# Group C — invoke wiring fidelity
# ===========================================================================

def test_C1_invoke_called_with_correct_function_name_and_event_invocation_type():
    os.environ["NOTIFIER_FUNCTION_NAME"] = "test-notifier-fn"
    app = _import_app()
    mock_client = MagicMock()
    mock_client.invoke.return_value = {"StatusCode": 202}
    with patch.object(app, "_get_lambda_client", return_value=mock_client):
        app._async_invoke_notifier(
            _snapshot_with_decimals(),
            make_watch("u-c1", "w-c1"),
            {"alert": True, "reason": "good", "bedrock_called": True},
            {"watch_id": "w-c1"},
        )
    kwargs = mock_client.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == "test-notifier-fn"
    assert kwargs["InvocationType"] == "Event"


def test_C2_invoke_payload_is_utf8_encoded_json_bytes():
    os.environ["NOTIFIER_FUNCTION_NAME"] = "test-notifier-fn"
    app = _import_app()
    mock_client = MagicMock()
    mock_client.invoke.return_value = {"StatusCode": 202}
    with patch.object(app, "_get_lambda_client", return_value=mock_client):
        app._async_invoke_notifier(
            _snapshot_with_decimals(),
            make_watch("u-c2", "w-c2"),
            {"alert": True, "reason": "good", "bedrock_called": True},
            {"watch_id": "w-c2"},
        )
    payload_bytes = mock_client.invoke.call_args.kwargs["Payload"]
    assert isinstance(payload_bytes, bytes)
    payload = json.loads(payload_bytes.decode("utf-8"))
    assert payload["snapshot"]["totalPrice"] == "1200.00"  # Decimal -> string
    assert payload["watch"]["watchId"] == "w-c2"
    assert payload["decision"]["alert"] is True


# ===========================================================================
# Group D — invoke error containment
# ===========================================================================

def test_D1_invoke_raising_does_not_propagate_into_caller():
    os.environ["NOTIFIER_FUNCTION_NAME"] = "test-notifier-fn"
    app = _import_app()
    mock_client = MagicMock()
    mock_client.invoke.side_effect = RuntimeError("transient")
    log = MemoryLogHandler()
    app.logger.addHandler(log)
    with patch.object(app, "_get_lambda_client", return_value=mock_client):
        # Must not raise.
        app._async_invoke_notifier(
            _snapshot_with_decimals(),
            make_watch("u-d1", "w-d1"),
            {"alert": True, "reason": "x", "bedrock_called": True},
            {"watch_id": "w-d1"},
        )
    failed = [r for r in log.records if r.msg == "notifier_invoke_failed"]
    assert len(failed) == 1
    assert failed[0].error == "RuntimeError"


def test_D3_serialisation_typeerror_is_contained_not_raised_into_loop():
    """A future schema change that adds an unserialisable field (e.g.
    a datetime, a custom object) to the snapshot must surface as a
    contained `notifier_invoke_failed` log, NOT propagate out and
    kill the rest of the poll cycle."""
    os.environ["NOTIFIER_FUNCTION_NAME"] = "test-notifier-fn"
    app = _import_app()
    log = MemoryLogHandler()
    app.logger.addHandler(log)
    # Inject an unserialisable object into the snapshot so json.dumps
    # raises TypeError. `_decimal_to_str` doesn't know how to convert
    # a `set` either, so it passes through and `json.dumps` chokes.
    snap = _snapshot_with_decimals()
    snap["bestOfferBlob"]["unserialisable"] = {1, 2, 3}
    # Must not raise.
    app._async_invoke_notifier(
        snap,
        make_watch("u-d3", "w-d3"),
        {"alert": True, "reason": "x", "bedrock_called": True},
        {"watch_id": "w-d3"},
    )
    failed = [r for r in log.records if r.msg == "notifier_invoke_failed"]
    assert len(failed) == 1
    assert failed[0].error == "TypeError"


def test_D2_notifier_invoke_failed_log_does_not_include_error_message():
    """A raw ClientError from `lambda.invoke` can include the function
    ARN and account ID. The log should expose only the exception class
    name."""
    os.environ["NOTIFIER_FUNCTION_NAME"] = "test-notifier-fn"
    app = _import_app()
    from botocore.exceptions import ClientError
    sentinel_message = "arn:aws:lambda:us-east-1:123456789012:function:secret-fn does not exist"
    fake_err = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": sentinel_message}},
        "Invoke",
    )
    mock_client = MagicMock()
    mock_client.invoke.side_effect = fake_err
    log = MemoryLogHandler()
    app.logger.addHandler(log)
    with patch.object(app, "_get_lambda_client", return_value=mock_client):
        app._async_invoke_notifier(
            _snapshot_with_decimals(),
            make_watch("u-d2", "w-d2"),
            {"alert": True, "reason": "x", "bedrock_called": True},
            {"watch_id": "w-d2"},
        )
    failed = [r for r in log.records if r.msg == "notifier_invoke_failed"]
    assert len(failed) == 1
    record = failed[0]
    for attr in vars(record).values():
        assert sentinel_message not in str(attr), (
            f"AWS error message leaked into notifier_invoke_failed log: {attr!r}"
        )
