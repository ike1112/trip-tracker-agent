"""
Alert-worthiness decision for the trip-tracker poller.

Routing (design-spec §5):
  1. Dedup gate — if a recent alert hasn't decayed by ≥5%, return False
     immediately (anti-spam; running Bedrock here is wasted cost).
  2. Threshold OR anomaly — if either passes, the candidate is worth the
     model's attention. Delegates to `bedrock_decide.decide`, which
     returns the final `{alert, reason, bedrock_called: True}`. In slice 5
     that module was a local stub; in slice 6 it's a real Bedrock Haiku
     4.5 call selected by the `BEDROCK_MODE` env var (ADR 0004).
  3. Otherwise — no alert; reason names the missing condition.

The return shape `{alert, reason, bedrock_called}` is the slice-7
Notifier's contract — `reason` is templated into the alert email so the
user understands *why* the alert fired (design-spec §5 motivation).
"""

from __future__ import annotations

import bedrock_decide
from gates import is_anomaly, is_dedup_eligible, passes_threshold


def decide(snapshot: dict, watch: dict, history: list[dict]) -> dict:
    """Decide whether `snapshot` for `watch` is alert-worthy.

    Returns `{"alert": bool, "reason": str, "bedrock_called": bool}`:
      - `alert`/`reason` always present and non-empty so the Notifier
        has something to say.
      - `bedrock_called` is True iff the model layer was reached (i.e.,
        dedup + at least one of threshold/anomaly passed). Pre-gate
        skips return False so the `bedrock_decisions_made` metric only
        increments when an actual model invocation happened.
    """
    if not is_dedup_eligible(snapshot, watch):
        return {"alert": False, "reason": "dedup_blocked", "bedrock_called": False}

    threshold_pass = passes_threshold(snapshot, watch)
    anomaly_pass = is_anomaly(snapshot, history)

    if not (threshold_pass or anomaly_pass):
        return {"alert": False, "reason": "no_gate_passed", "bedrock_called": False}

    # Delegate to bedrock_decide — returns the same {alert, reason,
    # bedrock_called: True} shape in stub mode, live success, AND the
    # defensive-fallback path. The metric is honest about the attempt
    # regardless of whether the call succeeded.
    return bedrock_decide.decide(snapshot, watch, history)
