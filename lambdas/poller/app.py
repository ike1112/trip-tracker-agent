"""
Lambda entrypoint for the trip-tracker poller.

Slice 5 progress (per `tasks/slice-5-poller.plan.md`):
  ✅ T1: walking skeleton — enumerate active watches and log each.
  ✅ T2: per-watch MCP calls (flights + hotels) with internal JWT auth.
  ✅ T3: snapshot composer + FareHistory writer per watch.
  ✅ T4: gates + decision stub + CloudWatch metrics.
  ⏭ T5: EventBridge enable + ADR 0003 + threat model + e2e.

The handler runs sequentially per ADR 0003 — one watch at a time, one MCP
at a time. A failure on one watch logs and skips; the loop continues with
the next. This keeps a single misbehaving provider response from starving
all the other watches in the same poll.

Triggered by EventBridge (slice 5 T5) — the `event` payload is empty / a
schedule envelope, never user input. The poller does not parse user input
from `event` and never trusts it.
"""

import os
from datetime import datetime, timedelta, timezone

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

import metrics
from decision import decide
from enumerator import iter_active_watches
from history_window import get_window
from jwt_signer import sign_for_user
from mcp_client import (
    McpCallError,
    call_flights,
    call_hotels,
    derive_dates,
)
from snapshot import compose_snapshot
from writer import write_snapshot

logger = Logger(service="trip-tracker-poller")

# 30-day window for the anomaly gate (design-spec §5).
ANOMALY_WINDOW_DAYS = 30

FLIGHTS_MCP_ENDPOINT = os.environ.get("FLIGHTS_MCP_ENDPOINT")
HOTELS_MCP_ENDPOINT = os.environ.get("HOTELS_MCP_ENDPOINT")


def _require_endpoints() -> None:
    """Fail loud if the MCP endpoints aren't configured.

    Without this, the first MCP call would pass `None` to `urllib.Request`
    and raise `TypeError` mid-loop — uncaught by `except McpCallError`,
    crashing the whole poll. Same defence as `jwt_signer.py`.
    """
    missing = [
        name for name, value in (
            ("FLIGHTS_MCP_ENDPOINT", FLIGHTS_MCP_ENDPOINT),
            ("HOTELS_MCP_ENDPOINT", HOTELS_MCP_ENDPOINT),
        )
        if not value
    ]
    if missing:
        raise EnvironmentError(f"Required env vars not set: {', '.join(missing)}")


def _poll_one(watch: dict) -> None:
    """Process one watch: sign per-user JWT, call both MCPs, compose +
    write a FareHistory snapshot.

    Returns early (without raising) when compose_snapshot determines the
    poll has no qualifying offers — that's a soft skip logged as
    `snapshot_skipped`, not a `watch_errored`.

    Raises:
        McpCallError: on any MCP provider/transport failure (T2).
        ValueError: snapshot composer found a non-USD currency (T3).
        KeyError:    malformed Duffel-shaped offer (missing slices/segments).
        The caller catches all three and logs `watch_errored`, per ADR 0003.
    """
    user_id = watch["userId"]
    watch_id = watch["watchId"]
    log_extra = {"watch_id": watch_id, "user_id_prefix": user_id[:8]}

    token = sign_for_user(user_id)
    depart, ret = derive_dates(watch["dateWindow"])
    prefs = watch.get("preferences") or {}
    # DDB returns numeric attrs as Decimal; coerce to int before JSON-RPC
    # serialisation (json.dumps doesn't know how to encode Decimal).
    max_stops = int(prefs["maxStops"]) if "maxStops" in prefs else None
    min_stars = int(prefs["hotelMinStars"]) if "hotelMinStars" in prefs else None
    pax = int(watch["pax"])

    # Origin may be a string or a list (e.g. ["SFO","OAK","SJC"]) per design
    # spec §3 — pass through unchanged; the flights tool's zod schema accepts
    # both shapes.
    flights_payload = call_flights(
        FLIGHTS_MCP_ENDPOINT,
        token,
        origin=watch["origin"],
        destination=watch["destination"],
        depart_date=depart,
        return_date=ret,
        pax=pax,
        max_stops=max_stops,
    )
    logger.info(
        "flights_searched",
        extra={
            **log_extra,
            "offer_count": len(flights_payload.get("offers", [])),
            "source": flights_payload.get("source"),
        },
    )

    hotels_payload = call_hotels(
        HOTELS_MCP_ENDPOINT,
        token,
        city=watch["destination"],
        checkin=depart,
        checkout=ret,
        pax=pax,
        min_stars=min_stars,
    )
    logger.info(
        "hotels_searched",
        extra={
            **log_extra,
            "hotel_count": len(hotels_payload.get("hotels", [])),
            "source": hotels_payload.get("source"),
        },
    )

    # T3: compose + write the FareHistory snapshot. compose_snapshot()
    # returns None when either side has no qualifying offers — log it as
    # a soft skip and stop here (no decision to make).
    snapshot = compose_snapshot(watch, flights_payload, hotels_payload)
    if snapshot is None:
        logger.info(
            "snapshot_skipped",
            extra={**log_extra, "reason": "no_qualifying_offers"},
        )
        return
    # T4: pull the 30-day anomaly window BEFORE writing the new row. With
    # `cutoff = now - 30d` and the just-composed snapshot's timestamp =
    # `now`, the new row's timestamp is strictly > cutoff. So when the
    # subsequent query runs `timestamp > cutoff`, the boundary is "rows
    # written between cutoff and now", and the just-written row is the
    # only thing AT `now` — naturally excluded because we query before
    # writing. No fragile equality filter needed.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ANOMALY_WINDOW_DAYS)).isoformat()
    history = get_window(watch_id, cutoff)

    write_snapshot(snapshot)
    logger.info(
        "snapshot_written",
        extra={
            **log_extra,
            "flight_price": str(snapshot["flightPrice"]),
            "hotel_price":  str(snapshot["hotelPrice"]),
            "total_price":  str(snapshot["totalPrice"]),
        },
    )

    decision = decide(snapshot, watch, history)
    # `bedrock_decisions_made` reflects actual model invocations — only
    # increment when the gate cascade would have called Bedrock (i.e.,
    # passed the dedup gate AND at least one of threshold/anomaly).
    # Slice 6's real Bedrock call sits behind the same flag, so the
    # metric semantics stay accurate when the stub is replaced.
    if decision.get("bedrock_called"):
        metrics.increment(metrics.BEDROCK_DECISIONS_MADE)
    logger.info(
        "decision_made",
        extra={
            **log_extra,
            "alert": decision["alert"],
            "reason": decision["reason"],
            "history_size": len(history),
        },
    )
    if decision["alert"]:
        metrics.increment(metrics.ALERTS_SENT)


def handler(event: dict, context: LambdaContext) -> dict:
    """
    Per-invocation entrypoint. Walks every active watch sequentially.

    Returns a small status dict for CloudWatch / manual invocation; the
    real "did anything happen" signal is the structured logs + (T4) the
    CloudWatch EMF metrics.
    """
    _require_endpoints()
    polled = 0
    errored = 0
    for watch in iter_active_watches():
        polled += 1
        metrics.increment(metrics.WATCHES_POLLED)
        logger.info(
            "watch_polled",
            extra={
                "watch_id": watch["watchId"],
                "user_id_prefix": watch["userId"][:8],
                "destination": watch["destination"],
            },
        )
        try:
            _poll_one(watch)
        except (McpCallError, ValueError, KeyError) as e:
            # McpCallError: transport / HTTP / envelope failures (T2).
            # ValueError: snapshot composer rejects non-USD currency or
            #             unsafe deep link (T3).
            # KeyError:   malformed offer (missing slices/segments) (T3).
            # All of these are "this watch couldn't be processed; skip
            # and continue with the next" per ADR 0003.
            errored += 1
            metrics.increment(metrics.WATCHES_ERRORED)
            # Deliberately do NOT log `e.body` — it may contain reflected
            # request fragments (incl. our own JWT parse errors). Surface
            # only the categorised reason + HTTP status. See security audit
            # LOW-1.
            logger.warning(
                "watch_errored",
                extra={
                    "watch_id": watch["watchId"],
                    "user_id_prefix": watch["userId"][:8],
                    "reason": str(e),
                    "status": getattr(e, "status", None),
                },
            )
            # Continue — ADR 0003.

    logger.info(
        "poll_complete",
        extra={"watches_polled": polled, "watches_errored": errored},
    )
    # Flush all four metrics in one shot. Powertools writes the EMF blob
    # to stdout; CloudWatch parses it server-side. Done after the loop
    # so a partial poll still gets the metrics that fired before failure.
    metrics.metrics.flush_metrics()
    return {"watches_polled": polled, "watches_errored": errored}
