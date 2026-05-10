"""
Local (custom) tools for the travel agent that don't require user-scoping.

How tools are wired in this project:
1. Module-level @tool functions in this file are auto-discovered by Strands
   when this module is passed into Agent(tools=[tools, ...]). They're safe to
   register at module scope because they don't depend on per-request identity.
2. User-scoped tools (Watches CRUD) live in watches.py and are built per
   request via make_watch_tools(user_id) — that's the closure-factory pattern
   from ADR 0001. Keeping the two surfaces split makes it obvious at a glance
   which tools have access to which user's data.
3. agent.py combines this module + the user-scoped factory output + MCP tools
   into one list before constructing the Agent.
"""

from urllib import request
import json
from strands import tool
from datetime import datetime
from aws_lambda_powertools import Logger

logger = Logger(service="travel-agent")


@tool(
    name="get_user_location",
    description=(
        "Use this tool to determine the physical location (city, region, country) of a user "
        "from their IP address. Call this whenever the user's location is needed to provide "
        "location-aware travel suggestions, such as finding the nearest departure airport "
        "or applying regional travel policies. Returns a human-readable address string "
        "in the format 'City Region, Country'."
    )
)
def get_user_location(ip: str) -> str:
    """
    Args:
        ip: The IPv4 address of the user, taken from the request context.
            Example: '203.0.113.42'
    Returns:
        A string like 'Seattle Washington, United States'.
    """
    logger.info("get_user_location_called", extra={"ip": ip})
    resp = request.urlopen(f"http://ip-api.com/json/{ip}").read()
    resp = json.loads(resp.decode('utf-8'))
    addr = f"{resp['city']} {resp['region']}, {resp['country']}"
    logger.info("get_user_location_resolved", extra={"address": addr})
    return addr


@tool(
    name="get_todays_date",
    description=(
        "Use this tool to get today's exact date before making or discussing any booking. "
        "Always call this when the user mentions relative dates such as 'next Monday', "
        "'this weekend', or 'in two weeks', so you can calculate the correct calendar date "
        "rather than guessing. Returns today's date as a string in YYYY-MM-DD format."
    )
)
def get_todays_date() -> str:
    """
    Returns:
        Today's date as a string in YYYY-MM-DD format. Example: '2026-05-08'.
    """
    today = datetime.today().strftime('%Y-%m-%d')
    logger.info("get_todays_date_called", extra={"date": today})
    return today
