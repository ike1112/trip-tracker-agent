"""Integration tests for the T4 handler — full pipeline through decision.

Reuses the mock-MCP pattern from `test_handler_writes_history.py`. Each
test asserts on the metrics emitted by the handler (via powertools'
serialize_metric_set, since the handler calls `flush_metrics()` at the
end which clears state — we install a hook to capture the EMF before
flush).
"""

import json
import socket
import threading
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt as pyjwt

from tests.conftest import make_watch


SECRET = "test-secret-aaaaaaaaaaaaaaaaaaaaa"


def _ok(payload: dict) -> bytes:
    return json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
    }).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        try:
            pyjwt.decode(token, SECRET, algorithms=["HS256"])
        except Exception:
            self.send_response(401); self.end_headers(); return
        tool = body["params"]["name"]
        args = body["params"]["arguments"]
        status, payload = self.server.responder(tool, args)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_a, **_k): pass


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _serve(responder):
    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), _Handler)
    srv.responder = responder
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        srv.shutdown(); srv.server_close(); t.join(timeout=2)


def _flight_offer(args, total="1200.00"):
    return {
        "id": "off_1", "totalAmount": total, "currency": "USD",
        "slices": [
            {"stops": 0, "segments": [{
                "airline": "UA", "flightNumber": "100",
                "departAt": args["departDate"] + "T10:00:00",
            }]},
            {"stops": 0, "segments": [{
                "airline": "UA", "flightNumber": "101",
                "departAt": args["returnDate"] + "T17:00:00",
            }]},
        ],
    }


def _hotel_offer(args, total="300.00"):
    return {
        "id": "h_1", "totalAmount": total, "currency": "USD",
        "hotelName": "Test", "checkin": args["checkin"], "checkout": args["checkout"],
        "bookingDeepLink": "https://example.test/h_1",
    }


def _captured_emf(app_module):
    """Hook into powertools to capture EMF before the handler's flush
    clears it. Returns a list that test code populates by patching
    `metrics.flush_metrics`."""
    captured = []
    import metrics as metrics_module
    original_flush = metrics_module.metrics.flush_metrics

    def _capture():
        emf = metrics_module.metrics.serialize_metric_set()
        captured.append(emf)
        # Now actually flush (clears internal state).
        metrics_module.metrics.clear_metrics()

    metrics_module.metrics.flush_metrics = _capture
    return captured, original_flush, metrics_module


def _emf_value(emf: dict, name: str) -> int:
    """Read a metric's count, treating absent and zero-list as 0."""
    if name not in emf:
        return 0
    val = emf[name]
    if isinstance(val, list):
        return int(sum(val))
    return int(val)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_low_total_watch_increments_alerts_sent(app_module, monkeypatch):
    app, watches, _fare, log = app_module
    captured, original, mm = _captured_emf(app)

    # Total = 1200 + 300 = 1500; max=2000 → threshold passes → alert.
    f_resp = lambda _t, args: (200, _ok({"source": "fixture", "offers": [_flight_offer(args, total="1200.00")]}))
    h_resp = lambda _t, args: (200, _ok({"source": "fixture", "hotels": [_hotel_offer(args, total="300.00")]}))

    with _serve(f_resp) as fl, _serve(h_resp) as ht:
        monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", fl)
        monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  ht)

        watches.put_item(Item=make_watch("u1", "w-low", max_total_price=2000.0))
        app.handler({}, None)

    mm.metrics.flush_metrics = original
    assert len(captured) == 1
    emf = captured[0]
    assert _emf_value(emf, "watches_polled") == 1
    assert _emf_value(emf, "watches_errored") == 0
    assert _emf_value(emf, "bedrock_decisions_made") == 1
    assert _emf_value(emf, "alerts_sent") == 1

    decision_logs = [r for r in log.records if r.msg == "decision_made"]
    assert decision_logs and decision_logs[0].alert is True
    assert decision_logs[0].reason == "stub"


def test_high_total_watch_does_not_increment_alerts_sent(app_module, monkeypatch):
    app, watches, _fare, log = app_module
    captured, original, mm = _captured_emf(app)

    # Total 1500; max 1000 → threshold fails. No history → anomaly false.
    f_resp = lambda _t, args: (200, _ok({"source": "fixture", "offers": [_flight_offer(args, total="1200.00")]}))
    h_resp = lambda _t, args: (200, _ok({"source": "fixture", "hotels": [_hotel_offer(args, total="300.00")]}))

    with _serve(f_resp) as fl, _serve(h_resp) as ht:
        monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", fl)
        monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  ht)

        watches.put_item(Item=make_watch("u1", "w-high", max_total_price=1000.0))
        app.handler({}, None)

    mm.metrics.flush_metrics = original
    emf = captured[0]
    assert _emf_value(emf, "watches_polled") == 1
    assert _emf_value(emf, "bedrock_decisions_made") == 1  # we still asked
    assert _emf_value(emf, "alerts_sent") == 0             # …and got "no"

    decision_logs = [r for r in log.records if r.msg == "decision_made"]
    assert decision_logs[0].alert is False


def test_watch_with_empty_history_anomaly_returns_false_not_error(app_module, monkeypatch):
    """Pin the spec acceptance criterion: empty history doesn't crash."""
    app, watches, _fare, log = app_module
    captured, original, mm = _captured_emf(app)

    # Total above max so threshold doesn't carry; history empty for new watch.
    f_resp = lambda _t, args: (200, _ok({"source": "fixture", "offers": [_flight_offer(args, total="1500.00")]}))
    h_resp = lambda _t, args: (200, _ok({"source": "fixture", "hotels": [_hotel_offer(args, total="500.00")]}))

    with _serve(f_resp) as fl, _serve(h_resp) as ht:
        monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", fl)
        monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  ht)

        watches.put_item(Item=make_watch("u1", "w-new", max_total_price=1000.0))
        # Should not raise — anomaly gate handles empty list.
        app.handler({}, None)

    mm.metrics.flush_metrics = original
    emf = captured[0]
    assert _emf_value(emf, "watches_errored") == 0
    assert _emf_value(emf, "alerts_sent") == 0


def test_dedup_blocks_alert_for_recently_alerted_price(app_module, monkeypatch):
    """At ≥0.95 × lastAlertedPrice, dedup gates blocks even if threshold passes."""
    app, watches, _fare, log = app_module
    captured, original, mm = _captured_emf(app)

    # New total = 1100; lastAlertedPrice = 1100 → 0.95×1100 = 1045 → 1100 > 1045 → dedup blocks.
    f_resp = lambda _t, args: (200, _ok({"source": "fixture", "offers": [_flight_offer(args, total="900.00")]}))
    h_resp = lambda _t, args: (200, _ok({"source": "fixture", "hotels": [_hotel_offer(args, total="200.00")]}))

    with _serve(f_resp) as fl, _serve(h_resp) as ht:
        monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", fl)
        monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  ht)

        watches.put_item(Item=make_watch(
            "u1", "w-dedup", max_total_price=2000.0,
            last_alerted_price=1100.0, last_alerted_at="2026-05-01T00:00:00+00:00",
        ))
        app.handler({}, None)

    mm.metrics.flush_metrics = original
    emf = captured[0]
    assert _emf_value(emf, "alerts_sent") == 0
    decisions = [r for r in log.records if r.msg == "decision_made"]
    assert decisions[0].alert is False
    assert decisions[0].reason == "dedup_blocked"


def test_watches_polled_increments_once_per_active_watch(app_module, monkeypatch):
    app, watches, _fare, _log = app_module
    captured, original, mm = _captured_emf(app)

    f_resp = lambda _t, args: (200, _ok({"source": "fixture", "offers": [_flight_offer(args)]}))
    h_resp = lambda _t, args: (200, _ok({"source": "fixture", "hotels": [_hotel_offer(args)]}))

    with _serve(f_resp) as fl, _serve(h_resp) as ht:
        monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", fl)
        monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  ht)

        watches.put_item(Item=make_watch("u1", "w1", destination="Tokyo"))
        watches.put_item(Item=make_watch("u2", "w2", destination="Paris"))
        watches.put_item(Item=make_watch("u3", "w3", destination="Rome"))
        watches.put_item(Item=make_watch("u4", "w-paused", status="paused"))

        app.handler({}, None)

    mm.metrics.flush_metrics = original
    emf = captured[0]
    assert _emf_value(emf, "watches_polled") == 3
    assert _emf_value(emf, "watches_errored") == 0


def test_watches_errored_increments_when_mcp_returns_500(app_module, monkeypatch):
    app, watches, _fare, _log = app_module
    captured, original, mm = _captured_emf(app)

    f_resp = lambda _t, _args: (500, b'{"error":"upstream"}')
    h_resp = lambda _t, args:  (200, _ok({"source": "fixture", "hotels": [_hotel_offer(args)]}))

    with _serve(f_resp) as fl, _serve(h_resp) as ht:
        monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", fl)
        monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  ht)

        watches.put_item(Item=make_watch("u1", "w1"))
        app.handler({}, None)

    mm.metrics.flush_metrics = original
    emf = captured[0]
    assert _emf_value(emf, "watches_polled") == 1
    assert _emf_value(emf, "watches_errored") == 1
    assert _emf_value(emf, "bedrock_decisions_made") == 0
    assert _emf_value(emf, "alerts_sent") == 0
