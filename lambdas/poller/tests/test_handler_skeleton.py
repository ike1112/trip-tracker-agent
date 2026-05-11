"""Tests for `app.handler` Task 1 surface.

At T1 the handler only enumerates active watches and emits one structured
log per row, plus a `poll_complete` summary. The contract these tests pin
down:
  - One `watch_polled` log per active watch with `watch_id`,
    `user_id_prefix`, `destination` fields.
  - A trailing `poll_complete` log with the count.
  - The returned dict contains `watches_polled = <count>`.
  - Inactive watches are NOT logged (filter must be enforced at enumerator,
    not in the handler).

Captured via the `MemoryLogHandler` attached in conftest — see its docstring
for why neither `capsys`/`capfd` nor `caplog` work reliably with powertools.
"""

import logging

from tests.conftest import make_watch


def _events(records: list[logging.LogRecord], name: str) -> list[logging.LogRecord]:
    # `record.msg` is the literal first arg passed to logger.info(); the
    # `.message` attribute only exists after the record is formatted.
    return [r for r in records if r.msg == name]


def test_handler_logs_one_event_per_active_watch(app_module, monkeypatch):
    """T1 contract — verifies the enumerator's filter survives at the
    handler boundary. We point the handler at a dead MCP endpoint so each
    watch errors after the watch_polled log; that gives us the count
    without standing up a mock MCP server (which has its own coverage in
    test_handler_with_mcp.py).
    """
    app, watches, _, log = app_module
    monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", "http://127.0.0.1:1/dead")
    monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  "http://127.0.0.1:1/dead")

    watches.put_item(Item=make_watch("user-aaaa1111", "w1", destination="Tokyo"))
    watches.put_item(Item=make_watch("user-bbbb2222", "w2", destination="Paris"))
    watches.put_item(Item=make_watch("user-cccc3333", "w3", status="paused"))
    watches.put_item(Item=make_watch("user-dddd4444", "w4", status="archived"))

    result = app.handler({}, None)

    polled = _events(log.records, "watch_polled")

    assert {r.watch_id for r in polled} == {"w1", "w2"}
    assert result["watches_polled"] == 2
    # All polled watches errored on the dead endpoint — proves the per-watch
    # try/except keeps the loop going (ADR 0003).
    assert result["watches_errored"] == 2


def test_handler_log_record_shape(app_module, monkeypatch):
    app, watches, _, log = app_module
    monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", "http://127.0.0.1:1/dead")
    monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  "http://127.0.0.1:1/dead")
    # 16-char user_id; first 8 chars are unambiguously "cognito-".
    watches.put_item(Item=make_watch(
        "cognito-XXXXXXXX", "w-shape", destination="Rome",
    ))

    app.handler({}, None)

    polled = _events(log.records, "watch_polled")
    assert len(polled) == 1
    rec = polled[0]
    # Field assertions — pin down exact contract so dashboards / CW Logs
    # Insights queries can rely on names. Powertools hoists `extra={...}`
    # entries onto the LogRecord as attributes.
    assert rec.watch_id == "w-shape"
    assert rec.user_id_prefix == "cognito-"  # first 8 chars only
    assert rec.destination == "Rome"
    assert rec.levelname == "INFO"


def test_handler_emits_poll_complete_with_count(app_module, monkeypatch):
    app, watches, _, log = app_module
    monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", "http://127.0.0.1:1/dead")
    monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  "http://127.0.0.1:1/dead")
    for i in range(5):
        watches.put_item(Item=make_watch(f"u{i}", f"w{i}"))

    app.handler({}, None)

    completes = _events(log.records, "poll_complete")
    assert len(completes) == 1
    assert completes[0].watches_polled == 5
    assert completes[0].watches_errored == 5


def test_anomaly_window_days_constant_pins_to_30(app_module):
    """Spec §5 says the anomaly gate looks at the 30-day median + 30-day
    low. The constant lives in `app.py` because it's a pipeline-level
    knob (not a gate-internal one); pin it here so a silent edit fails."""
    app, _watches, _fare, _log = app_module
    assert app.ANOMALY_WINDOW_DAYS == 30


def test_handler_returns_zero_for_no_active_watches(app_module, monkeypatch):
    app, watches, _, log = app_module
    monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", "http://127.0.0.1:1/dead")
    monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  "http://127.0.0.1:1/dead")
    watches.put_item(Item=make_watch("u1", "w-paused", status="paused"))

    result = app.handler({}, None)

    polled = _events(log.records, "watch_polled")
    assert polled == []
    assert result == {"watches_polled": 0, "watches_errored": 0}
