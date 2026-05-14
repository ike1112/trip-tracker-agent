"""
Deliberately splits tools into two categories:

1) Local tools (tools.py) — simple, stateless utilities baked into the agent
   Lambda itself (e.g. get today's date, look up a user's location).

2) MCP tools (this module) — richer, domain-specific capabilities served by
   separate Lambdas. Two MCP servers are wired in: `flights-mcp` (Duffel-backed
   flight search) and `hotels-mcp` (LiteAPI-backed hotel search). Each runs
   as its own Lambda + API Gateway + JWT authorizer.

The MCP split is a real architectural decision:

- Separation of concerns: the agent Lambda owns reasoning and orchestration;
  each MCP server owns one domain's API integration. Each can be deployed,
  scaled, and updated independently without touching the others.

- Security boundary: every MCP call carries a user-scoped JWT signed here with
  a shared secret (JWT_SIGNATURE_SECRET). Each MCP server's authorizer Lambda
  validates that JWT before invoking the tool handler. This means even if
  someone could call an MCP server directly, they would still need a valid
  signed token.

- Tool discovery at runtime: the agent calls list_tools_sync() against each
  configured endpoint and merges the results. New tools added on any MCP
  server are picked up automatically — no agent redeployment needed.

- Tolerance for partial outages: the endpoint loop catches per-endpoint
  failures so a single MCP server being down (or intentionally disabled
  via an empty env var) degrades only that one tool surface rather than
  taking the whole turn down.
"""

from user import User
import jwt
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client
import os
from aws_lambda_powertools import Logger

# Shared secret used to sign internal JWTs sent to the MCP server.
# This is different from Cognito — it's a service-to-service credential,
# not a user-facing token.
jwt_signature_secret = os.environ['JWT_SIGNATURE_SECRET']

# Endpoints for each MCP server the agent talks to. All values are
# injected by CDK at deploy time. Empty or unset endpoints are tolerated
# so the agent still boots if a server is mid-deploy or intentionally
# disabled in this environment.
_mcp_endpoints = [
    ("flights", os.getenv("FLIGHTS_MCP_ENDPOINT")),
    ("hotels",  os.getenv("HOTELS_MCP_ENDPOINT")),
]

l = Logger(service="travel-agent")

# Module-level caches keyed by user.id.
# Why cache per user? Lambda execution environments can be reused across warm
# invocations. Reusing the MCP client avoids the overhead of re-establishing
# the HTTP connection and re-fetching the tool list on every request.
# Each user gets their own client so user-scoped tokens are never shared.
#
# ⚠️  Cache invalidation design consideration:
# The cache has NO TTL and NO invalidation mechanism. If the MCP server adds,
# removes, or updates a tool, this Lambda instance will keep using the stale
# tool list until AWS recycles the execution environment (cold start).
#
# Lambda execution environment lifetime varies:
# - Inactive environments are typically recycled after 5–15 minutes of no traffic.
# - Under sustained traffic, a warm instance can live for hours. In that case
#   the stale tool list persists the entire time — regardless of MCP deploys.
#
# Choose a strategy based on how often MCP tools change in your use case:
#
# Use case A — tools only change on deployment (most common):
#   Best approach: bump a TOOLS_VERSION environment variable in the agent Lambda
#   as part of every MCP server deploy. This forces a cold start and obviates
#   the need for TTL logic entirely. The in-memory cache becomes a pure
#   performance optimisation with no correctness risk.
#
# Use case B — tools change infrequently but independently of agent deploys:
#   Best approach: add a TTL (e.g. 5 minutes) alongside the cached tools.
#   Re-fetch when the TTL expires. The TTL must be shorter than the expected
#   warm environment lifetime (hours under load), not just shorter than the
#   idle recycle window (5–15 min), otherwise the TTL never fires under traffic.
#   Impact of a stale list: missing a new tool (degraded, not broken) or calling
#   a removed tool (fails gracefully; agent can recover).
#
# Use case C — tools change frequently or are dynamic per tenant:
#   Best approach: re-fetch on every request. Remove the cache entirely.
#   Accept the latency cost of a list_tools_sync() call per invocation.
#
# Use case D — zero-latency + always-fresh requirement:
#   Best approach: use MCP protocol change notifications so the server pushes
#   tool list updates to the client rather than polling.
# Caches keyed by user.id. Values are lists across all MCP endpoints.
mcp_tools = {}
mcp_clients = {}


def _connect(endpoint_name: str, endpoint_url: str, token: str):
    """Open one MCP client over Streamable HTTP and return (client, tools)."""
    l.info("mcp_connect", extra={"endpoint": endpoint_name, "url": endpoint_url})
    client = MCPClient(lambda: streamablehttp_client(
        url=endpoint_url,
        headers={"Authorization": f"Bearer {token}"},
    ))
    client.start()
    tools = client.list_tools_sync()
    return client, tools


def get_mcp_tools_for_user(user: User):
    """
    Return the union of MCP tools available for this user across every
    configured MCP endpoint. Connections are created once per user per
    warm Lambda instance and cached.

    Any single endpoint failing to connect is logged and skipped — the
    agent should keep working with whatever subset of MCP servers is
    reachable rather than crashing the whole turn.
    """
    if user.id in mcp_tools and user.id in mcp_clients:
        l.info("mcp_cache_hit", extra={"user_id_prefix": user.id[:8]})
        return mcp_tools[user.id]

    l.info("mcp_cache_miss", extra={"user_id_prefix": user.id[:8]})

    # Mint a short-lived internal JWT that identifies both the calling service
    # ("travel-agent") and the end user. The MCP server validates this token
    # so it can apply user-specific policies (e.g. travel budget limits).
    # One token works for every endpoint — they share the same JWT secret.
    token = jwt.encode({
        "sub": "travel-agent",
        "user_id": user.id,
        "user_name": user.name,
    }, jwt_signature_secret, algorithm="HS256")

    clients = []
    tools = []
    for name, url in _mcp_endpoints:
        if not url:
            l.info("mcp_endpoint_skipped_empty", extra={"endpoint": name})
            continue
        try:
            client, endpoint_tools = _connect(name, url, token)
            clients.append(client)
            tools.extend(endpoint_tools)
        except Exception as e:
            l.exception(f"mcp_connect_failed endpoint={name}: {e}")

    mcp_clients[user.id] = clients
    mcp_tools[user.id] = tools
    return mcp_tools[user.id]

