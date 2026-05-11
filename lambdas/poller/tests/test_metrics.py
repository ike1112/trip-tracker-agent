"""Tests for `metrics.py` — EMF emission via aws-lambda-powertools.

Powertools' `Metrics.serialize_metric_set()` returns the EMF dict the
Lambda runtime would write to stdout. We assert directly on that dict
so the tests don't depend on capturing stdout (which has the powertools
binding-at-import quirk we wrestled with in T1's handler tests).
"""

import importlib

import pytest


@pytest.fixture(autouse=True)
def _fresh_metrics_module():
    """Powertools' `Metrics` is a singleton. Reimport before each test so
    counters from a prior test don't leak in."""
    import sys
    sys.modules.pop("metrics", None)
    yield
    sys.modules.pop("metrics", None)


def _serialize(metrics_module):
    """Powertools serializer → EMF dict; clear state afterwards."""
    emf = metrics_module.metrics.serialize_metric_set()
    metrics_module.metrics.clear_metrics()
    return emf


def test_namespace_is_trip_tracker_poller():
    metrics = importlib.import_module("metrics")
    metrics.increment(metrics.WATCHES_POLLED)
    emf = _serialize(metrics)
    assert emf["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "TripTracker/Poller"


def test_emf_contains_all_four_metric_names():
    metrics = importlib.import_module("metrics")
    metrics.increment(metrics.WATCHES_POLLED)
    metrics.increment(metrics.WATCHES_ERRORED)
    metrics.increment(metrics.BEDROCK_DECISIONS_MADE)
    metrics.increment(metrics.ALERTS_SENT)

    emf = _serialize(metrics)
    cw_metrics = emf["_aws"]["CloudWatchMetrics"][0]["Metrics"]
    names = {m["Name"] for m in cw_metrics}
    assert names == {
        "watches_polled",
        "watches_errored",
        "bedrock_decisions_made",
        "alerts_sent",
    }


def test_watches_polled_count_reflects_increments():
    metrics = importlib.import_module("metrics")
    for _ in range(3):
        metrics.increment(metrics.WATCHES_POLLED)
    emf = _serialize(metrics)
    # Powertools may emit a list or a scalar depending on count; accept both.
    value = emf["watches_polled"]
    if isinstance(value, list):
        assert sum(value) == 3
    else:
        assert value == 3


def test_metrics_reset_between_invocations():
    """Sequential invocations in the same process must NOT carry state.
    The poller calls `flush_metrics()` (which serializes + clears) at the
    end of each Lambda run."""
    metrics = importlib.import_module("metrics")
    metrics.increment(metrics.WATCHES_POLLED)
    _serialize(metrics)  # flush

    # Second "invocation"
    metrics.increment(metrics.WATCHES_POLLED)
    emf = _serialize(metrics)
    value = emf["watches_polled"]
    assert (sum(value) if isinstance(value, list) else value) == 1


def test_alerts_sent_metric_omitted_when_zero():
    """Powertools doesn't emit a metric that wasn't incremented at all —
    confirms we don't pay for noise when no watches alert."""
    metrics = importlib.import_module("metrics")
    metrics.increment(metrics.WATCHES_POLLED)
    emf = _serialize(metrics)
    assert "alerts_sent" not in emf


def test_bedrock_decisions_made_increments_independently():
    metrics = importlib.import_module("metrics")
    metrics.increment(metrics.BEDROCK_DECISIONS_MADE)
    emf = _serialize(metrics)
    cw = emf["_aws"]["CloudWatchMetrics"][0]["Metrics"]
    names = {m["Name"] for m in cw}
    # Only the one we incremented appears.
    assert names == {"bedrock_decisions_made"}
