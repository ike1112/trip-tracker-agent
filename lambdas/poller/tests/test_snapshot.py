"""Tests for `snapshot.compose_snapshot`.

Scope, per the T3 test design (test-engineer subagent, 2026-05-10):
- Cheapest-of-list with deterministic tiebreaker
- Empty input guards (flight, hotel, both)
- Field-by-field bestOfferBlob shape
- TTL = 90 days from now using a monkeypatched clock (no ±5s slop)
- ISO 8601 UTC timestamp that sorts lexicographically
- Tie-break determinism
- Currency mismatch raises (USD-only invariant)

Follows the prescribed Decimal handling: every price comparison uses
`Decimal("1148.00")` literals, never floats and never `pytest.approx`.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

import snapshot

from tests.conftest import make_flight_offer, make_hotel_offer, make_watch


WATCH = make_watch("u1", "w1")


# ---------------------------------------------------------------------------
# Cheapest-of-list selection — does not trust input order.
# ---------------------------------------------------------------------------

def test_picks_minimum_price_flight_from_unsorted_list():
    flights = {"source": "fixture", "offers": [
        make_flight_offer("off_ANA", total="1284.50", airline="NH", flight_number="8"),
        make_flight_offer("off_UA",  total="1148.00", airline="UA", flight_number="874"),
    ]}
    hotels = {"source": "fixture", "hotels": [make_hotel_offer()]}

    snap = snapshot.compose_snapshot(WATCH, flights, hotels)

    assert snap["flightPrice"] == Decimal("1148.00")
    assert snap["bestOfferBlob"]["airline"] == "UA"
    assert snap["bestOfferBlob"]["flightNumber"] == "874"


def test_picks_minimum_price_hotel_from_unsorted_list():
    flights = {"source": "fixture", "offers": [make_flight_offer()]}
    hotels = {"source": "fixture", "hotels": [
        make_hotel_offer("h_imperial",  total="1240.00", name="Imperial Grand"),
        make_hotel_offer("h_shibuya",   total="485.00",  name="Shibuya Business Hotel"),
        make_hotel_offer("h_park",      total="720.00",  name="Park Central"),
    ]}

    snap = snapshot.compose_snapshot(WATCH, flights, hotels)

    assert snap["hotelPrice"] == Decimal("485.00")
    assert snap["bestOfferBlob"]["hotelName"] == "Shibuya Business Hotel"


def test_total_price_is_sum_of_cheapest_flight_and_hotel():
    """Decimal arithmetic, no float rounding."""
    flights = {"source": "fixture", "offers": [
        make_flight_offer("off_UA", total="1148.00"),
    ]}
    hotels = {"source": "fixture", "hotels": [
        make_hotel_offer("h_shibuya", total="485.00"),
    ]}

    snap = snapshot.compose_snapshot(WATCH, flights, hotels)

    assert snap["totalPrice"] == Decimal("1633.00")
    # Bonus: round-trip through string survives.
    assert str(snap["totalPrice"]) == "1633.00"


# ---------------------------------------------------------------------------
# Empty-list guards.
# ---------------------------------------------------------------------------

def test_returns_none_when_flight_offers_empty():
    flights = {"source": "fixture-miss", "offers": []}
    hotels = {"source": "fixture", "hotels": [make_hotel_offer()]}

    assert snapshot.compose_snapshot(WATCH, flights, hotels) is None


def test_returns_none_when_hotel_offers_empty():
    flights = {"source": "fixture", "offers": [make_flight_offer()]}
    hotels = {"source": "fixture-miss", "hotels": []}

    assert snapshot.compose_snapshot(WATCH, flights, hotels) is None


def test_returns_none_when_both_lists_empty():
    flights = {"source": "fixture-miss", "offers": []}
    hotels = {"source": "fixture-miss", "hotels": []}

    assert snapshot.compose_snapshot(WATCH, flights, hotels) is None


# ---------------------------------------------------------------------------
# Best-offer blob — exact field set + value mappings.
# ---------------------------------------------------------------------------

EXPECTED_BLOB_KEYS = {
    "airline", "flightNumber", "stops",
    "departDate", "returnDate",
    "hotelName", "checkin", "checkout", "bookingDeepLink",
}


def test_best_offer_blob_contains_exactly_required_fields():
    flights = {"source": "fixture", "offers": [make_flight_offer()]}
    hotels = {"source": "fixture", "hotels": [make_hotel_offer()]}

    snap = snapshot.compose_snapshot(WATCH, flights, hotels)

    assert set(snap["bestOfferBlob"].keys()) == EXPECTED_BLOB_KEYS


def test_blob_stops_reflects_outbound_slice_stops():
    """`stops` is taken from slices[0], not hardcoded 0."""
    flights = {"source": "fixture", "offers": [
        make_flight_offer("off_1stop", total="1148.00", stops=1),
    ]}
    hotels = {"source": "fixture", "hotels": [make_hotel_offer()]}

    snap = snapshot.compose_snapshot(WATCH, flights, hotels)

    assert snap["bestOfferBlob"]["stops"] == 1


def test_blob_carries_through_hotel_deep_link_and_dates():
    flights = {"source": "fixture", "offers": [make_flight_offer()]}
    hotels = {"source": "fixture", "hotels": [
        make_hotel_offer(
            "h_test",
            checkin="2026-10-15",
            checkout="2026-10-20",
            deep_link="https://example.test/book/h_test",
        ),
    ]}

    snap = snapshot.compose_snapshot(WATCH, flights, hotels)
    blob = snap["bestOfferBlob"]

    assert blob["checkin"] == "2026-10-15"
    assert blob["checkout"] == "2026-10-20"
    assert blob["bookingDeepLink"] == "https://example.test/book/h_test"


def test_malformed_flight_offer_missing_slices_raises_keyerror():
    """A defensive contract: a Duffel response without `slices` has no
    `stops`/`flightNumber` we can record. Surfacing as KeyError lets the
    handler's per-watch try/except catch it as `watch_errored`.
    """
    flights = {"source": "live", "offers": [
        {"id": "off_broken", "totalAmount": "100", "currency": "USD"},
    ]}
    hotels = {"source": "fixture", "hotels": [make_hotel_offer()]}

    with pytest.raises(KeyError, match="slices"):
        snapshot.compose_snapshot(WATCH, flights, hotels)


# ---------------------------------------------------------------------------
# TTL — exact formula under a frozen clock.
# ---------------------------------------------------------------------------

def test_ttl_is_exactly_90_days_from_now(monkeypatch):
    frozen = datetime(2026, 5, 10, 0, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(snapshot, "_now", lambda: frozen)

    flights = {"source": "fixture", "offers": [make_flight_offer()]}
    hotels = {"source": "fixture", "hotels": [make_hotel_offer()]}

    snap = snapshot.compose_snapshot(WATCH, flights, hotels)

    expected = int(frozen.timestamp()) + 90 * 86400
    assert snap["ttl"] == expected


# ---------------------------------------------------------------------------
# Timestamp — ISO 8601 UTC, lexicographically sortable.
# ---------------------------------------------------------------------------

def test_timestamp_is_iso_8601_utc(monkeypatch):
    frozen = datetime(2026, 5, 10, 12, 34, 56, tzinfo=timezone.utc)
    monkeypatch.setattr(snapshot, "_now", lambda: frozen)

    flights = {"source": "fixture", "offers": [make_flight_offer()]}
    hotels = {"source": "fixture", "hotels": [make_hotel_offer()]}

    snap = snapshot.compose_snapshot(WATCH, flights, hotels)

    parsed = datetime.fromisoformat(snap["timestamp"])
    assert parsed == frozen
    # UTC offset must be present so newest-first Query semantics are correct.
    assert snap["timestamp"].endswith("+00:00")


def test_timestamps_sort_lexicographically_in_chronological_order(monkeypatch):
    """Pin the property the FareHistory schema relies on (descending Query)."""
    times = [
        datetime(2026, 5, 10,  9, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 10, 13, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 11,  0, 0, 1, tzinfo=timezone.utc),
    ]
    iso = []
    for t in times:
        monkeypatch.setattr(snapshot, "_now", lambda t=t: t)
        snap = snapshot.compose_snapshot(
            WATCH,
            {"source": "fixture", "offers": [make_flight_offer()]},
            {"source": "fixture", "hotels": [make_hotel_offer()]},
        )
        iso.append(snap["timestamp"])

    assert iso == sorted(iso)  # chronological → lexicographic


# ---------------------------------------------------------------------------
# Tie-break determinism — same input ⇒ same selection across calls.
# ---------------------------------------------------------------------------

def test_price_tie_broken_deterministically_on_id_ascending():
    flights = {"source": "fixture", "offers": [
        make_flight_offer("off_z", total="1000.00", airline="ZZ"),
        make_flight_offer("off_a", total="1000.00", airline="AA"),
        make_flight_offer("off_m", total="1000.00", airline="MM"),
    ]}
    hotels = {"source": "fixture", "hotels": [make_hotel_offer()]}

    # Ten passes — stable selection on every run.
    chosen_airlines = {
        snapshot.compose_snapshot(WATCH, flights, hotels)["bestOfferBlob"]["airline"]
        for _ in range(10)
    }

    assert chosen_airlines == {"AA"}


# ---------------------------------------------------------------------------
# Currency invariant — USD only, raise on anything else.
# ---------------------------------------------------------------------------

def test_non_usd_flight_offer_raises_value_error():
    flights = {"source": "live", "offers": [
        # GBP at a numerically lower amount — would silently win the sort.
        make_flight_offer("off_gbp", total="900.00", currency="GBP"),
        make_flight_offer("off_usd", total="1148.00", currency="USD"),
    ]}
    hotels = {"source": "fixture", "hotels": [make_hotel_offer()]}

    with pytest.raises(ValueError, match="unsupported_currency.*GBP"):
        snapshot.compose_snapshot(WATCH, flights, hotels)


def test_non_usd_hotel_offer_raises_value_error():
    flights = {"source": "fixture", "offers": [make_flight_offer()]}
    hotels = {"source": "live", "hotels": [
        make_hotel_offer("h_eur", total="400.00", currency="EUR"),
    ]}

    with pytest.raises(ValueError, match="unsupported_currency.*EUR"):
        snapshot.compose_snapshot(WATCH, flights, hotels)


# ---------------------------------------------------------------------------
# Zero-price exclusion.
# ---------------------------------------------------------------------------

def test_zero_price_offer_is_excluded_from_selection():
    """Award tickets / test data with totalAmount=0 must not become the
    'cheapest' offer — that would corrupt the FareHistory time series."""
    flights = {"source": "live", "offers": [
        make_flight_offer("off_zero", total="0"),
        make_flight_offer("off_real", total="1148.00"),
    ]}
    hotels = {"source": "fixture", "hotels": [make_hotel_offer()]}

    snap = snapshot.compose_snapshot(WATCH, flights, hotels)

    assert snap["flightPrice"] == Decimal("1148.00")


# ---------------------------------------------------------------------------
# bookingDeepLink validation — payload that lands in DDB and emails.
# ---------------------------------------------------------------------------

def test_javascript_scheme_deep_link_is_rejected():
    flights = {"source": "fixture", "offers": [make_flight_offer()]}
    hotels = {"source": "live", "hotels": [
        make_hotel_offer("h_x", deep_link="javascript:alert(1)"),
    ]}

    with pytest.raises(ValueError, match="deep_link_scheme_not_https"):
        snapshot.compose_snapshot(WATCH, flights, hotels)


def test_http_scheme_deep_link_is_rejected():
    """Even http:// is too weak — alert emails ride over secure transport
    and an http link can be intercepted to swap the booking destination."""
    flights = {"source": "fixture", "offers": [make_flight_offer()]}
    hotels = {"source": "live", "hotels": [
        make_hotel_offer("h_x", deep_link="http://example.test/h_x"),
    ]}

    with pytest.raises(ValueError, match="deep_link_scheme_not_https"):
        snapshot.compose_snapshot(WATCH, flights, hotels)


def test_oversized_deep_link_is_rejected():
    flights = {"source": "fixture", "offers": [make_flight_offer()]}
    huge_link = "https://example.test/" + ("x" * 3000)
    hotels = {"source": "live", "hotels": [
        make_hotel_offer("h_x", deep_link=huge_link),
    ]}

    with pytest.raises(ValueError, match="deep_link_too_large"):
        snapshot.compose_snapshot(WATCH, flights, hotels)


def test_missing_deep_link_becomes_empty_string_not_none():
    """A `null` from the provider must not crash the writer or land as
    `None` in DDB (which would surface as `null` in the alert email)."""
    flights = {"source": "fixture", "offers": [make_flight_offer()]}
    hotel_with_no_link = make_hotel_offer("h_x")
    hotel_with_no_link.pop("bookingDeepLink", None)
    hotels = {"source": "live", "hotels": [hotel_with_no_link]}

    snap = snapshot.compose_snapshot(WATCH, flights, hotels)

    assert snap["bestOfferBlob"]["bookingDeepLink"] == ""


def test_null_deep_link_becomes_empty_string_not_none():
    flights = {"source": "fixture", "offers": [make_flight_offer()]}
    hotels = {"source": "live", "hotels": [
        make_hotel_offer("h_x", deep_link=None),
    ]}

    snap = snapshot.compose_snapshot(WATCH, flights, hotels)

    assert snap["bestOfferBlob"]["bookingDeepLink"] == ""
