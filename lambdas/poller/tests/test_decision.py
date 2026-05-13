"""Tests for `decision.decide` — gate routing under BEDROCK_MODE=stub.

These tests pin the dedup → (threshold OR anomaly) → model-delegate
flow and the `bedrock_called` metric contract so that swapping the
stub for a real Bedrock call (or back) cannot silently break the
routing or skew `bedrock_decisions_made`.
"""

from decimal import Decimal

import pytest

import decision


def _watch(*, last_alerted_price=None, max_total_price=Decimal("1500")):
    return {"lastAlertedPrice": last_alerted_price, "maxTotalPrice": max_total_price}


def _snap(total):
    return {"totalPrice": Decimal(str(total))}


def _hist(*totals):
    return [{"totalPrice": Decimal(str(t))} for t in totals]


def test_alert_true_when_dedup_passes_and_threshold_passes():
    result = decision.decide(_snap(1200), _watch(max_total_price=Decimal("1500")), [])
    assert result == {"alert": True, "reason": "stub", "bedrock_called": True}


def test_alert_true_when_dedup_passes_and_anomaly_passes_only():
    """Total above maxTotalPrice but ≤ 85% of median → anomaly fires alone."""
    result = decision.decide(
        _snap(850),
        _watch(max_total_price=Decimal("100")),  # threshold won't pass
        _hist(1000),  # median = 1000; 85% × 1000 = 850 → anomaly
    )
    assert result == {"alert": True, "reason": "stub", "bedrock_called": True}


def test_alert_false_when_dedup_blocks_even_if_threshold_would_pass():
    """Strict `<` at 0.95 × lastAlertedPrice: at exactly the boundary →
    dedup blocks even though threshold would have alerted."""
    result = decision.decide(
        _snap(950),  # exactly 95% of last
        _watch(last_alerted_price=Decimal("1000"), max_total_price=Decimal("1500")),
        [],
    )
    assert result["alert"] is False
    assert result["reason"] == "dedup_blocked"
    # Critical for metric correctness: dedup-blocked watches must NOT
    # count as Bedrock invocations.
    assert result["bedrock_called"] is False


def test_alert_false_when_neither_threshold_nor_anomaly_passes():
    result = decision.decide(
        _snap(2000),
        _watch(max_total_price=Decimal("1000")),
        [],  # empty history → anomaly always False
    )
    assert result["alert"] is False
    assert result["reason"] == "no_gate_passed"
    # Same — no model call when neither gate would justify the cost.
    assert result["bedrock_called"] is False


@pytest.mark.parametrize("scenario", [
    # (snapshot_total, last_alerted, max_price, history_totals, expected_alert)
    (1200, None,     1500, [],          True),   # threshold passes, no dedup
    (2000, None,     1500, [3000],      True),   # anomaly: 2000 ≤ 0.85×3000=2550 AND 2000<3000 (new low)
    # No-anomaly case: pick total above min so new-low fails AND above
    # 0.85×median. history [2400,2600]: min=2400, median=2500, 0.85×=2125.
    # Total 2600 > 2400 (new low F), 2600 > 2125 (median F), threshold F → False.
    (2600, None,     1500, [2400, 2600], False),
    (1200, 1300,     1500, [],          True),   # threshold passes; dedup also passes (1200 < 0.95×1300=1235)
    (1240, 1300,     1500, [],          False),  # dedup blocks (1240 > 1235)
])
def test_reason_field_always_present_and_non_empty(scenario):
    total, last, max_price, history_totals, expected_alert = scenario
    history = [{"totalPrice": Decimal(str(t))} for t in history_totals]
    result = decision.decide(
        _snap(total),
        _watch(last_alerted_price=Decimal(str(last)) if last else None,
               max_total_price=Decimal(str(max_price))),
        history,
    )
    assert result["alert"] is expected_alert
    assert "reason" in result
    assert isinstance(result["reason"], str)
    assert result["reason"]  # non-empty


def test_alert_true_when_both_threshold_and_anomaly_pass():
    """OR semantics, not AND — both passing still yields one alert."""
    result = decision.decide(
        _snap(800),
        _watch(max_total_price=Decimal("1500")),  # threshold passes
        _hist(1000),  # 85% × 1000 = 850; 800 ≤ 850 → anomaly passes
    )
    assert result["alert"] is True


def test_decide_does_not_call_boto3_in_stub_mode(monkeypatch):
    """Conftest sets BEDROCK_MODE=stub. The stub path must short-circuit
    BEFORE bedrock_decide reaches its boto3 client. Patch the actual
    factory `bedrock_decide._get_client` (not `boto3.client`, which the
    cached singleton would bypass anyway) so a regression that flips
    stub → live in this code path fails loudly."""
    import bedrock_decide

    def _no_client(*_a, **_k):
        raise AssertionError("stub mode must not instantiate a boto3 client")

    monkeypatch.setattr(bedrock_decide, "_get_client", _no_client)
    # Should not trigger the assertion.
    result = decision.decide(_snap(1200), _watch(max_total_price=Decimal("1500")), [])
    assert result == {"alert": True, "reason": "stub", "bedrock_called": True}
