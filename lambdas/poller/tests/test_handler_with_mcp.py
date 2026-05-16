"""Integration test for per-watch MCP wiring — the handler walks active
watches and issues real HTTP calls to mock MCP servers.

The mock servers run on threads in-process; one for "flights", one for
"hotels". Each one verifies the JWT in the Authorization header (the
poller signs with its own secret and `sub == "trip-tracker-poller"`,
the same coupling the real `mcp-authorizer/index.js` enforces — ADR
0006) and returns canned MCP envelopes.

Asserts that pin down the MCP-call behaviour:
  - 3 active watches → 6 successful MCP calls (3 × 2).
  - The Authorization header on every request is a Bearer JWT signed by
    the poller's `jwt_signer` (verified by decoding it on the mock side).
  - Each request body's `params.arguments` carry the exact MCP-tool
    argument shape derived from the watch (origin/destination/dates/pax,
    plus preference-derived `maxStops` / `minStars` when set).
  - Per-watch handler logs include `flights_searched` and `hotels_searched`
    with offer/hotel counts and a `source` field.
  - Per-watch failure (one MCP returns 500) is *isolated* — the other two
    watches still complete and the loop logs `watch_errored` for the
    failing one.
  - Inactive (paused/archived) watches are not polled at all.
"""

import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt as pyjwt

from tests.conftest import make_watch


SECRET = "test-secret-aaaaaaaaaaaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# Mock MCP server — verifies JWT, records every call, returns configurable
# response per (path, request body).
# ---------------------------------------------------------------------------

class _MockMcpHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        body = json.loads(raw_body)
        auth = self.headers.get("Authorization", "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else None
        try:
            claims = pyjwt.decode(token, SECRET, algorithms=["HS256"])
        except Exception as e:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return
        # Mirror `lambdas/mcp-authorizer/index.js` — the poller mints
        # sub=trip-tracker-poller (ADR 0006); only that passes. Any other
        # principal gets denied. The poller test still records the call
        # (so the test can count attempts) but the response is a 401 so
        # the poller treats it as `watch_errored`.
        if claims.get("sub") != "trip-tracker-poller":
            record = {
                "path": self.path, "tool": body.get("params", {}).get("name"),
                "arguments": body.get("params", {}).get("arguments"),
                "user_id": claims.get("user_id"), "sub": claims.get("sub"),
            }
            self.server.calls.append(record)
            self.send_response(401)
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"sub={claims.get('sub')}"}).encode("utf-8"))
            return

        record = {
            "path": self.path,
            "tool": body.get("params", {}).get("name"),
            "arguments": body.get("params", {}).get("arguments"),
            "user_id": claims.get("user_id"),
            "sub": claims.get("sub"),
        }
        self.server.calls.append(record)

        responder = self.server.responders.get(self.server.calls[-1]["tool"])
        status, payload = responder(record) if responder else (200, _ok_payload({"source": "fixture", "offers": [], "hotels": []}))

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload if isinstance(payload, bytes) else payload.encode("utf-8"))

    def log_message(self, *args, **kwargs):
        pass  # silence noisy stderr per-request


def _ok_payload(payload: dict) -> bytes:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {"type": "text", "text": json.dumps(payload)},
            ],
        },
    }).encode("utf-8")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _mock_mcp(responders: dict):
    """Run one mock MCP server. `responders` maps tool-name → fn(record) → (status, body)."""
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _MockMcpHandler)
    server.calls = []
    server.responders = responders
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{port}/mcp"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _events(records, name):
    return [r for r in records if r.msg == name]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_handler_calls_both_mcps_for_each_active_watch(app_module, monkeypatch):
    app, watches, _, log = app_module

    # Offers carry the full slice/segments shape compose_snapshot needs;
    # otherwise the writer raises KeyError → watch_errored.
    def _flight_offer():
        return {
            "id": "off_1", "totalAmount": "1284.50", "currency": "USD",
            "slices": [
                {"stops": 0, "segments": [{
                    "airline": "UA", "flightNumber": "100",
                    "departAt": "2026-10-15T10:00:00",
                }]},
                {"stops": 0, "segments": [{
                    "airline": "UA", "flightNumber": "101",
                    "departAt": "2026-10-20T17:00:00",
                }]},
            ],
        }

    def _hotel_offer():
        return {
            "id": "h_1", "totalAmount": "720.00", "currency": "USD",
            "hotelName": "Test Hotel",
            "checkin": "2026-10-15", "checkout": "2026-10-20",
            "bookingDeepLink": "https://example.test/h_1",
        }

    flight_resp = lambda rec: (200, _ok_payload({"source": "fixture", "offers": [_flight_offer()]}))
    hotel_resp  = lambda rec: (200, _ok_payload({"source": "fixture", "hotels": [_hotel_offer()]}))

    with _mock_mcp({"search_flight_offers": flight_resp}) as (fl_srv, fl_url), \
         _mock_mcp({"search_hotel_offers":  hotel_resp})  as (ht_srv, ht_url):

        monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", fl_url)
        monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  ht_url)

        watches.put_item(Item=make_watch("user-aaaa1111", "w1", destination="Tokyo"))
        watches.put_item(Item=make_watch("user-bbbb2222", "w2", destination="Paris",
                                          earliest_depart="2026-12-20", nights=3))
        watches.put_item(Item=make_watch("user-cccc3333", "w3", destination="London",
                                          preferences={"maxStops": 0, "hotelMinStars": 5}))
        watches.put_item(Item=make_watch("user-dddd4444", "w-paused", status="paused"))
        watches.put_item(Item=make_watch("user-eeee5555", "w-archived", status="archived"))

        result = app.handler({}, None)

    # 3 active watches → exactly 3 flight calls + 3 hotel calls = 6 total.
    assert len(fl_srv.calls) == 3
    assert len(ht_srv.calls) == 3
    assert result == {"watches_polled": 3, "watches_errored": 0}

    # Every call carries a JWT whose `sub` is `trip-tracker-poller` (the
    # mock already rejected anything else with a 401).
    assert all(c["sub"] == "trip-tracker-poller" for c in fl_srv.calls + ht_srv.calls)

    # And the per-call `user_id` claim matches the watch's owner — proves
    # the JWT was minted per-watch, not reused across users.
    fl_users = sorted(c["user_id"] for c in fl_srv.calls)
    assert fl_users == sorted(["user-aaaa1111", "user-bbbb2222", "user-cccc3333"])

    # Argument shape — pull the london (w3) call which has preferences set.
    london_flight = next(c for c in fl_srv.calls if c["arguments"]["destination"] == "London")
    assert london_flight["arguments"]["maxStops"] == 0
    london_hotel = next(c for c in ht_srv.calls if c["arguments"]["city"] == "London")
    assert london_hotel["arguments"]["minStars"] == 5

    # Date math for the Paris watch — earliest 2026-12-20, 3 nights.
    paris_flight = next(c for c in fl_srv.calls if c["arguments"]["destination"] == "Paris")
    assert paris_flight["arguments"]["departDate"] == "2026-12-20"
    assert paris_flight["arguments"]["returnDate"] == "2026-12-23"

    # Logs: flights_searched + hotels_searched per active watch + the
    # poll_complete summary.
    assert len(_events(log.records, "flights_searched")) == 3
    assert len(_events(log.records, "hotels_searched")) == 3
    assert len(_events(log.records, "watch_errored")) == 0

    # Each search log carries offer/hotel count and source.
    fl_log = _events(log.records, "flights_searched")[0]
    assert fl_log.offer_count == 1
    assert fl_log.source == "fixture"


def test_one_failing_mcp_does_not_block_other_watches(app_module, monkeypatch):
    """ADR 0003: per-watch error isolation. Three watches; the second's
    flights call returns 500. The first and third should still succeed; the
    second should be logged as `watch_errored` and the loop should keep going.
    """
    app, watches, _, log = app_module

    def _flight_offer(args):
        return {
            "id": "off_1", "totalAmount": "999.00", "currency": "USD",
            "slices": [{"stops": 0, "segments": [{
                "airline": "UA", "flightNumber": "1",
                "departAt": args["departDate"] + "T10:00:00",
            }]}],
        }

    # Custom flights responder: 500 for `dest=Paris`, 200 for everything else.
    def flight_resp(record):
        if record["arguments"]["destination"] == "Paris":
            return 500, b'{"error":"upstream"}'
        return 200, _ok_payload({"source": "fixture", "offers": [_flight_offer(record["arguments"])]})

    # Hotels responder needs at least one valid hotel for compose_snapshot
    # to produce a row; otherwise compose returns None (snapshot_skipped),
    # which is a valid path but not what this test is asserting.
    def hotel_resp(record):
        args = record["arguments"]
        return 200, _ok_payload({"source": "fixture", "hotels": [{
            "id": "h_1", "totalAmount": "100.00", "currency": "USD",
            "hotelName": "x", "checkin": args["checkin"], "checkout": args["checkout"],
            "bookingDeepLink": "https://example.test/x",
        }]})

    with _mock_mcp({"search_flight_offers": flight_resp}) as (fl_srv, fl_url), \
         _mock_mcp({"search_hotel_offers":  hotel_resp})  as (ht_srv, ht_url):

        monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", fl_url)
        monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  ht_url)

        watches.put_item(Item=make_watch("user-1", "w-tokyo", destination="Tokyo"))
        watches.put_item(Item=make_watch("user-2", "w-paris", destination="Paris"))
        watches.put_item(Item=make_watch("user-3", "w-rome",  destination="Rome"))

        result = app.handler({}, None)

    # All 3 flight calls were attempted (Paris hit + failed; the loop didn't
    # short-circuit on the first error).
    assert len(fl_srv.calls) == 3

    # Hotel calls happen only when the flight call succeeds (since flights
    # is called first inside `_poll_one`). So Tokyo + Rome got hotel calls,
    # Paris did not.
    assert len(ht_srv.calls) == 2
    assert {c["arguments"]["city"] for c in ht_srv.calls} == {"Tokyo", "Rome"}

    assert result == {"watches_polled": 3, "watches_errored": 1}

    errored_logs = _events(log.records, "watch_errored")
    assert len(errored_logs) == 1
    assert errored_logs[0].watch_id == "w-paris"
    assert errored_logs[0].status == 500


def test_handler_skips_when_no_active_watches(app_module, monkeypatch):
    """No watches → no MCP traffic at all. Belt-and-braces against an
    expensive call going out for an empty table."""
    app, watches, _, log = app_module

    with _mock_mcp({}) as (fl_srv, fl_url), \
         _mock_mcp({}) as (ht_srv, ht_url):

        monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", fl_url)
        monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  ht_url)

        watches.put_item(Item=make_watch("u1", "w-archived", status="archived"))

        result = app.handler({}, None)

    assert fl_srv.calls == []
    assert ht_srv.calls == []
    assert result == {"watches_polled": 0, "watches_errored": 0}


def test_token_with_wrong_sub_rejected_by_authorizer_path(app_module, monkeypatch):
    """Mock authorizer rejects any token whose `sub != "trip-tracker-poller"`
    — the same rule `lambdas/mcp-authorizer/index.js` enforces (ADR 0006).
    We patch the poller's `sign_for_user` to mint a token with
    `sub="attacker"`, then expect every watch to land in `watch_errored`.
    """
    import jwt as pyjwt_lib

    app, watches, _, log = app_module

    with _mock_mcp({}) as (fl_srv, fl_url), \
         _mock_mcp({}) as (ht_srv, ht_url):

        monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", fl_url)
        monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  ht_url)

        def _attacker_token(user_id, **_kw):
            return pyjwt_lib.encode(
                {"sub": "attacker", "user_id": user_id, "exp": 9999999999},
                SECRET, algorithm="HS256",
            )
        monkeypatch.setattr(app, "sign_for_user", _attacker_token)

        watches.put_item(Item=make_watch("u-x", "w1", destination="Tokyo"))
        result = app.handler({}, None)

    # Mock rejects sub != travel-agent with 401 — the call still hits the
    # server (so server records the call) but the response status is 401
    # so the poller treats it as `watch_errored`.
    assert len(fl_srv.calls) == 1
    assert result == {"watches_polled": 1, "watches_errored": 1}

    errored = [r for r in log.records if r.msg == "watch_errored"]
    assert len(errored) == 1
    assert errored[0].status == 401


def test_watch_errored_log_does_not_carry_response_body(app_module, monkeypatch):
    """Reflected response bodies can include sensitive content (request
    echoes, JWT parse errors, internal error messages). The handler must
    log the reason + status only, never the body. Security audit LOW-1.
    """
    app, watches, _, log = app_module

    sensitive_body = b'{"error":"reflected user_id victim-uuid-1234"}'
    flight_resp = lambda _r: (500, sensitive_body)

    with _mock_mcp({"search_flight_offers": flight_resp}) as (_fl, fl_url), \
         _mock_mcp({}) as (_ht, ht_url):

        monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", fl_url)
        monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  ht_url)

        watches.put_item(Item=make_watch("user-x", "w1", destination="Tokyo"))

        app.handler({}, None)

    errored = [r for r in log.records if r.msg == "watch_errored"]
    assert len(errored) == 1
    rec = errored[0]
    # The reason field carries the categorised string; body must not appear.
    record_str = str(rec.__dict__)
    assert "victim-uuid-1234" not in record_str
    # Confirm the legitimate fields are present.
    assert rec.status == 500
    assert "mcp_http_error" in rec.reason


def test_handler_fails_loud_if_endpoints_not_configured(app_module, monkeypatch):
    """Unconfigured endpoints surface as EnvironmentError at the *start*
    of handler() — before any DDB or MCP traffic. Avoids the in-loop
    TypeError that would crash the whole invocation past the first watch.
    """
    app, watches, _, _ = app_module
    monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", "")
    monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  None)

    watches.put_item(Item=make_watch("u1", "w1", destination="Tokyo"))

    import pytest
    with pytest.raises(EnvironmentError, match="FLIGHTS_MCP_ENDPOINT|HOTELS_MCP_ENDPOINT"):
        app.handler({}, None)
