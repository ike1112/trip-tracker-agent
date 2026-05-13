"""
Plain-text email template for trip-tracker alert notifications.

Owns the `render(snapshot, watch, decision) -> (subject, body)` pure
function:
  - Same inputs always yield byte-identical output (no wall clock,
    no random, no env reads, no log emission).
  - Plain text only. No HTML, no markdown. Reason strings interpolate
    verbatim with no autoescape — plain text IS the escape boundary,
    and the upstream `bedrock_decide` parser already strips HTML and
    control / bidi codepoints from `reason`.
  - Subject line stripped of CR / LF / C0 controls so a malicious
    destination string can't smuggle a header break.

Missing-field fallbacks (so a partial snapshot doesn't crash the
template at delivery time):
  - `bestOfferBlob.hotelName` absent or empty -> `(unknown hotel)`
  - `bestOfferBlob.airline` absent or empty  -> `(unknown airline)`
  - `bestOfferBlob.bookingDeepLink` absent   -> the literal line
    `Booking link: (no link available)` instead of a blank line.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

REASON_PREFIX = "Reason: "
NO_HOTEL = "(unknown hotel)"
NO_AIRLINE = "(unknown airline)"
NO_LINK = "(no link available)"


def _f2(value: Any) -> str:
    """Render a numeric (int / float / str / Decimal) as a two-decimal
    string. Goes through Decimal(str(...)) to dodge float-imprecision
    artefacts when the source happens to be a float."""
    if value is None:
        return "0.00"
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    return f"{Decimal(str(value)):.2f}"


def _strip_subject_unsafe(text: str) -> str:
    """Remove CR, LF, every byte below 0x20 (the full C0 block,
    including tab), and DEL (0x7F) from a string before assembling
    it into the subject line. Defends against email-header injection
    even though the upstream sources (watch destination, Cognito-
    provided fields) should already be clean."""
    return "".join(c for c in text if c not in "\r\n" and 0x20 <= ord(c) and ord(c) != 0x7F)


def _get_blob(snapshot: dict) -> dict:
    blob = snapshot.get("bestOfferBlob")
    return blob if isinstance(blob, dict) else {}


def _subject(watch: dict) -> str:
    destination = _strip_subject_unsafe(str(watch.get("destination", "")).strip())
    if not destination:
        destination = "(unknown destination)"
    return f"trip-tracker alert: deal found for {destination}"


def _body(snapshot: dict, watch: dict, decision: dict) -> str:
    blob = _get_blob(snapshot)
    reason = str(decision.get("reason", "")).strip() or "(no reason supplied)"
    hotel_name = (blob.get("hotelName") or "").strip() or NO_HOTEL
    airline = (blob.get("airline") or "").strip() or NO_AIRLINE
    deep_link = (blob.get("bookingDeepLink") or "").strip() or NO_LINK

    depart_date = (blob.get("departDate") or "").strip() or "(unknown)"
    return_date = (blob.get("returnDate") or "").strip() or "(unknown)"
    origin = str(watch.get("origin", "")).strip() or "(unknown)"
    destination = str(watch.get("destination", "")).strip() or "(unknown)"

    total = _f2(snapshot.get("totalPrice"))
    flight = _f2(snapshot.get("flightPrice"))
    hotel = _f2(snapshot.get("hotelPrice"))

    preferences = watch.get("preferences") or {}
    pref_lines = ""
    if preferences:
        # Deterministic key order so two renders byte-match.
        pref_lines = "\nPreferences:\n"
        for k in sorted(preferences.keys()):
            pref_lines += f"  - {k}: {preferences[k]}\n"

    lines = [
        f"{REASON_PREFIX}{reason}",
        "",
        f"Route: {origin} -> {destination}",
        f"Depart: {depart_date}",
        f"Return: {return_date}",
        "",
        f"Total: USD {total}",
        f"Flight: USD {flight} ({airline})",
        f"Hotel: USD {hotel} ({hotel_name})",
        "",
        f"Booking link: {deep_link}",
    ]
    body = "\n".join(lines)
    body += pref_lines
    # Trailing newline so SMTP / SES don't tack on garbage at the
    # boundary.
    if not body.endswith("\n"):
        body += "\n"
    return body


def render(snapshot: dict, watch: dict, decision: dict) -> tuple[str, str]:
    """Return `(subject, body)` for the alert email.

    Raises:
        KeyError: if `snapshot` is missing entirely (not a dict, or
            None). The caller's structured log names the offending key.
    """
    if not isinstance(snapshot, dict):
        raise KeyError("snapshot")
    if not isinstance(watch, dict):
        raise KeyError("watch")
    if not isinstance(decision, dict):
        raise KeyError("decision")
    return _subject(watch), _body(snapshot, watch, decision)
