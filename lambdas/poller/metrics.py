"""
CloudWatch metric emission for the poller, via aws-lambda-powertools EMF.

Four metrics named in the production-readiness companion §3.5:

  * `watches_polled`        — incremented once per watch attempt.
  * `watches_errored`       — once per watch that raised an MCP/snapshot
                              error and was skipped.
  * `bedrock_decisions_made`— once per call to `decision.decide` (= once per
                              watch that produced a written snapshot).
  * `alerts_sent`           — once per `decide()` returning `alert=True`.

Powertools' `Metrics` writes EMF JSON to the log stream; CloudWatch parses
it server-side. No extra IAM, no extra API call, no extra cost vs raw
`PutMetricData`. The `flush_metrics()` decorator is NOT used here — the
handler in app.py owns the flush so failure paths still get partial
metrics rather than nothing.
"""

from __future__ import annotations

from aws_lambda_powertools import Metrics
from aws_lambda_powertools.metrics import MetricUnit

NAMESPACE = "TripTracker/Poller"
SERVICE = "trip-tracker-poller"

WATCHES_POLLED = "watches_polled"
WATCHES_ERRORED = "watches_errored"
BEDROCK_DECISIONS_MADE = "bedrock_decisions_made"
ALERTS_SENT = "alerts_sent"

metrics = Metrics(namespace=NAMESPACE, service=SERVICE)


def increment(name: str, value: int = 1) -> None:
    """Bump a count metric by `value` (default 1)."""
    metrics.add_metric(name=name, unit=MetricUnit.Count, value=value)
