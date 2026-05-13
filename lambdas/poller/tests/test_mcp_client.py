"""Tests for `mcp_client` — date-derivation, JSON-RPC envelope, and the
error categorisation that lets the poller skip one bad watch and continue.

Strategy: spin up a tiny `http.server.HTTPServer` on a free port for one
test class, configure each test to send the response shape we want to
exercise (200 with valid envelope, 200 with malformed JSON, 500 with body,
slow handler that triggers `urlopen` timeout, etc.). This exercises the
real `urllib.request` code path — no monkeypatching of internal symbols.
The mock server lives only for the duration of one test and is torn down
in `finally`.

Authentication isn't tested at the HTTP level here — the mock doesn't run
the real authorizer. `test_jwt_signer.py` covers the token contract;
`test_handler_with_mcp.py` covers the JWT-over-HTTP wiring end-to-end.
"""

import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import mcp_client
from mcp_client import (
    MAX_RESPONSE_BYTES,
    MCP_TIMEOUT_SECONDS,
    McpCallError,
    call_flights,
    call_hotels,
    derive_dates,
)


# ---------------------------------------------------------------------------
# `derive_dates` — pure function, no I/O. Cover the boundary cases the
# poller exposes from real Watches rows.
# ---------------------------------------------------------------------------

def test_derive_dates_one_night():
    assert derive_dates({"earliestDepart": "2026-10-15", "nights": 1}) == (
        "2026-10-15", "2026-10-16",
    )


def test_derive_dates_five_nights():
    assert derive_dates({"earliestDepart": "2026-10-15", "nights": 5}) == (
        "2026-10-15", "2026-10-20",
    )


def test_derive_dates_handles_decimal_nights_from_ddb():
    """DDB returns Decimal for numeric fields. `int(Decimal('5'))` works
    and the date math should accept it transparently."""
    from decimal import Decimal
    assert derive_dates({"earliestDepart": "2026-12-31", "nights": Decimal("3")}) == (
        "2026-12-31", "2027-01-03",
    )


def test_derive_dates_crosses_month_boundary():
    assert derive_dates({"earliestDepart": "2026-01-30", "nights": 5}) == (
        "2026-01-30", "2026-02-04",
    )


# ---------------------------------------------------------------------------
# Mock JSON-RPC server. Each test installs a response strategy on the
# server before issuing the call.
# ---------------------------------------------------------------------------

class _FakeMcpHandler(BaseHTTPRequestHandler):
    # Default handler — overridden per test by setting `server.respond`.
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.last_request_body = body
        self.server.last_request_headers = dict(self.headers)
        status, payload, sleep_for = self.server.respond(body)
        if sleep_for:
            time.sleep(sleep_for)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload if isinstance(payload, bytes) else payload.encode("utf-8"))

    def log_message(self, *args, **kwargs):
        pass  # suppress noisy stderr per-request logging during tests


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _running_server():
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _FakeMcpHandler)
    server.respond = lambda _body: (200, b'{"jsonrpc":"2.0","id":1,"result":{}}', 0)
    server.last_request_body = None
    server.last_request_headers = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{port}/mcp"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _ok_envelope(payload: dict) -> bytes:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {"type": "text", "text": json.dumps(payload)},
            ],
        },
    }).encode("utf-8")


# ---------------------------------------------------------------------------
# Flight + hotel happy paths — wire-format and request-shape assertions.
# ---------------------------------------------------------------------------

def test_call_flights_returns_parsed_payload_and_sends_correct_request():
    payload = {"source": "fixture", "offers": [{"id": "off_1", "totalAmount": 1234.5}]}
    with _running_server() as (server, url):
        server.respond = lambda _body: (200, _ok_envelope(payload), 0)

        result = call_flights(
            url, "fake-jwt",
            origin="SFO", destination="NRT",
            depart_date="2026-10-15", return_date="2026-10-20",
            pax=2, max_stops=1,
        )

    assert result == payload
    sent = json.loads(server.last_request_body)
    assert sent["method"] == "tools/call"
    assert sent["params"]["name"] == "search_flight_offers"
    assert sent["params"]["arguments"] == {
        "origin": "SFO",
        "destination": "NRT",
        "departDate": "2026-10-15",
        "returnDate": "2026-10-20",
        "pax": 2,
        "maxStops": 1,
    }
    assert server.last_request_headers["Authorization"] == "Bearer fake-jwt"


def test_call_flights_omits_max_stops_when_not_set():
    with _running_server() as (server, url):
        server.respond = lambda _body: (200, _ok_envelope({"source": "live", "offers": []}), 0)

        call_flights(url, "fake-jwt",
                     origin=["SFO", "OAK"], destination="LHR",
                     depart_date="2026-12-01", return_date="2026-12-08",
                     pax=1)

    args = json.loads(server.last_request_body)["params"]["arguments"]
    assert "maxStops" not in args
    assert args["origin"] == ["SFO", "OAK"]  # list origin preserved


def test_call_hotels_returns_parsed_payload_and_sends_correct_request():
    payload = {"source": "fixture", "hotels": [{"id": "h_1", "totalAmount": 720}]}
    with _running_server() as (server, url):
        server.respond = lambda _body: (200, _ok_envelope(payload), 0)

        result = call_hotels(
            url, "fake-jwt",
            city="Tokyo",
            checkin="2026-10-15", checkout="2026-10-20",
            pax=2, min_stars=4,
        )

    assert result == payload
    sent = json.loads(server.last_request_body)
    assert sent["params"]["name"] == "search_hotel_offers"
    assert sent["params"]["arguments"] == {
        "city": "Tokyo",
        "checkin": "2026-10-15",
        "checkout": "2026-10-20",
        "pax": 2,
        "minStars": 4,
    }


# ---------------------------------------------------------------------------
# Error categorisation — every failure mode must surface as `McpCallError`
# so the poller's per-watch try/except can catch one type and continue.
# ---------------------------------------------------------------------------

def test_5xx_raises_mcp_call_error_with_status_and_body():
    with _running_server() as (_server, url):
        _server.respond = lambda _b: (502, b'{"error":"upstream"}', 0)

        with pytest.raises(McpCallError) as exc:
            call_flights(url, "j", origin="SFO", destination="NRT",
                         depart_date="2026-10-15", return_date="2026-10-20", pax=1)

    assert exc.value.status == 502
    assert "upstream" in (exc.value.body or "")
    assert "mcp_http_error" in str(exc.value)


def test_4xx_raises_mcp_call_error():
    with _running_server() as (_server, url):
        _server.respond = lambda _b: (401, b'{"error":"unauthorized"}', 0)

        with pytest.raises(McpCallError) as exc:
            call_hotels(url, "j", city="Paris",
                        checkin="2026-12-20", checkout="2026-12-23", pax=2)

    assert exc.value.status == 401


def test_response_with_non_json_body_raises_mcp_call_error():
    with _running_server() as (_server, url):
        _server.respond = lambda _b: (200, b'<html>oops</html>', 0)

        with pytest.raises(McpCallError, match="mcp_response_not_json"):
            call_flights(url, "j", origin="SFO", destination="NRT",
                         depart_date="2026-10-15", return_date="2026-10-20", pax=1)


def test_jsonrpc_error_envelope_raises_mcp_call_error():
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32601, "message": "Method not found"},
    }).encode("utf-8")
    with _running_server() as (_server, url):
        _server.respond = lambda _b: (200, body, 0)

        with pytest.raises(McpCallError, match="Method not found"):
            call_flights(url, "j", origin="SFO", destination="NRT",
                         depart_date="2026-10-15", return_date="2026-10-20", pax=1)


def test_envelope_missing_result_raises():
    body = json.dumps({"jsonrpc": "2.0", "id": 1}).encode("utf-8")
    with _running_server() as (_server, url):
        _server.respond = lambda _b: (200, body, 0)

        with pytest.raises(McpCallError, match="missing_result"):
            call_flights(url, "j", origin="SFO", destination="NRT",
                         depart_date="2026-10-15", return_date="2026-10-20", pax=1)


def test_envelope_with_empty_content_raises():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": []}}).encode("utf-8")
    with _running_server() as (_server, url):
        _server.respond = lambda _b: (200, body, 0)

        with pytest.raises(McpCallError, match="empty_content"):
            call_flights(url, "j", origin="SFO", destination="NRT",
                         depart_date="2026-10-15", return_date="2026-10-20", pax=1)


def test_text_content_with_invalid_json_payload_raises():
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": "not json{{{"}]},
    }).encode("utf-8")
    with _running_server() as (_server, url):
        _server.respond = lambda _b: (200, body, 0)

        with pytest.raises(McpCallError, match="payload_not_json"):
            call_hotels(url, "j", city="Tokyo",
                        checkin="2026-10-15", checkout="2026-10-20", pax=1)


def test_timeout_raises_mcp_call_error(monkeypatch):
    """Socket-level timeout from `urlopen(timeout=...)` becomes McpCallError.

    Patching `urlopen` directly is more reliable than spinning up a slow
    server — the real `urlopen(timeout=...)` raises `URLError` wrapping
    `socket.timeout` on Linux but `TimeoutError` on Windows, and getting a
    server-side sleep to reliably exceed a test-friendly timeout on every
    OS is fiddly. The mcp_client code path we're testing is the
    `URLError` → `McpCallError` translation, which is what this asserts.
    """
    import socket
    import urllib.error
    import urllib.request

    def _fake_urlopen(*_args, **_kwargs):
        raise urllib.error.URLError(socket.timeout("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    with pytest.raises(McpCallError, match="transport_error|timed out"):
        call_flights("http://example.invalid/mcp", "j",
                     origin="SFO", destination="NRT",
                     depart_date="2026-10-15", return_date="2026-10-20", pax=1)


# ---------------------------------------------------------------------------
# Constant-value pinning.
# ---------------------------------------------------------------------------

def test_mcp_timeout_seconds_constant_pins_to_15():
    """Threat model boundary [3b] cites this 15s value as the LiteAPI
    latency-budget defence; changes to it should be deliberate."""
    assert MCP_TIMEOUT_SECONDS == 15


def test_max_response_bytes_constant_pins_to_2MB():
    """Security audit MED-1 cap. A larger value reopens the OOM-DoS path."""
    assert MAX_RESPONSE_BYTES == 2 * 1024 * 1024


def test_connection_refused_raises_mcp_call_error():
    """Hit a port nothing's listening on — should surface as a transport
    error, not a generic ConnectionRefusedError."""
    bogus = f"http://127.0.0.1:{_free_port()}/mcp"  # nobody listening

    with pytest.raises(McpCallError, match="transport_error"):
        call_hotels(bogus, "j", city="Tokyo",
                    checkin="2026-10-15", checkout="2026-10-20", pax=1)
