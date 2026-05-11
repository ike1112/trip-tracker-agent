"""Integration tests for `decision.py` in live mode (mocked Bedrock).

Slice 5's `test_decide_does_not_call_bedrock_in_slice5` confirmed the
stub never reaches boto3. This file is the slice-6 complement: confirm
that when `BEDROCK_MODE=live`, `decision.decide` actually delegates to
`bedrock_decide.decide` and the real boto3 client gets invoked. Gates
that should short-circuit (dedup-blocked, no-gate-passed) MUST NOT
reach boto3.
"""

import importlib
import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def live_decision_module():
    """Reimport bedrock_decide + decision in live mode for one test."""
    saved = os.environ.get("BEDROCK_MODE")
    os.environ["BEDROCK_MODE"] = "live"
    sys.modules.pop("bedrock_decide", None)
    sys.modules.pop("decision", None)
    importlib.import_module("bedrock_decide")
    decision = importlib.import_module("decision")
    try:
        yield decision
    finally:
        if saved is None:
            os.environ.pop("BEDROCK_MODE", None)
        else:
            os.environ["BEDROCK_MODE"] = saved
        sys.modules.pop("bedrock_decide", None)
        sys.modules.pop("decision", None)


def _watch(*, last_alerted_price=None, max_total_price=Decimal("1500")):
    return {"lastAlertedPrice": last_alerted_price, "maxTotalPrice": max_total_price}


def _snap(total):
    return {"totalPrice": Decimal(str(total)), "bestOfferBlob": {}}


def _hist(*totals):
    return [{"totalPrice": Decimal(str(t))} for t in totals]


def _bedrock_response(text: str) -> dict:
    body_bytes = json.dumps({"content": [{"type": "text", "text": text}]}).encode()
    body_stream = MagicMock()
    body_stream.read.return_value = body_bytes
    return {"body": body_stream}


# ---------------------------------------------------------------------------

def test_live_mode_threshold_pass_invokes_bedrock(live_decision_module):
    decision = live_decision_module
    import bedrock_decide
    mock_client = MagicMock()
    mock_client.invoke_model.return_value = _bedrock_response(
        '{"alert": true, "reason": "30% below 30-day median"}'
    )
    with patch.object(bedrock_decide, "_get_client", return_value=mock_client):
        result = decision.decide(_snap(1200), _watch(max_total_price=Decimal("1500")), [])

    # Real model call happened, not the stub.
    assert mock_client.invoke_model.called
    assert result["alert"] is True
    assert result["reason"] == "30% below 30-day median"
    assert result["bedrock_called"] is True


def test_live_mode_dedup_blocked_does_not_invoke_bedrock(live_decision_module):
    """Pre-gate skips must short-circuit BEFORE the model call so we
    don't pay for a decision Bedrock would have ignored."""
    decision = live_decision_module
    import bedrock_decide
    mock_client = MagicMock()
    with patch.object(bedrock_decide, "_get_client", return_value=mock_client):
        result = decision.decide(
            _snap(950),  # exactly 95% of last
            _watch(last_alerted_price=Decimal("1000"), max_total_price=Decimal("1500")),
            [],
        )

    assert not mock_client.invoke_model.called
    assert result == {"alert": False, "reason": "dedup_blocked", "bedrock_called": False}


def test_live_mode_no_gate_passed_does_not_invoke_bedrock(live_decision_module):
    decision = live_decision_module
    import bedrock_decide
    mock_client = MagicMock()
    with patch.object(bedrock_decide, "_get_client", return_value=mock_client):
        result = decision.decide(
            _snap(2000),
            _watch(max_total_price=Decimal("1000")),
            [],
        )

    assert not mock_client.invoke_model.called
    assert result == {"alert": False, "reason": "no_gate_passed", "bedrock_called": False}


def test_live_mode_bedrock_failure_returns_defensive_fallback(live_decision_module):
    """ThrottlingException from Bedrock → fallback shape; metric still
    increments (bedrock_called True) so the operator sees the attempt."""
    from botocore.exceptions import ClientError
    decision = live_decision_module
    import bedrock_decide

    mock_client = MagicMock()
    mock_client.invoke_model.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "x"}}, "InvokeModel"
    )
    with patch.object(bedrock_decide, "_get_client", return_value=mock_client):
        result = decision.decide(_snap(1200), _watch(max_total_price=Decimal("1500")), [])

    assert result == {"alert": False, "reason": "model_call_failed", "bedrock_called": True}


def test_live_mode_invalid_model_response_returns_defensive_fallback(live_decision_module):
    decision = live_decision_module
    import bedrock_decide

    mock_client = MagicMock()
    mock_client.invoke_model.return_value = _bedrock_response('garbage not json')
    with patch.object(bedrock_decide, "_get_client", return_value=mock_client):
        result = decision.decide(_snap(1200), _watch(max_total_price=Decimal("1500")), [])

    assert result == {"alert": False, "reason": "model_response_invalid", "bedrock_called": True}


def test_live_mode_anomaly_pass_alone_invokes_bedrock(live_decision_module):
    """Anomaly without threshold should still reach the model — same path
    that exercises the OR semantics from gates."""
    decision = live_decision_module
    import bedrock_decide

    mock_client = MagicMock()
    mock_client.invoke_model.return_value = _bedrock_response(
        '{"alert": false, "reason": "anomaly is misleading; price actually rose"}'
    )
    with patch.object(bedrock_decide, "_get_client", return_value=mock_client):
        # Total above maxTotalPrice → threshold False; ≤ 0.85 × median → anomaly True.
        result = decision.decide(
            _snap(850),
            _watch(max_total_price=Decimal("100")),
            _hist(1000),
        )

    assert mock_client.invoke_model.called
    assert result["bedrock_called"] is True
    # Model said no — even though a gate passed, the model decided no alert.
    # This is exactly the use case for the model: filter out false-positives
    # the gates would have surfaced.
    assert result["alert"] is False
