"""End-to-end snapshot-write test — handler walks active watches, calls
mock MCPs, writes FareHistory rows.

Builds on `test_handler_with_mcp.py`'s mock servers but additionally
asserts on the FareHistory table state after the handler returns.
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _flights_responder(_tool, args):
    """Vary the price by IATA so we can pin specific FareHistory rows.
    Flight calls receive the IATA airport code; hotels receive the city."""
    base = {"NRT": "1148.00", "CDG": "850.00", "FCO": "1320.50"}
    total = base.get(args["destination"], "999.00")
    return 200, _ok({"source": "fixture", "offers": [
        {
            "id": f"off_{args['destination']}",
            "totalAmount": total,
            "currency": "USD",
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
        },
    ]})


def _hotels_responder(_tool, args):
    base = {"Tokyo": "485.00", "Paris": "320.00", "Rome": "410.75"}
    total = base.get(args["city"], "555.00")
    return 200, _ok({"source": "fixture", "hotels": [
        {
            "id": f"h_{args['city']}", "totalAmount": total, "currency": "USD",
            "hotelName": f"{args['city']} Test Hotel",
            "checkin": args["checkin"], "checkout": args["checkout"],
            "bookingDeepLink": f"https://example.test/{args['city']}",
        },
    ]})


def test_three_active_watches_produce_three_fare_history_rows(app_module, monkeypatch):
    app, watches, fare, log = app_module

    with _serve(_flights_responder) as fl_url, \
         _serve(_hotels_responder)  as ht_url:

        monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", fl_url)
        monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  ht_url)

        watches.put_item(Item=make_watch("u1", "w-tokyo", destination="Tokyo"))
        watches.put_item(Item=make_watch("u2", "w-paris", destination="Paris"))
        watches.put_item(Item=make_watch("u3", "w-rome",  destination="Rome"))
        # Inactive — should not produce a FareHistory row.
        watches.put_item(Item=make_watch("u4", "w-paused", status="paused"))

        result = app.handler({}, None)

    assert result == {"watches_polled": 3, "watches_errored": 0}

    items = fare.scan().get("Items", [])
    rows_by_watch = {it["watchId"]: it for it in items}
    assert set(rows_by_watch.keys()) == {"w-tokyo", "w-paris", "w-rome"}

    # Exact totals from the responders.
    assert rows_by_watch["w-tokyo"]["totalPrice"] == Decimal("1148.00") + Decimal("485.00")
    assert rows_by_watch["w-paris"]["totalPrice"] == Decimal("850.00") + Decimal("320.00")
    assert rows_by_watch["w-rome"]["totalPrice"] == Decimal("1320.50") + Decimal("410.75")

    # Snapshot logs emitted with the expected event names.
    snap_logs = [r for r in log.records if r.msg == "snapshot_written"]
    assert len(snap_logs) == 3


def test_watch_with_empty_flight_response_writes_no_history(app_module, monkeypatch):
    """compose_snapshot returns None on empty offers → no row + no error
    (this is a soft skip, not a `watch_errored`). FareHistory stays empty
    for the affected watch; the others still get rows."""
    app, watches, fare, log = app_module

    def flights(_tool, args):
        if args["destination"] == "CDG":  # Paris IATA
            return 200, _ok({"source": "fixture-miss", "offers": []})
        return _flights_responder(_tool, args)

    with _serve(flights) as fl_url, _serve(_hotels_responder) as ht_url:
        monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", fl_url)
        monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  ht_url)

        watches.put_item(Item=make_watch("u1", "w-tokyo", destination="Tokyo"))
        watches.put_item(Item=make_watch("u2", "w-paris", destination="Paris"))
        watches.put_item(Item=make_watch("u3", "w-rome",  destination="Rome"))

        result = app.handler({}, None)

    # Soft skip — Paris counted as polled but not as errored.
    assert result == {"watches_polled": 3, "watches_errored": 0}

    # Only Tokyo + Rome have FareHistory rows.
    written = {it["watchId"] for it in fare.scan().get("Items", [])}
    assert written == {"w-tokyo", "w-rome"}

    # The Paris poll emitted snapshot_skipped (not snapshot_written, not watch_errored).
    skipped = [r for r in log.records if r.msg == "snapshot_skipped"]
    assert len(skipped) == 1
    assert skipped[0].watch_id == "w-paris"


def test_non_usd_flight_offer_skips_watch_as_errored(app_module, monkeypatch):
    """Currency-mismatch ValueError surfaces as `watch_errored` — the
    watch is skipped (no FareHistory row) and the rest of the loop continues."""
    app, watches, fare, log = app_module

    def flights(_tool, args):
        if args["destination"] == "NRT":  # Tokyo IATA
            return 200, _ok({"source": "live", "offers": [
                {"id": "off_gbp", "totalAmount": "900.00", "currency": "GBP",
                 "slices": [{"stops": 0, "segments": [{
                     "airline": "BA", "flightNumber": "5",
                     "departAt": args["departDate"] + "T10:00:00"}]}]},
            ]})
        return _flights_responder(_tool, args)

    with _serve(flights) as fl_url, _serve(_hotels_responder) as ht_url:
        monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", fl_url)
        monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  ht_url)

        watches.put_item(Item=make_watch("u1", "w-tokyo", destination="Tokyo"))
        watches.put_item(Item=make_watch("u2", "w-paris", destination="Paris"))

        result = app.handler({}, None)

    # Tokyo errored on currency mismatch; Paris fine.
    assert result == {"watches_polled": 2, "watches_errored": 1}
    written = {it["watchId"] for it in fare.scan().get("Items", [])}
    assert written == {"w-paris"}

    errored = [r for r in log.records if r.msg == "watch_errored"]
    assert len(errored) == 1
    assert "GBP" in errored[0].reason
