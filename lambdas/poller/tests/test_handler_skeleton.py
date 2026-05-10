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


def test_handler_logs_one_event_per_active_watch(app_module):
    app, watches, _, log = app_module
    watches.put_item(Item=make_watch("user-aaaa1111", "w1", destination="Tokyo"))
    watches.put_item(Item=make_watch("user-bbbb2222", "w2", destination="Paris"))
    watches.put_item(Item=make_watch("user-cccc3333", "w3", status="paused"))
    watches.put_item(Item=make_watch("user-dddd4444", "w4", status="archived"))

    result = app.handler({}, None)

    polled = _events(log.records, "watch_polled")

    assert {r.watch_id for r in polled} == {"w1", "w2"}
    assert result == {"watches_polled": 2}


def test_handler_log_record_shape(app_module):
    app, watches, _, log = app_module
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


def test_handler_emits_poll_complete_with_count(app_module):
    app, watches, _, log = app_module
    for i in range(5):
        watches.put_item(Item=make_watch(f"u{i}", f"w{i}"))

    app.handler({}, None)

    completes = _events(log.records, "poll_complete")
    assert len(completes) == 1
    assert completes[0].watches_polled == 5


def test_handler_returns_zero_for_no_active_watches(app_module):
    app, watches, _, log = app_module
    watches.put_item(Item=make_watch("u1", "w-paused", status="paused"))

    result = app.handler({}, None)

    polled = _events(log.records, "watch_polled")
    assert polled == []
    assert result == {"watches_polled": 0}
