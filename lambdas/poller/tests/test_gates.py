"""Boundary-tested unit tests for `gates.py` — 20 cases per the T4 design.

Each test exercises a specific input → output mapping. Constants are read
directly from `gates` (e.g. `gates.DEDUP_DISCOUNT`) so a tuning change
that doesn't update the test file's intent is caught immediately.
"""

from decimal import Decimal

import pytest

import gates


# ---------------------------------------------------------------------------
# is_dedup_eligible — anti-spam gate, strict `<` at boundary.
# ---------------------------------------------------------------------------

def _watch(*, last_alerted_price=None, max_total_price=Decimal("1500")):
    return {"lastAlertedPrice": last_alerted_price, "maxTotalPrice": max_total_price}


def _snap(total):
    return {"totalPrice": total}


def test_dedup_eligible_when_last_alerted_price_is_none():
    assert gates.is_dedup_eligible(_snap(Decimal("9999")), _watch(last_alerted_price=None)) is True


def test_dedup_eligible_when_total_is_strictly_below_threshold():
    # 0.95 × 1000 = 950 → strictly below means < 950.
    assert gates.is_dedup_eligible(_snap(Decimal("949.99")), _watch(last_alerted_price=Decimal("1000"))) is True


def test_dedup_not_eligible_at_exact_discount_boundary():
    """Strict `<`: at exactly 0.95 × last → False (no re-alert)."""
    assert gates.is_dedup_eligible(_snap(Decimal("950.00")), _watch(last_alerted_price=Decimal("1000"))) is False


def test_dedup_not_eligible_when_total_equals_last_alerted_price():
    assert gates.is_dedup_eligible(_snap(Decimal("1000.00")), _watch(last_alerted_price=Decimal("1000"))) is False


def test_dedup_not_eligible_when_total_is_one_cent_above_boundary():
    assert gates.is_dedup_eligible(_snap(Decimal("950.01")), _watch(last_alerted_price=Decimal("1000"))) is False


def test_dedup_eligible_when_total_is_one_cent_below_boundary():
    assert gates.is_dedup_eligible(_snap(Decimal("949.99")), _watch(last_alerted_price=Decimal("1000"))) is True


def test_dedup_not_eligible_when_last_alerted_price_is_zero():
    """0 × 0.95 = 0; any positive total fails the strict `<` check."""
    assert gates.is_dedup_eligible(_snap(Decimal("0.01")), _watch(last_alerted_price=Decimal("0"))) is False


# ---------------------------------------------------------------------------
# passes_threshold — strict `<` per locked decision §7.4.
# ---------------------------------------------------------------------------

def test_threshold_passes_when_total_strictly_below_max():
    assert gates.passes_threshold(_snap(Decimal("1499.99")), _watch(max_total_price=Decimal("1500"))) is True


def test_threshold_fails_at_exact_max_price():
    assert gates.passes_threshold(_snap(Decimal("1500.00")), _watch(max_total_price=Decimal("1500"))) is False


def test_threshold_fails_when_total_above_max():
    assert gates.passes_threshold(_snap(Decimal("1600")), _watch(max_total_price=Decimal("1500"))) is False


def test_threshold_passes_with_large_price_spread():
    assert gates.passes_threshold(_snap(Decimal("100")), _watch(max_total_price=Decimal("1500"))) is True


# ---------------------------------------------------------------------------
# is_anomaly — `≤` for median branch, `<` for new-low branch.
# ---------------------------------------------------------------------------

def _hist(*totals):
    return [{"totalPrice": Decimal(str(t))} for t in totals]


def test_anomaly_false_on_empty_history():
    assert gates.is_anomaly(_snap(Decimal("100")), _hist()) is False


def test_anomaly_false_when_total_above_both_branches():
    """Total above 85% of median AND above min → neither branch fires.
    history [1000, 900]: median = 950; 85% × 950 = 807.5; min = 900.
    Total 950 > 807.5 (median False), 950 > 900 (new low False)."""
    assert gates.is_anomaly(_snap(Decimal("950")), _hist(1000, 900)) is False


def test_anomaly_true_on_single_row_at_exact_median_discount():
    """`≤` for median branch — exactly 85% of median IS anomalous."""
    assert gates.is_anomaly(_snap(Decimal("850")), _hist(1000)) is True


def test_anomaly_true_when_total_is_below_85pct_of_median():
    assert gates.is_anomaly(_snap(Decimal("849.99")), _hist(1000)) is True


def test_anomaly_true_when_total_is_new_30day_low():
    """history min = 900; total 899.99 strictly below → new low even
    though total is ABOVE 85% × median (median=950, 85% × 950 = 807.5)."""
    assert gates.is_anomaly(_snap(Decimal("899.99")), _hist(900, 1000)) is True


def test_anomaly_false_when_total_equals_30day_min():
    """Tying min isn't a new low (strict `<`); also median guard ignores it."""
    # statistics.median([900,1000,1100]) = 1000 (middle of odd-length sort);
    # 85% × 1000 = 850; total 900 > 850 (median branch False);
    # 900 == min (new-low branch False, strict `<`). Both False.
    assert gates.is_anomaly(_snap(Decimal("900")), _hist(900, 1000, 1100)) is False


def test_anomaly_median_branch_uses_statistics_median_for_odd_history():
    # Median of [1000, 1100, 1300, 1400, 1500] = 1300; 85% × 1300 = 1105.
    assert gates.is_anomaly(_snap(Decimal("1100")), _hist(1000, 1100, 1300, 1400, 1500)) is True


def test_anomaly_median_branch_uses_statistics_median_for_even_history():
    """Pin that median-of-even uses the average-of-middle-two, not the
    mean of the whole list. History `[800,1100,1200,1300]` has min=800
    (so we can isolate the median branch). statistics.median = (1100+1200)/2 = 1150;
    85% × 1150 = 977.5. Boundary tested at exact + just-above.
    """
    history = _hist(800, 1100, 1200, 1300)
    # 977 ≤ 977.5 → median branch True; 977 > 800 → new low False.
    assert gates.is_anomaly(_snap(Decimal("977")), history) is True
    # 978 > 977.5 → median branch False; 978 > 800 → new low False; both False.
    assert gates.is_anomaly(_snap(Decimal("978")), history) is False


def test_anomaly_false_when_total_above_median_and_above_min():
    assert gates.is_anomaly(_snap(Decimal("1500")), _hist(1000, 1100, 1200)) is False


# ---------------------------------------------------------------------------
# Constant-value pinning — boundary tests above use absolute values, so a
# silent edit to the discount constants wouldn't fail any of them. These
# tests fail loudly if a constant drifts from the value the design spec
# §5 prescribed.
# ---------------------------------------------------------------------------

def test_dedup_discount_constant_pins_to_five_percent():
    assert gates.DEDUP_DISCOUNT == 0.95


def test_anomaly_median_discount_constant_pins_to_fifteen_percent():
    assert gates.ANOMALY_MEDIAN_DISCOUNT == 0.85


def test_anomaly_handles_history_rows_without_total_price():
    """Defensive: a row that lost the totalPrice field shouldn't crash."""
    history = [{"totalPrice": Decimal("1000")}, {"someOtherField": "x"}]
    # Effective history is [1000]; total 850 = 85% × 1000 → True (median branch).
    assert gates.is_anomaly(_snap(Decimal("850")), history) is True
