"""
HTTP client for calling flights-mcp + hotels-mcp from the poller.

Why stdlib only?
- The Lambda already ships `boto3`, `aws-lambda-powertools`, `pyjwt`, and
  `aws-xray-sdk` (≈ 30MB packed). Adding `httpx` or `requests` is another
  ~3MB and a transitive surface we don't need for two POSTs per watch.
- `urllib.request` is in the stdlib, supports `timeout=`, and raises
  exceptions we can categorise. Good enough for a JSON-RPC client.

What this module does:
- Wraps `tools/call` JSON-RPC calls to flights-mcp's `search_flight_offers`
  and hotels-mcp's `search_hotel_offers`.
- Times out after `MCP_TIMEOUT_SECONDS` (15s — design-spec §5 / threat
  model [3b] LiteAPI). Lambda timeout is 60s; per-watch budget is 2 × 15s
  worst case + headroom.
- Maps non-success outcomes to a single `McpCallError` so the handler's
  per-watch try/except can keep the loop going (ADR 0003 — one bad watch
  never blocks the others).

Date math: the watch's `dateWindow.earliestDepart` + `nights` becomes
`departDate` and `returnDate` for flights, `checkin` and `checkout` for
hotels. The flexible-window sweep ("cheapest day in the window") is
deferred to v1.5; documented in `tasks/slice-5-poller.plan.md` §2.5.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import Any

MCP_TIMEOUT_SECONDS = 15

# Cap on the response body we'll buffer in memory. The MCP servers serialise
# 5–10 offers as JSON which is comfortably under 64 KB; 2 MB is generous
# headroom that still bounds OOM risk if a misbehaving (or compromised)
# upstream returns an unbounded body. See security audit MED-1.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Disable urlopen's silent redirect-following.

    Today the MCP endpoint URLs are CDK-output constants, so SSRF via
    redirect is not exploitable. But urlopen follows up to 10 redirects
    by default, which would let any future change that lets a watch
    field leak into the URL pivot to internal-only endpoints (instance
    metadata, VPC endpoints, etc.). Block redirects entirely — if an MCP
    server ever needs to redirect, that's a deliberate decision we'd
    rather make explicitly. See security audit MED-2.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise McpCallError(f"mcp_redirect_blocked: {code} -> {newurl}", status=code)


_OPENER = urllib.request.build_opener(_NoRedirectHandler())


class McpCallError(Exception):
    """Wraps any failure communicating with an MCP server.

    The handler logs and continues to the next watch on this exception.
    `status` may be `None` for transport-level failures (timeout, DNS).
    """

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body

    def __str__(self) -> str:
        if self.status is not None:
            return f"{self.args[0]} [status={self.status}]"
        return self.args[0]


def _next_request_id() -> int:
    # Monotonic, unique-per-process JSON-RPC request id. We don't run
    # concurrent calls (sequential per ADR 0003) so a counter is enough.
    _next_request_id._n = getattr(_next_request_id, "_n", 0) + 1  # type: ignore[attr-defined]
    return _next_request_id._n  # type: ignore[attr-defined]


def _post_jsonrpc(endpoint: str, jwt_token: str, payload: dict) -> dict:
    """Issue one JSON-RPC POST, returning the parsed response envelope."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {jwt_token}",
        },
    )
    try:
        with _OPENER.open(req, timeout=MCP_TIMEOUT_SECONDS) as resp:
            # Bound how much we'll buffer in memory — see MAX_RESPONSE_BYTES.
            raw_bytes = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw_bytes) > MAX_RESPONSE_BYTES:
                raise McpCallError(
                    f"mcp_response_too_large: > {MAX_RESPONSE_BYTES} bytes",
                    status=resp.status,
                )
            raw = raw_bytes.decode("utf-8")
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                raise McpCallError(
                    f"mcp_response_not_json: {e.msg}", status=resp.status, body=raw[:200]
                ) from e
    except urllib.error.HTTPError as e:
        # Non-2xx response with a (capped) body. Read defensively.
        try:
            body_text = e.read(MAX_RESPONSE_BYTES + 1).decode("utf-8", errors="replace")[:200]
        except Exception:
            body_text = "<unreadable>"
        raise McpCallError(
            f"mcp_http_error: {e.reason}", status=e.code, body=body_text
        ) from e
    except urllib.error.URLError as e:
        # Transport-level: timeout, DNS, connection refused, etc.
        reason = getattr(e, "reason", e)
        raise McpCallError(f"mcp_transport_error: {reason}") from e


def _parse_tool_response(envelope: dict) -> Any:
    """
    Pull the tool's payload out of a JSON-RPC `tools/call` response.

    The MCP server returns `result.content[0].text` containing the JSON the
    tool produced (see `lambdas/{flights,hotels}-mcp/tool-*.js`). Anything
    else — `error` field, missing content, non-text content — is treated
    as a hard failure so the poller can skip this watch instead of writing
    a garbage FareHistory row.
    """
    if "error" in envelope:
        err = envelope["error"]
        raise McpCallError(
            f"mcp_jsonrpc_error: {err.get('message', 'unknown')}",
            status=err.get("code"),
        )
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise McpCallError("mcp_response_missing_result")
    content = result.get("content") or []
    if not content or not isinstance(content, list):
        raise McpCallError("mcp_response_empty_content")
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        raise McpCallError("mcp_response_unexpected_content_type")
    text = first.get("text", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise McpCallError(f"mcp_tool_payload_not_json: {e.msg}", body=text[:200]) from e


def _tool_call(name: str, arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": _next_request_id(),
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def call_flights(endpoint: str, jwt_token: str, *, origin, destination: str,
                 depart_date: str, return_date: str, pax: int, max_stops: int | None = None) -> dict:
    """
    Invoke flights-mcp's `search_flight_offers`. Returns the parsed payload
    `{source, offers: [...]}` exactly as the tool emitted it (see
    `lambdas/flights-mcp/tool-search-offers.js`).
    """
    args: dict = {
        "origin": origin,
        "destination": destination,
        "departDate": depart_date,
        "returnDate": return_date,
        "pax": pax,
    }
    if max_stops is not None:
        args["maxStops"] = max_stops
    envelope = _post_jsonrpc(endpoint, jwt_token, _tool_call("search_flight_offers", args))
    return _parse_tool_response(envelope)


def call_hotels(endpoint: str, jwt_token: str, *, city: str,
                checkin: str, checkout: str, pax: int, min_stars: int | None = None) -> dict:
    """
    Invoke hotels-mcp's `search_hotel_offers`. Returns the parsed payload
    `{source, hotels: [...]}` exactly as the tool emitted it.
    """
    args: dict = {
        "city": city,
        "checkin": checkin,
        "checkout": checkout,
        "pax": pax,
    }
    if min_stars is not None:
        args["minStars"] = min_stars
    envelope = _post_jsonrpc(endpoint, jwt_token, _tool_call("search_hotel_offers", args))
    return _parse_tool_response(envelope)


def derive_dates(date_window: dict) -> tuple[str, str]:
    """
    Pick the (depart, return) date pair the poller searches for.

    For slice 5 we use `earliestDepart` as the depart date and add `nights`
    days for the return date. The flexible-window sweep is deferred to
    v1.5 — see `tasks/slice-5-poller.plan.md` §2.5.

    Returns a `(YYYY-MM-DD, YYYY-MM-DD)` tuple.
    """
    depart_str = date_window["earliestDepart"]
    nights = int(date_window["nights"])
    depart = date.fromisoformat(depart_str)
    return_dt = depart + timedelta(days=nights)
    return depart.isoformat(), return_dt.isoformat()


__all__ = [
    "MCP_TIMEOUT_SECONDS",
    "MAX_RESPONSE_BYTES",
    "McpCallError",
    "call_flights",
    "call_hotels",
    "derive_dates",
]
