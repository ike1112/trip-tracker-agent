"""
Alert-worthiness decision for the trip-tracker poller.

Slice 5: stubbed — returns `{"alert": True, "reason": "stub"}` whenever
the gate cascade would have asked Bedrock the question. Slice 6 swaps
the body for a real Bedrock Haiku 4.5 call (ADR 0004); the call sites
in app.py do NOT change.

Routing logic:
  1. Dedup gate — if a recent alert hasn't decayed by ≥5%, return False
     immediately. This is the anti-spam guard from design-spec §5; running
     Bedrock in this branch would cost without producing a useful answer.
  2. Threshold OR anomaly — if either gate passes, this is alert-worthy
     enough to ask the model. In slice 5 the model is stubbed; in slice 6
     the real call returns `{alert, reason}` from Bedrock.
  3. Otherwise — no alert, reason names the missing condition.

The return shape `{alert: bool, reason: str}` is the slice-7 Notifier's
contract — `reason` is templated into the alert email so the user
understands *why* the alert fired (the central design-spec §5 motivation).
"""

from __future__ import annotations

from gates import is_anomaly, is_dedup_eligible, passes_threshold


def decide(snapshot: dict, watch: dict, history: list[dict]) -> dict:
    """Decide whether `snapshot` for `watch` is alert-worthy.

    Returns `{"alert": bool, "reason": str}` — `reason` is always present
    and non-empty so the Notifier has something to say.
    """
    if not is_dedup_eligible(snapshot, watch):
        return {"alert": False, "reason": "dedup_blocked"}

    threshold_pass = passes_threshold(snapshot, watch)
    anomaly_pass = is_anomaly(snapshot, history)

    if not (threshold_pass or anomaly_pass):
        return {"alert": False, "reason": "no_gate_passed"}

    # Slice 6 replaces this body with a Bedrock call returning a real
    # reason string. Until then, the routing above is the actual logic
    # under test; the stub below is a placeholder for the model's answer.
    return {"alert": True, "reason": "stub"}
