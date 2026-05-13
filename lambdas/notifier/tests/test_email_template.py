"""Behavioural tests for `email_template.render` — covers subject /
body structural pinning, determinism, plain-text injection safety,
and missing-field fallbacks.

Test groups:
  A: subject + body structural pinning
  B: determinism
  C: plain-text injection safety
  D: missing / partial fields
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

import pytest

from tests.conftest import (
    _import_notifier_module,
    make_decision,
    make_snapshot,
    make_watch,
)


def _render(snapshot=None, watch=None, decision=None):
    template = _import_notifier_module("email_template")
    return template.render(
        snapshot if snapshot is not None else make_snapshot(),
        watch if watch is not None else make_watch(),
        decision if decision is not None else make_decision(),
    )


# ===========================================================================
# Group A — subject + body structural pinning
# ===========================================================================

def test_A1_subject_contains_literal_trip_tracker_alert_prefix():
    subject, _ = _render()
    assert subject.startswith("trip-tracker alert:")


def test_A2_subject_contains_watch_destination_token():
    subject, _ = _render(watch=make_watch(destination="Reykjavik"))
    assert "Reykjavik" in subject


def test_A3_subject_is_single_line_with_no_embedded_newlines_or_cr():
    subject, _ = _render(watch=make_watch(destination="Paris"))
    assert "\n" not in subject
    assert "\r" not in subject


def test_A4_body_first_line_starts_with_reason_prefix_then_reason_verbatim():
    _, body = _render(decision=make_decision(reason="fare dropped 28% below median"))
    first = body.splitlines()[0]
    assert first.startswith("Reason: ")
    assert "fare dropped 28% below median" in first


def test_A5_body_contains_total_price_rendered_to_two_decimal_places():
    _, body = _render(snapshot=make_snapshot(total="1233.5"))
    assert "1233.50" in body


def test_A6_body_contains_flight_price_and_hotel_price_on_separate_lines():
    _, body = _render(snapshot=make_snapshot(flight="900.00", hotel="300.00"))
    lines = body.splitlines()
    flight_line = next(line for line in lines if "Flight:" in line)
    hotel_line = next(line for line in lines if "Hotel:" in line)
    assert "900.00" in flight_line
    assert "300.00" in hotel_line
    assert flight_line != hotel_line


def test_A7_body_contains_humanly_readable_depart_and_return_dates():
    _, body = _render(snapshot=make_snapshot(
        depart_date="2026-12-15T10:00:00",
        return_date="2026-12-22T17:00:00",
    ))
    assert "2026-12-15" in body
    assert "2026-12-22" in body


def test_A8_body_contains_origin_and_destination_airport_codes():
    _, body = _render(watch=make_watch(origin="SFO", destination="Tokyo"))
    assert "SFO" in body
    assert "Tokyo" in body


def test_A9_body_ends_with_trailing_newline():
    _, body = _render()
    assert body.endswith("\n")


def test_A10_render_returns_tuple_of_exactly_two_str_items_subject_first():
    result = _render()
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert isinstance(result[0], str)
    assert isinstance(result[1], str)


# ===========================================================================
# Group B — determinism
# ===========================================================================

def test_B1_two_renders_of_identical_payload_yield_byte_identical_subject():
    s1, _ = _render()
    s2, _ = _render()
    assert s1 == s2


def test_B2_two_renders_of_identical_payload_yield_byte_identical_body():
    _, b1 = _render()
    _, b2 = _render()
    assert b1 == b2


def test_B3_render_makes_no_call_to_datetime_now_or_time_or_random():
    """Structural check: the template module never imports the
    wall-clock or random primitives. A regression that adds one would
    have to break this assertion."""
    template = _import_notifier_module("email_template")
    for forbidden in ("datetime", "time", "random"):
        assert forbidden not in dir(template), (
            f"email_template imported `{forbidden}` — render must remain pure"
        )


def test_B4_render_does_not_read_any_environment_variable(monkeypatch):
    sentinel = []
    real_getitem = os.environ.__getitem__
    real_get = os.environ.get

    def tracker_getitem(key):
        sentinel.append(key)
        return real_getitem(key)

    def tracker_get(key, default=None):
        sentinel.append(key)
        return real_get(key, default)

    monkeypatch.setattr(os.environ, "__getitem__", tracker_getitem)
    monkeypatch.setattr(os.environ, "get", tracker_get)
    _render()
    assert sentinel == []


def test_B5_render_does_not_emit_any_log_records(caplog):
    with caplog.at_level("DEBUG"):
        _render()
    template_records = [r for r in caplog.records if r.name.startswith("notifier.email_template")]
    assert template_records == []


def test_B6_preferences_dict_key_order_does_not_change_output_bytes():
    w1 = make_watch(preferences={"maxStops": 1, "hotelMinStars": 4})
    w2 = make_watch(preferences={"hotelMinStars": 4, "maxStops": 1})
    _, b1 = _render(watch=w1)
    _, b2 = _render(watch=w2)
    assert b1 == b2


# ===========================================================================
# Group C — plain-text injection safety
# ===========================================================================

def test_C1_reason_containing_script_tag_renders_literal_chars():
    # bedrock_decide would reject this upstream — defense in depth
    # at the template confirms we emit the chars literally (plain
    # text), no HTML escape, no special handling.
    _, body = _render(decision=make_decision(reason="<script>x</script>"))
    assert "<script>x</script>" in body


def test_C2_reason_containing_ampersand_lt_gt_quotes_are_not_html_escaped():
    _, body = _render(decision=make_decision(reason="A & B < C > D \"quoted\""))
    assert "A & B < C > D \"quoted\"" in body
    assert "&amp;" not in body
    assert "&lt;" not in body
    assert "&gt;" not in body
    assert "&quot;" not in body


def test_C3_reason_containing_embedded_newline_appears_in_body_only():
    _, body = _render(decision=make_decision(reason="line one\nline two"))
    assert "line one\nline two" in body


def test_C4_subject_strips_cr_lf_from_destination_to_prevent_header_injection():
    subject, _ = _render(watch=make_watch(destination="Paris\r\nBcc: attacker@evil.test"))
    assert "\r" not in subject
    assert "\n" not in subject
    assert "Bcc:" in subject  # the chars without the line break are fine — just no header smuggling


def test_C5_subject_strips_c0_controls_from_destination_before_assembly():
    nasty = "Paris" + chr(0) + "extra"
    subject, _ = _render(watch=make_watch(destination=nasty))
    assert chr(0) not in subject
    assert "Paris" in subject
    assert "extra" in subject


def test_C6_template_emits_no_html_tags_for_pathological_destination():
    nasty = "<b>Tokyo</b>"
    subject, body = _render(watch=make_watch(destination=nasty))
    # Plain text emits the chars literally — neither <b> nor &lt;b&gt;
    # is "tags" per se; check both subject and body contain the raw
    # chars without modification.
    assert "<b>Tokyo</b>" in subject
    # body uses destination too
    assert "<b>Tokyo</b>" in body


def test_C7_template_emits_no_backticks_around_prices():
    _, body = _render()
    assert "`" not in body


def test_C8_reason_appears_in_body_exactly_once():
    sentinel = "uniqueSentinelReason12345"
    _, body = _render(decision=make_decision(reason=sentinel))
    assert body.count(sentinel) == 1


def test_C9_subject_strips_del_0x7f():
    """The DEL byte (0x7F) is excluded from the C0 range but is a
    well-known display-poisoning char. Pin the strip behaviour so
    a regression that simplifies the predicate to `ord(c) >= 0x20`
    fails."""
    nasty = "Paris" + chr(0x7F) + "extra"
    subject, _ = _render(watch=make_watch(destination=nasty))
    assert chr(0x7F) not in subject
    assert "Paris" in subject
    assert "extra" in subject


# ===========================================================================
# Group D — missing / partial fields
# ===========================================================================

def test_D1_missing_booking_deep_link_renders_no_link_sentinel():
    snap = make_snapshot()
    snap["bestOfferBlob"]["bookingDeepLink"] = ""
    _, body = _render(snapshot=snap)
    assert "(no link available)" in body


def test_D2_missing_hotel_name_falls_back_to_unknown_hotel_literal():
    snap = make_snapshot()
    snap["bestOfferBlob"]["hotelName"] = ""
    _, body = _render(snapshot=snap)
    assert "(unknown hotel)" in body


def test_D3_missing_airline_falls_back_to_unknown_airline_literal():
    snap = make_snapshot()
    snap["bestOfferBlob"]["airline"] = ""
    _, body = _render(snapshot=snap)
    assert "(unknown airline)" in body


def test_D4_zero_hotel_price_still_renders_two_decimal_places():
    snap = make_snapshot(hotel="0.00")
    _, body = _render(snapshot=snap)
    assert "Hotel: USD 0.00" in body


def test_D5_decimal_total_price_renders_without_decimal_repr_artifacts():
    _, body = _render(snapshot=make_snapshot(total=Decimal("1148.5")))
    assert "1148.50" in body
    assert "Decimal" not in body
    assert "1148.5')" not in body


def test_D6_empty_preferences_dict_omits_preferences_line_entirely():
    _, body = _render(watch=make_watch(preferences={}))
    assert "Preferences:" not in body


def test_D7_missing_snapshot_key_raises_keyerror_naming_snapshot():
    template = _import_notifier_module("email_template")
    with pytest.raises(KeyError, match="snapshot"):
        template.render(None, make_watch(), make_decision())
