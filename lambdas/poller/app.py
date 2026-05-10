"""
Lambda entrypoint for the trip-tracker poller.

Slice 5 scope (per `tasks/slice-5-poller.plan.md`):
  Task 1: walking skeleton — enumerate active watches and log each.

Subsequent tasks add: MCP calls (T2), FareHistory snapshot writes (T3),
gates + decision stub + CloudWatch metrics (T4), EventBridge enable + ADR
+ threat-model entry + e2e test (T5).

The handler is intentionally thin — the per-watch loop, gates, and decision
will live in their own modules so each can be tested in isolation. ADR 0003
(planned in T5) documents why this loop is sequential, not parallel.

Triggered by EventBridge (slice 5 T5) — the `event` payload is empty / a
schedule envelope, never user input. The poller does not parse user input
from `event` and never trusts it.
"""

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

from enumerator import iter_active_watches

logger = Logger(service="trip-tracker-poller")


def handler(event: dict, context: LambdaContext) -> dict:
    """
    Per-invocation entrypoint. Walks every active watch sequentially.

    Returns a small status dict for CloudWatch / manual invocation; the
    real "did anything happen" signal is the structured logs + (T4) the
    CloudWatch EMF metrics.
    """
    polled = 0
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
    logger.info("poll_complete", extra={"watches_polled": polled})
    return {"watches_polled": polled}
