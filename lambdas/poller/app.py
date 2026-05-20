"""
Lambda entrypoint for the trip-tracker poller.

Walks every active watch sequentially per ADR 0003: enumerate the
Watches table, call flights-mcp + hotels-mcp under a per-user JWT,
compose and persist a FareHistory snapshot, then run the alert gates
and (when warranted) the Bedrock decision. A failure on one watch logs
and continues so a single misbehaving provider response can't starve
the rest of the poll.

Triggered by EventBridge on a cron — the `event` payload is a schedule
envelope, never user input. The poller does not parse anything out of
`event` and never trusts it.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

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
        McpCallError: on any MCP provider/transport failure.
        ValueError:   snapshot composer found a non-USD currency or
                      rejected an unsafe deep link.
        KeyError:     malformed Duffel-shaped offer (missing slices/segments).
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
    # Flight search takes IATA, hotel search takes the city name. They
    # are stored as distinct fields on the watch because the poller has
    # no LLM in the loop to resolve city → airport at search time; a
    # legacy watch missing destinationAirport fails loud (KeyError) and
    # gets logged as watch_errored.
    flights_payload = call_flights(
        FLIGHTS_MCP_ENDPOINT,
        token,
        origin=watch["origin"],
        destination=watch["destinationAirport"],
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

    # Compose + write the FareHistory snapshot. compose_snapshot()
    # returns None when either side has no qualifying offers — log it as
    # a soft skip and stop here (no decision to make).
    snapshot = compose_snapshot(watch, flights_payload, hotels_payload)
    if snapshot is None:
        logger.info(
            "snapshot_skipped",
            extra={**log_extra, "reason": "no_qualifying_offers"},
        )
        return
    # Pull the 30-day anomaly window BEFORE writing the new row. With
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
    # increment when the gate cascade reached the model layer (i.e.,
    # passed the dedup gate AND at least one of threshold/anomaly). The
    # `bedrock_called` flag is set by `decision.decide` regardless of
    # stub vs live mode so this metric stays accurate either way.
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
        _async_invoke_notifier(snapshot, watch, decision, log_extra)


def _decimal_to_str(value):
    """Recursive Decimal -> str coercion for JSON-payload safety. The
    notifier expects numeric snapshot fields as strings (matching the
    on-the-wire shape its loader / template handle); boto3's invoke
    payload also can't serialise raw Decimals."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _decimal_to_str(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decimal_to_str(v) for v in value]
    return value


_lambda_client = None


def _get_lambda_client():
    """Lazy boto3 lambda client. Constructed on first use so the cold-
    start cost is paid once per Lambda container, not per poll."""
    global _lambda_client
    if _lambda_client is None:
        import boto3
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def _async_invoke_notifier(snapshot: dict, watch: dict, decision: dict, log_extra: dict) -> None:
    """Fire-and-forget invoke of the notifier Lambda.

    Async (`InvocationType=Event`) so a slow SES send doesn't extend
    poll-cycle wall time. Failures during the local invoke are logged
    but never raised — the alert is lost for this cycle, but the
    next poll's dedup gate gives us a natural retry surface.

    If `NOTIFIER_FUNCTION_NAME` is unset (e.g. local invocation, an
    older stack, or a manual debugging run), log a WARNING and skip.
    """
    fn_name = os.environ.get("NOTIFIER_FUNCTION_NAME", "").strip()
    if not fn_name:
        logger.warning(
            "notifier_function_name_missing",
            extra=log_extra,
        )
        return

    try:
        # Serialise inside the try so a future Decimal-laden field
        # that `_decimal_to_str` misses (raising TypeError in
        # json.dumps) is caught with the same containment guarantee
        # as a boto3 invoke failure — the poll loop is never killed
        # by a serialisation bug.
        payload = {
            "snapshot": _decimal_to_str(snapshot),
            "watch": _decimal_to_str(watch),
            "decision": decision,
        }
        payload_bytes = json.dumps(payload).encode("utf-8")
        _get_lambda_client().invoke(
            FunctionName=fn_name,
            InvocationType="Event",
            Payload=payload_bytes,
        )
    except Exception as e:
        # Log only the exception class name. A raw ClientError from
        # lambda.invoke can include the function ARN; downstream
        # error classes might include richer payloads. Class name
        # is sufficient for triage and removes the latent leak.
        logger.warning(
            "notifier_invoke_failed",
            extra={**log_extra, "error": type(e).__name__},
        )


def handler(event: dict, context: LambdaContext) -> dict:
    """
    Per-invocation entrypoint. Walks every active watch sequentially.

    Returns a small status dict for CloudWatch / manual invocation; the
    real "did anything happen" signal is the structured logs plus the
    CloudWatch EMF metrics flushed at the end of the poll.
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
            # McpCallError: transport / HTTP / envelope failures.
            # ValueError:   snapshot composer rejects non-USD currency or
            #               unsafe deep link.
            # KeyError:     malformed offer (missing slices/segments).
            # All of these are "this watch couldn't be processed; skip
            # and continue with the next" per ADR 0003.
            errored += 1
            metrics.increment(metrics.WATCHES_ERRORED)
            # Deliberately do NOT log `e.body` — it may contain reflected
            # request fragments (incl. our own JWT parse errors). Surface
            # only the categorised reason + HTTP status.
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
