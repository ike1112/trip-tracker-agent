"""
Lambda entrypoint for the trip-tracker poller.

Slice 5 progress (per `tasks/slice-5-poller.plan.md`):
  ✅ T1: walking skeleton — enumerate active watches and log each.
  🚧 T2: per-watch MCP calls (flights + hotels) with internal JWT auth.

Subsequent tasks add: FareHistory snapshot writes (T3), gates + decision
stub + CloudWatch metrics (T4), EventBridge enable + ADR + threat model +
e2e test (T5).

The handler runs sequentially per ADR 0003 — one watch at a time, one MCP
at a time. A failure on one watch logs and skips; the loop continues with
the next. This keeps a single misbehaving provider response from starving
all the other watches in the same poll.

Triggered by EventBridge (slice 5 T5) — the `event` payload is empty / a
schedule envelope, never user input. The poller does not parse user input
from `event` and never trusts it.
"""

import os

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

from enumerator import iter_active_watches
from jwt_signer import sign_for_user
from mcp_client import (
    McpCallError,
    call_flights,
    call_hotels,
    derive_dates,
)

logger = Logger(service="trip-tracker-poller")

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
    """Process one watch: sign a per-user JWT, hit both MCPs, log results.

    T2 stops at logging — no FareHistory write yet (T3) and no decision
    yet (T4). The MCP responses are deliberately not stored on the watch
    dict to avoid muddying the per-task contract.

    Raises:
        McpCallError: on any provider/transport failure. The caller catches
            and logs, per ADR 0003 (one bad watch never blocks the others).
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
        except McpCallError as e:
            errored += 1
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
                    "status": e.status,
                },
            )
            # Continue — ADR 0003.

    logger.info(
        "poll_complete",
        extra={"watches_polled": polled, "watches_errored": errored},
    )
    return {"watches_polled": polled, "watches_errored": errored}
