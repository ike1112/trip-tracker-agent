"""
FareHistory snapshot composer.

Takes the raw `flights_payload` and `hotels_payload` returned by `mcp_client`
(`{"source": ..., "offers": [...]}` and `{"source": ..., "hotels": [...]}`),
picks the cheapest offer + cheapest hotel, and assembles a row matching
design-spec §3 schema for the `FareHistory` table.

Design notes:

- **Cheapest-of-list with deterministic tiebreaker.** The MCP tools sort by
  price already, but we re-sort defensively so a future change in tool
  semantics doesn't silently corrupt the snapshot. Tiebreaker is the
  offer/hotel `id` ascending so two equally-cheap snapshots are always
  resolved the same way (tests rely on this).

- **USD only — fail loud on currency mismatch.** Mirrors the LiteAPI live
  client's stance documented in the threat model (boundary [3b]):
  silent unit conversion would corrupt the FareHistory time series in a
  way that's invisible to the user *and* permanent. Raise loud; the
  per-watch try/except in app.py catches and logs `watch_errored`.

- **Zero-price offers are excluded.** A `totalAmount` of 0 almost always
  indicates test data, award tickets, or a provider error — never a
  real opportunity worth alerting on.

- **`bestOfferBlob` denormalisation.** Per design-spec §3 we duplicate the
  parts of the MCP response we'll need for the alert email so the
  Notifier (slice 7) can compose the message without re-querying any
  provider.

- **`source` field captured.** We don't have provider request IDs at the
  MCP-tool level (only the `source` debug field), so we write `source`
  into the legacy `duffelRequestId` / `liteApiRequestId` slots until
  slice 6+ work makes the live client surface real request IDs. Better
  than empty.

- **All numeric fields are `Decimal`.** DDB rejects native `float`. Coerce
  via `Decimal(str(value))` so float imprecision never enters the data
  path. Same convention `lambdas/travel-agent/tests/conftest.py` uses for
  test fixtures.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

USD = "USD"
TTL_DAYS = 90
# bookingDeepLink lands in DDB and is later embedded in alert emails by the
# Notifier (slice 7). Bound the field so a misbehaving (or compromised) MCP
# response can't ship a multi-MB string into the time series, and reject
# any scheme other than https so a `javascript:` payload can't reach a
# user's mail client.
MAX_DEEP_LINK_BYTES = 2048
ALLOWED_DEEP_LINK_SCHEME = "https://"


def _now() -> datetime:
    """Test seam — patch this to freeze the clock in unit tests.

    Returning a real `datetime` object (not just `time.time()`) lets the
    composer derive both the ISO timestamp and the unix-epoch TTL from
    one consistent reading, so the two are always exactly aligned.
    """
    return datetime.now(timezone.utc)


def _to_decimal(value: Any) -> Decimal:
    """Float-safe Decimal coercion — never `Decimal(float)`."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _check_currency(item: dict, label: str) -> None:
    """Reject any item whose currency is not USD — including a MISSING
    currency field. Treating absent-as-USD would silently corrupt the
    FareHistory time series the moment a provider changed its response
    shape (security audit MED-2 / threat model boundary [3b])."""
    currency = item.get("currency")
    if currency != USD:
        raise ValueError(
            f"unsupported_currency: {label} returned {currency!r}, expected {USD}"
        )


def _select_cheapest(items: list[dict], price_key: str = "totalAmount", label: str = "offer") -> dict | None:
    """Pick the lowest-price item, with deterministic tiebreaking on `id`.

    Excludes zero-price entries (provider/test artefacts) and entries
    missing the price key. Returns None if no candidate qualifies."""
    candidates = [
        i for i in items
        if price_key in i and _to_decimal(i[price_key]) > 0
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda i: (_to_decimal(i[price_key]), i.get("id", "")))


def _flight_blob_fields(offer: dict) -> dict:
    """Pull airline / flightNumber / stops / depart / return from a Duffel-shaped offer.

    `stops` and the marketing carrier come from the OUTBOUND slice (slices[0]).
    The first segment carries the airline + flight number that show on the
    ticket — codeshares can have differing operating carriers in later
    segments, but the user-visible airline is the marketing one.
    """
    slices = offer.get("slices") or []
    if not slices:
        raise KeyError("flight offer has no slices")
    outbound = slices[0]
    segments = outbound.get("segments") or []
    if not segments:
        raise KeyError("flight offer outbound slice has no segments")
    first_seg = segments[0]
    return_dt = ""
    if len(slices) > 1 and (slices[1].get("segments") or []):
        return_dt = slices[1]["segments"][0].get("departAt", "")
    return {
        "airline": first_seg.get("airline", ""),
        "flightNumber": first_seg.get("flightNumber", ""),
        "stops": int(outbound.get("stops", 0)),
        "departDate": first_seg.get("departAt", ""),
        "returnDate": return_dt,
    }


def _validate_deep_link(raw: Any) -> str:
    """Reject deep links that aren't HTTPS or are unreasonably long.

    The Notifier (slice 7) will embed this string in alert emails. A
    `javascript:` URI or an unbounded string from a misbehaving provider
    must not survive the snapshot. Raises ValueError so the per-watch
    try/except logs `watch_errored` and skips this poll cleanly.
    """
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"unsupported_deep_link_type: {type(raw).__name__}")
    encoded_len = len(raw.encode("utf-8"))
    if encoded_len > MAX_DEEP_LINK_BYTES:
        raise ValueError(
            f"deep_link_too_large: {encoded_len} bytes > {MAX_DEEP_LINK_BYTES}"
        )
    if not raw.startswith(ALLOWED_DEEP_LINK_SCHEME):
        raise ValueError(f"deep_link_scheme_not_https: {raw[:32]}")
    return raw


def _hotel_blob_fields(hotel: dict) -> dict:
    return {
        "hotelName": hotel.get("hotelName", "") or "",
        "checkin": hotel.get("checkin", "") or "",
        "checkout": hotel.get("checkout", "") or "",
        "bookingDeepLink": _validate_deep_link(hotel.get("bookingDeepLink")),
    }


def compose_snapshot(
    watch: dict,
    flights_payload: dict,
    hotels_payload: dict,
) -> dict | None:
    """
    Build the FareHistory row for one poll of one watch.

    Returns None if either side has no qualifying offers — we don't write
    half-snapshots, and the `decide()` path (slice 5 T4) shouldn't see one.

    Raises:
        ValueError: any non-USD offer/hotel made it into the input list.
            The handler catches and logs `watch_errored`.
        KeyError: a malformed Duffel-shaped offer (missing slices/segments).
            Same handling.
    """
    flight_offers = flights_payload.get("offers") or []
    hotel_offers = hotels_payload.get("hotels") or []

    if not flight_offers or not hotel_offers:
        return None

    # Currency check applies to every candidate, not just the cheapest —
    # a non-USD entry could win the sort and silently mis-record the price.
    for o in flight_offers:
        _check_currency(o, "flight")
    for h in hotel_offers:
        _check_currency(h, "hotel")

    cheapest_flight = _select_cheapest(flight_offers, label="flight")
    cheapest_hotel = _select_cheapest(hotel_offers, label="hotel")

    if cheapest_flight is None or cheapest_hotel is None:
        return None

    flight_price = _to_decimal(cheapest_flight["totalAmount"])
    hotel_price = _to_decimal(cheapest_hotel["totalAmount"])
    total_price = flight_price + hotel_price

    now = _now()
    timestamp = now.isoformat()
    ttl = int(now.timestamp()) + TTL_DAYS * 86400

    return {
        "watchId": watch["watchId"],
        "timestamp": timestamp,
        "flightPrice": flight_price,
        "hotelPrice": hotel_price,
        "totalPrice": total_price,
        "bestOfferBlob": {
            **_flight_blob_fields(cheapest_flight),
            **_hotel_blob_fields(cheapest_hotel),
        },
        # slice 6+ replaces these with real provider request IDs once the
        # live client surfaces them. For slice 5 (fixture-default) the
        # `source` debug field is the most informative breadcrumb we have.
        "duffelRequestId": str(flights_payload.get("source", "")),
        "liteApiRequestId": str(hotels_payload.get("source", "")),
        "ttl": ttl,
    }
