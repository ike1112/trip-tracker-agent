"""
Pure-functional alert gates for the trip-tracker poller.

Three boolean gates (per design-spec §5):

  * `is_dedup_eligible(snapshot, watch)`: anti-spam — must be ≥5% cheaper than
    `lastAlertedPrice` (strict `<`, not `<=`). `lastAlertedPrice = None`
    means "never alerted" → eligible.

  * `passes_threshold(snapshot, watch)`: simple budget gate — strict `<` of
    `maxTotalPrice`. A total equal to the budget does not trip the gate.

  * `is_anomaly(snapshot, history)`: catches "user-set threshold was too
    high" cases. True iff:
        totalPrice ≤ ANOMALY_MEDIAN_DISCOUNT × median(history) [≤ matches spec]
        OR
        totalPrice <  min(history)                              [strict new-low]

Constants are module-level so tunings happen in one obvious place and
tests can `gates.DEDUP_DISCOUNT` without copying magic numbers around.

Decimal handling: snapshot prices come back from DDB as `Decimal`. Gates
coerce to `float` once at the boundary so the discount-multiplier
arithmetic stays simple. Tests pass `Decimal` values to mimic the real
data shape; gates accept either.
"""

from __future__ import annotations

import statistics
from decimal import Decimal

# Tunable thresholds. Names match design-spec §5.
DEDUP_DISCOUNT = 0.95            # require ≥5% cheaper than last-alerted price
ANOMALY_MEDIAN_DISCOUNT = 0.85   # ≥15% below 30-day median triggers anomaly


def _f(value) -> float:
    """Coerce DDB Decimal / int / float to float for comparison math."""
    if value is None:
        return 0.0
    return float(value)


def is_dedup_eligible(snapshot: dict, watch: dict) -> bool:
    """True when the new total is meaningfully cheaper than the last alert.

    The first-ever alert (no `lastAlertedPrice` recorded yet) is always
    eligible. Strict `<` at the boundary so a price hovering at exactly
    0.95 × the last alert does not re-fire.
    """
    last = watch.get("lastAlertedPrice")
    if last is None:
        return True
    threshold = _f(last) * DEDUP_DISCOUNT
    return _f(snapshot["totalPrice"]) < threshold


def passes_threshold(snapshot: dict, watch: dict) -> bool:
    """Strict `<`: a total at exactly `maxTotalPrice` does NOT pass.

    Caller guarantees `watch["maxTotalPrice"]` is set — `add_watch`
    requires it as a non-optional tool argument. If the field is ever
    missing in DDB, `KeyError` will surface to the per-watch try/except
    in app.py and be logged as `watch_errored`, not silently converted
    to `False`.
    """
    return _f(snapshot["totalPrice"]) < _f(watch["maxTotalPrice"])


def is_anomaly(snapshot: dict, history: list[dict]) -> bool:
    """Anomaly = ≤85% of the 30-day median OR strictly below the 30-day min.

    Empty history → False (no baseline to anomaly against; the threshold
    gate is the only path while a watch warms up).
    """
    if not history:
        return False
    totals = [_f(h["totalPrice"]) for h in history if "totalPrice" in h]
    if not totals:
        return False
    total = _f(snapshot["totalPrice"])
    # `≤` for the median branch matches the spec's "at least 15% below"
    # phrasing — exactly 15% below is itself anomalous.
    median_branch = total <= ANOMALY_MEDIAN_DISCOUNT * statistics.median(totals)
    # Strict `<` for the new-low branch — equal to the existing min isn't
    # a new low, just a tie.
    new_low_branch = total < min(totals)
    return median_branch or new_low_branch
