"""End-to-end poll-cycle test exercising every layer in one handler
invocation:

  - DDB Watches enumeration with mixed active / paused / archived rows
  - JWT signing per watch
  - Live HTTPS calls to mock MCPs that re-verify the JWT
  - Snapshot composition + FareHistory write
  - 30-day history Query with pre-existing rows from DDB
  - Gate routing + decision delegate + four EMF metrics

Asserts on:
  - FareHistory rows materialised (one per active watch, none per inactive)
  - All four metrics emitted with the right counts
  - Per-watch logs have the structured fields the production-readiness
    companion §3.2 commits to (`watch_id`, `user_id_prefix`, decision
    `alert` + `reason`)
  - `dedup_blocked` path verified by a watch with `lastAlertedPrice`
    set such that the gate denies the alert
  - `is_anomaly = True` path verified by pre-seeding FareHistory rows
    that make the new total a 30-day low
"""

import json
import socket
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt as pyjwt

from tests.conftest import make_watch


SECRET = "test-secret-aaaaaaaaaaaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# Mock MCPs — re-verify JWT, return canned envelopes per destination/city.
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        try:
            claims = pyjwt.decode(token, SECRET, algorithms=["HS256"])
        except Exception:
            self.send_response(401); self.end_headers(); return
        if claims.get("sub") != "trip-tracker-poller":
            self.send_response(401); self.end_headers(); return
        tool = body["params"]["name"]
        args = body["params"]["arguments"]
        status, payload = self.server.responder(tool, args, claims)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_a, **_k): pass


def _ok(payload: dict) -> bytes:
    return json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
    }).encode("utf-8")


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


def _emf_value(emf, name):
    if name not in emf:
        return 0
    val = emf[name]
    return int(sum(val)) if isinstance(val, list) else int(val)


# ---------------------------------------------------------------------------
# Per-watch responders. Total prices are tuned to drive specific gate paths:
#   w-alert    → flight 1100 + hotel 300 = 1400; max 1500 → threshold passes → alert
#   w-anomaly  → flight  500 + hotel 200 =  700; max  600 (no threshold);
#                pre-seeded history of $1500 → 700 < 0.85×1500 = 1275 → anomaly
#   w-noalert  → flight 1500 + hotel 500 = 2000; max 1000; no history → both gates fail
#   w-dedup    → flight  900 + hotel 100 = 1000; lastAlertedPrice 1000 → dedup blocks
# ---------------------------------------------------------------------------

# Flight responder keys on IATA, hotel responder keys on city. Both
# point at the same logical watch — keep the two halves in lockstep.
FLIGHT_PRICES = {
    "NRT":  "1100.00",  # w-alert (Tokyo)
    "KIX":  "500.00",   # w-anomaly (Osaka)
    "CDG":  "1500.00",  # w-noalert (Paris)
    "FCO":  "900.00",   # w-dedup (Rome)
}
HOTEL_PRICES = {
    "Tokyo": "300.00",
    "Osaka": "200.00",
    "Paris": "500.00",
    "Rome":  "100.00",
}


def _flight_for(_tool, args, _claims):
    flight_total = FLIGHT_PRICES[args["destination"]]
    return 200, _ok({"source": "fixture", "offers": [{
        "id": f"off_{args['destination']}", "totalAmount": flight_total, "currency": "USD",
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
    }]})


def _hotel_for(_tool, args, _claims):
    hotel_total = HOTEL_PRICES[args["city"]]
    return 200, _ok({"source": "fixture", "hotels": [{
        "id": f"h_{args['city']}", "totalAmount": hotel_total, "currency": "USD",
        "hotelName": f"{args['city']} Test", "checkin": args["checkin"], "checkout": args["checkout"],
        "bookingDeepLink": f"https://example.test/{args['city']}",
    }]})


def test_full_e2e_poll(app_module, monkeypatch):
    app, watches, fare, log = app_module

    # Hook to capture EMF before the handler clears it. Use monkeypatch so
    # the restore happens automatically even if assertions raise.
    captured = []
    import metrics as metrics_module

    def _capture():
        captured.append(metrics_module.metrics.serialize_metric_set())
        metrics_module.metrics.clear_metrics()

    monkeypatch.setattr(metrics_module.metrics, "flush_metrics", _capture)

    # Pre-seed FareHistory: w-anomaly has prior $1500 prints so its
    # current $700 total is well below 0.85 × median.
    now = datetime.now(timezone.utc)
    for days_ago in (5, 10, 15, 20):
        fare.put_item(Item={
            "watchId": "w-anomaly",
            "timestamp": (now - timedelta(days=days_ago)).isoformat(),
            "totalPrice": Decimal("1500.00"),
            "flightPrice": Decimal("1200.00"),
            "hotelPrice": Decimal("300.00"),
            "ttl": int((now - timedelta(days=days_ago)).timestamp()) + 90 * 86400,
        })

    with _serve(_flight_for) as fl, _serve(_hotel_for) as ht:
        monkeypatch.setattr(app, "FLIGHTS_MCP_ENDPOINT", fl)
        monkeypatch.setattr(app, "HOTELS_MCP_ENDPOINT",  ht)

        watches.put_item(Item=make_watch("u1", "w-alert",   destination="Tokyo",  max_total_price=1500.0))
        watches.put_item(Item=make_watch("u2", "w-anomaly", destination="Osaka",  max_total_price=600.0))
        watches.put_item(Item=make_watch("u3", "w-noalert", destination="Paris",  max_total_price=1000.0))
        watches.put_item(Item=make_watch("u4", "w-dedup",   destination="Rome",   max_total_price=2000.0,
                                          last_alerted_price=1000.0,
                                          last_alerted_at="2026-05-01T00:00:00+00:00"))
        # Inactive — must not appear in any FareHistory row, must not
        # bump any metric.
        watches.put_item(Item=make_watch("u5", "w-paused",   status="paused"))
        watches.put_item(Item=make_watch("u6", "w-archived", status="archived"))

        result = app.handler({}, None)

    # ------------------------------------------------------------------
    # Handler return shape — counts the 4 active watches polled and 0 errors.
    # ------------------------------------------------------------------
    assert result == {"watches_polled": 4, "watches_errored": 0}

    # ------------------------------------------------------------------
    # Metrics: one EMF blob, all four counters with expected cardinality.
    # ------------------------------------------------------------------
    assert len(captured) == 1, "handler must flush metrics exactly once"
    emf = captured[0]
    assert _emf_value(emf, "watches_polled") == 4
    assert _emf_value(emf, "watches_errored") == 0
    # bedrock_decisions_made counts ACTUAL model invocations: only the
    # watches whose snapshots cleared the dedup gate AND passed at least
    # one of (threshold, anomaly). w-alert (threshold) + w-anomaly (anomaly)
    # = 2. w-noalert (no gate passed) and w-dedup (dedup blocked) don't
    # invoke the model.
    assert _emf_value(emf, "bedrock_decisions_made") == 2
    # alerts_sent matches bedrock_decisions_made under stub mode (the
    # stub always returns alert=True when called). In live mode the real
    # model can return alert=False even when called.
    assert _emf_value(emf, "alerts_sent") == 2

    # ------------------------------------------------------------------
    # FareHistory rows materialised — one new row per active watch.
    # (w-anomaly already has 4 pre-seeded rows + 1 new = 5.)
    # ------------------------------------------------------------------
    items = fare.scan().get("Items", [])
    by_watch = {}
    for it in items:
        by_watch.setdefault(it["watchId"], []).append(it)

    assert set(by_watch.keys()) == {"w-alert", "w-anomaly", "w-noalert", "w-dedup"}
    assert len(by_watch["w-alert"]) == 1
    assert len(by_watch["w-anomaly"]) == 5     # 4 pre-seeded + 1 new
    assert len(by_watch["w-noalert"]) == 1
    assert len(by_watch["w-dedup"]) == 1
    assert "w-paused" not in by_watch
    assert "w-archived" not in by_watch

    # Exact totals — proves snapshot composer summed flight + hotel correctly.
    new_alert = max(by_watch["w-alert"], key=lambda r: r["timestamp"])
    assert new_alert["totalPrice"] == Decimal("1100.00") + Decimal("300.00")

    # ------------------------------------------------------------------
    # Decision logs — every active watch produces a `decision_made` event;
    # the alert/reason combinations match the gate routing.
    # ------------------------------------------------------------------
    decisions = {r.watch_id: r for r in log.records if r.msg == "decision_made"}
    assert set(decisions.keys()) == {"w-alert", "w-anomaly", "w-noalert", "w-dedup"}
    assert decisions["w-alert"].alert is True
    assert decisions["w-alert"].reason == "stub"
    assert decisions["w-anomaly"].alert is True
    assert decisions["w-anomaly"].reason == "stub"
    # The anomaly path saw the pre-seeded history rows.
    assert decisions["w-anomaly"].history_size == 4
    assert decisions["w-noalert"].alert is False
    assert decisions["w-noalert"].reason == "no_gate_passed"
    assert decisions["w-dedup"].alert is False
    assert decisions["w-dedup"].reason == "dedup_blocked"

    # ------------------------------------------------------------------
    # Structured-log contract from production-readiness companion §3.2:
    # every decision log carries `watch_id` + `user_id_prefix`.
    # ------------------------------------------------------------------
    for rec in decisions.values():
        assert hasattr(rec, "user_id_prefix")
        assert len(rec.user_id_prefix) <= 8

    # ------------------------------------------------------------------
    # Sanity: no unhandled exceptions in error path; no `watch_errored`
    # logs were emitted (we crafted no failures into this scenario).
    # ------------------------------------------------------------------
    errored = [r for r in log.records if r.msg == "watch_errored"]
    assert errored == []
