"""
Bedrock Haiku 4.5 wrapper for the trip-tracker poller's alert decision.

Owns the Bedrock InvokeModel call used by `decision.decide()` when the
gate cascade passes:
  - Prompt construction (system + user messages)
  - Bedrock InvokeModel call (boto3 bedrock-runtime client)
  - Strict JSON-only response parsing
  - Defensive fallback on any failure (network, IAM, throttle, malformed
    output) — never raises out of `decide()` so the per-watch try/except
    in app.py doesn't have to special-case Bedrock errors.

Modes — selected at module load time via `BEDROCK_MODE`:
  - `live` (default): real boto3 call.
  - `stub`: returns `{"alert": True, "reason": "stub", "bedrock_called": True}`
    without touching boto3. Used by every poller test in this codebase so
    no test ever burns a real Bedrock call.

Prompt-injection posture: provider-controlled strings (hotel names,
airline names) are interpolated into the USER message only — never into
the system message. A future refactor that violates this is caught by
`tests/test_bedrock_decide.py` group E (sentinel-based assertion that
the system message contains no provider data).
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from decimal import Decimal
from typing import Any

from aws_lambda_powertools import Logger

logger = Logger(service="trip-tracker-poller")

# --- Constants (pinned by tests in group B) ---------------------------------
DEFAULT_MODEL_ID = "claude-haiku-4-5-20251001"
MAX_TOKENS = 200
TEMPERATURE = 0.1  # in the locked low band (0.0–0.2)
MAX_REASON_CHARS = 200
BEDROCK_ANTHROPIC_VERSION = "bedrock-2023-05-31"

_MODE_LIVE = "live"
_MODE_STUB = "stub"
_VALID_MODES = (_MODE_LIVE, _MODE_STUB)


# --- Mode selection (at import time per test group A) -----------------------
def _resolve_mode() -> str:
    raw = os.environ.get("BEDROCK_MODE", "").strip()
    if not raw:
        return _MODE_LIVE
    if raw not in _VALID_MODES:
        raise ImportError(
            f"unsupported BEDROCK_MODE: {raw!r} — expected one of {_VALID_MODES}"
        )
    return raw


BEDROCK_MODE = _resolve_mode()
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID") or DEFAULT_MODEL_ID

# Boto3 client created lazily (and only in live mode) so stub-mode tests
# never pay the import-time cost or accidentally hit the network.
_client = None


def _get_client():
    global _client
    if _client is None:
        import boto3  # local import — keeps stub-mode startup minimal
        _client = boto3.client("bedrock-runtime")
    return _client


# --- Defensive fallback shape -----------------------------------------------
_REASON_INVALID = "model_response_invalid"
_REASON_FAILED = "model_call_failed"


def _fallback(reason_code: str) -> dict:
    return {"alert": False, "reason": reason_code, "bedrock_called": True}


# --- Prompt builder ---------------------------------------------------------
SYSTEM_PROMPT = (
    "You are deciding whether a flight+hotel price snapshot is worth alerting "
    "the user about. The user has set a budget threshold and the system has "
    "already filtered to candidates that pass at least one of: below budget, "
    "or anomaly-low vs the 30-day history. Your job is the final yes/no plus "
    "a short reason for the email body.\n"
    "\n"
    "Respond with strict JSON: {\"alert\": bool, \"reason\": string}. "
    f"`reason` must be at most {MAX_REASON_CHARS} characters. "
    "No prose, no markdown, no extra keys, no code fences."
)


def _to_float(value: Any) -> float:
    """Coerce Decimal/int/float to a plain float for prompt serialisation.

    The prompt builder f-strings need an arithmetic type that survives
    `:.2f` formatting; Decimal survives but mixing Decimal and float in
    `min()` / `statistics.median()` can crash, so we normalise here.
    """
    if value is None:
        return 0.0
    return float(value)


def _build_prompt(snapshot: dict, watch: dict, history: list[dict]) -> dict:
    """Construct the Bedrock Anthropic Messages payload.

    System message contains ONLY instructional text — no provider-controlled
    strings. User message contains the structured data the model needs to
    decide. Provider-controlled fields (`hotelName`, `airline`) land in the
    user role as data, never in the system role as instructions.
    """
    blob = snapshot.get("bestOfferBlob") or {}
    history_totals = [_to_float(h.get("totalPrice")) for h in history if "totalPrice" in h]
    median_str = (
        f"{statistics.median(history_totals):.2f}" if history_totals else "n/a"
    )
    min_str = f"{min(history_totals):.2f}" if history_totals else "n/a"

    # Use deterministic sorted preference rendering so prompt bytes are
    # reproducible across calls with the same input (test C1).
    prefs = watch.get("preferences") or {}
    prefs_str = json.dumps(prefs, sort_keys=True) if prefs else "{}"

    user_message = (
        f"Current total price (USD): {_to_float(snapshot.get('totalPrice')):.2f}\n"
        f"User's max budget (USD): {_to_float(watch.get('maxTotalPrice')):.2f}\n"
        f"30-day median total: {median_str}\n"
        f"30-day min total: {min_str}\n"
        f"Sample size: {len(history_totals)}\n"
        f"User preferences: {prefs_str}\n"
        f"Best offer details:\n"
        f"  airline: {blob.get('airline', '')}\n"
        f"  flightNumber: {blob.get('flightNumber', '')}\n"
        f"  stops: {blob.get('stops', '')}\n"
        f"  departDate: {blob.get('departDate', '')}\n"
        f"  returnDate: {blob.get('returnDate', '')}\n"
        f"  hotelName: {blob.get('hotelName', '')}\n"
        f"  checkin: {blob.get('checkin', '')}\n"
        f"  checkout: {blob.get('checkout', '')}\n"
    )

    return {
        "anthropic_version": BEDROCK_ANTHROPIC_VERSION,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_message}],
    }


# --- Response parser --------------------------------------------------------
def _parse_response(raw: str) -> dict | None:
    """Parse the model's response. Returns the dict on success, None on any
    deviation from the strict contract (caller maps None → fallback).

    Strict means:
      - The text is itself a JSON object (no markdown fences, no prose).
      - Top-level keys are exactly {alert, reason} — no extras, none missing.
      - alert is a bool (Python's json gives bool, not int).
      - reason is a non-empty string of at most MAX_REASON_CHARS.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    # No markdown fences, no preamble — first char must be '{', last '}'.
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    if set(parsed.keys()) != {"alert", "reason"}:
        return None
    alert = parsed["alert"]
    reason = parsed["reason"]
    # `bool` is a subclass of `int`; test explicitly so `1` / `0` don't pass.
    if not isinstance(alert, bool):
        return None
    if not isinstance(reason, str) or not reason:
        return None
    if len(reason) > MAX_REASON_CHARS:
        return None
    # The reason string is templated verbatim into the alert email body
    # by the Notifier. A model that emits `<script>...` or stray markup
    # could inject HTML if the email template ever loses an escape pass;
    # rejecting at the parser is a load-bearing primitive that doesn't
    # rely on every future consumer remembering to escape.
    if "<" in reason or ">" in reason:
        return None
    # Reject C0 control chars (except newline + tab) — null bytes and
    # similar codepoints crash downstream UTF-8 sinks. Reject the bidi-
    # formatting block (U+202A-U+202E + U+2066-U+2069) separately — those
    # are not < 0x20 but ARE visual-spoofing primitives for the rendered
    # alert email.
    for c in reason:
        cp = ord(c)
        if cp < 0x20 and c not in "\n\t":
            return None
        if 0x202A <= cp <= 0x202E:
            return None
        if 0x2066 <= cp <= 0x2069:
            return None
    # Lone surrogates parse as JSON strings but fail `.encode("utf-8")`;
    # one downstream `record.encode("utf-8")` later would crash the log
    # pipeline. Reject at the source.
    try:
        reason.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return {"alert": alert, "reason": reason}


# --- Public API -------------------------------------------------------------
def decide(snapshot: dict, watch: dict, history: list[dict]) -> dict:
    """Decide whether `snapshot` for `watch` is alert-worthy.

    Always returns `{"alert": bool, "reason": str, "bedrock_called": bool}`.
    `bedrock_called` is True for both stub and live modes — it tracks
    whether the gate cascade reached the model layer, not whether a real
    network call happened. This is what `bedrock_decisions_made` counts
    in app.py.
    """
    if BEDROCK_MODE == _MODE_STUB:
        return {"alert": True, "reason": "stub", "bedrock_called": True}

    body = json.dumps(_build_prompt(snapshot, watch, history))
    try:
        response = _get_client().invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        raw = response["body"].read().decode("utf-8")
    except Exception as e:
        # Note: don't use `message` as an extra key — Python logging
        # reserves it on every LogRecord and raises KeyError on collision.
        logger.warning(
            "bedrock_call_failed",
            extra={"error": type(e).__name__, "error_msg": str(e)[:200]},
        )
        return _fallback(_REASON_FAILED)

    # Bedrock's Anthropic body shape: {"content": [{"type": "text", "text": "..."}], ...}
    try:
        envelope = json.loads(raw)
        text = envelope["content"][0]["text"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        logger.warning("bedrock_envelope_invalid", extra={"raw_preview": raw[:200]})
        return _fallback(_REASON_INVALID)

    parsed = _parse_response(text)
    if parsed is None:
        logger.warning("bedrock_response_invalid", extra={"text_preview": text[:200]})
        return _fallback(_REASON_INVALID)
    return {**parsed, "bedrock_called": True}
